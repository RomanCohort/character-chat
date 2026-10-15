"""arXiv 文献查询工具 — 让凌暮雪能自己找论文。

和 web_search 的分工：
  - web_search  : 实时信息 / 外部文档 / 新闻 / 最新版本号
  - arxiv_search: 学术文献 —— 学长让她查文献、问某方向有什么新论文时用

搜到的论文会累积进 library.json（存储盘），去重、可回查，
也供主动消息当"文献源"话题素材（和 search_history 是一个思路）。

全 stdlib（urllib + xml.etree），不引第三方依赖 —— 云端那台 896MB 的机器禁不起。
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from loguru import logger

# 论文库：云端放 /var/lib/arxiv-push/，本地开发可被环境变量覆盖
LIBRARY_FILE = os.environ.get(
    "ARXIV_LIBRARY_FILE",
    "/var/lib/arxiv-push/library.json",
)
LIBRARY_MAX = 2000  # 环形上限，防无限增长

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ENDPOINT = "https://export.arxiv.org/api/query"
USER_AGENT = "ling-muxue-arxiv/1.0 (contact: 18806370529@163.com)"
REQUEST_TIMEOUT = 25


# --------------------------------------------------------------------------
# OpenAI function-calling schema
# --------------------------------------------------------------------------
ARXIV_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "arxiv_search",
        "description": (
            "在 arXiv 上检索学术论文。当学长让你找文献、问某个方向最近有什么论文、"
            "或需要论文支撑时可以调用。返回标题/作者/摘要/链接，并自动存进论文库，"
            "之后可以追问库里已有的。日常聊天/情感交流不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词，英文效果最好（如 'circRNA structure prediction'、'RNA folding'）",
                },
                "category": {
                    "type": "string",
                    "description": "可选 arXiv 分类限定，如 q-bio.BM / q-bio.QM / cs.LG / cs.CV。不填则全库搜",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最多 10",
                },
            },
            "required": ["query"],
        },
    },
}


# --------------------------------------------------------------------------
# arXiv API
# --------------------------------------------------------------------------
def _strip_version(raw_id: str) -> str:
    """http://arxiv.org/abs/2401.01234v2 -> 2401.01234"""
    tail = raw_id.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", tail)


def _text(node) -> str:
    return re.sub(r"\s+", " ", (node.text or "")).strip()


def _encode_query(q: str) -> str:
    """arXiv 要求: 空格->+, 引号->%22, 括号->%28%29。"""
    return q.replace('"', "%22").replace("(", "%28").replace(")", "%29").replace(" ", "+")


def _parse_feed(xml_text: str) -> list[dict]:
    papers = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        logger.warning(f"[arxiv_search] Atom 解析失败: {e}")
        return papers

    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _text(entry.find(f"{ATOM}id"))
        if not raw_id:
            continue
        cats = [c.get("term", "") for c in entry.findall(f"{ATOM}category")]
        prim = entry.find(f"{ARXIV}primary_category")
        abs_url = raw_id
        for link in entry.findall(f"{ATOM}link"):
            if link.get("rel") == "alternate":
                abs_url = link.get("href", raw_id)
        authors = [_text(a.find(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")]
        papers.append({
            "arxiv_id": _strip_version(raw_id),
            "title": _text(entry.find(f"{ATOM}title")),
            "abstract": _text(entry.find(f"{ATOM}summary")),
            "authors": authors,
            "categories": cats,
            "primary_category": prim.get("term", "") if prim is not None else (cats[0] if cats else ""),
            "abs_url": abs_url,
            "published": _text(entry.find(f"{ATOM}published")),
        })
    return papers


def _fetch(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[arxiv_search] 抓取失败: {e}")
        return None


# --------------------------------------------------------------------------
# 论文库（存储盘）
# --------------------------------------------------------------------------
def _load_library() -> dict:
    p = Path(LIBRARY_FILE)
    if not p.exists():
        return {"papers": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("papers", {})
        return d
    except Exception as e:
        logger.warning(f"[arxiv_search] library 读取失败: {e}")
        return {"papers": {}}


def _save_library(lib: dict) -> None:
    p = Path(LIBRARY_FILE)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 环形裁剪：超上限时丢最旧的
        papers = lib.get("papers", {})
        if len(papers) > LIBRARY_MAX:
            ordered = sorted(papers.items(), key=lambda kv: kv[1].get("added", ""))
            for k, _ in ordered[: len(papers) - LIBRARY_MAX]:
                del papers[k]
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LIBRARY_FILE)  # 原子写
    except Exception as e:
        logger.warning(f"[arxiv_search] library 写入失败: {e}")


def save_papers(papers: list[dict], source: str = "manual_search") -> int:
    """把论文存进库（已存在的只更新 last_seen）。返回新增条数。"""
    lib = _load_library()
    store = lib["papers"]
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for p in papers:
        aid = p.get("arxiv_id")
        if not aid:
            continue
        if aid in store:
            store[aid]["last_seen"] = now
        else:
            store[aid] = {
                "title": p.get("title", ""),
                "abstract": p.get("abstract", "")[:1200],
                "authors": p.get("authors", [])[:5],
                "categories": p.get("categories", [])[:5],
                "primary_category": p.get("primary_category", ""),
                "abs_url": p.get("abs_url", ""),
                "published": p.get("published", ""),
                "added": now,
                "first_source": source,
            }
            added += 1
    lib["last_updated"] = now
    _save_library(lib)
    return added


def get_recent_papers(days: int = 7, limit: int = 10) -> list[dict]:
    """给主动消息当"文献源"话题素材：取库里最近入库的论文。"""
    try:
        lib = _load_library()
        items = list(lib.get("papers", {}).values())
        items.sort(key=lambda x: x.get("added", ""), reverse=True)
        return items[:limit]
    except Exception as e:
        logger.warning(f"[arxiv_search] get_recent_papers failed: {e}")
        return []


def library_stats() -> dict:
    lib = _load_library()
    return {"total": len(lib.get("papers", {})), "last_updated": lib.get("last_updated", "")}


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------
def arxiv_search(query: str, category: str = "", max_results: int = 5) -> str:
    """检索 arXiv，存库，返回精简结果串（回传给 LLM）。"""
    if not query or not query.strip():
        return "(检索词是空的)"

    n = max(1, min(int(max_results or 5), 10))
    q = query.strip()
    if category and category.strip():
        # 分类限定 + 关键词
        search_query = f'cat:{category.strip()} AND all:"{q}"' if " " in q else f"cat:{category.strip()} AND all:{q}"
    else:
        search_query = f'all:"{q}"' if " " in q else f"all:{q}"

    url = (
        f"{ENDPOINT}?search_query={_encode_query(search_query)}"
        f"&sortBy=relevance&sortOrder=descending&start=0&max_results={n}"
    )
    xml_text = _fetch(url)
    if not xml_text:
        return f"(arXiv 检索失败，网络问题，稍后再试)"

    papers = _parse_feed(xml_text)
    if not papers:
        return f"(在 arXiv 上搜「{query}」没找到结果)"

    added = save_papers(papers, source="manual_search")

    lines = []
    for i, p in enumerate(papers, 1):
        authors = p["authors"]
        a_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "") if authors else "—"
        abstract = p["abstract"]
        if len(abstract) > 400:
            abstract = abstract[:400] + "…"
        lines.append(
            f"{i}. {p['title']}\n"
            f"   作者: {a_str}\n"
            f"   分类: {', '.join(p['categories'][:3]) or p['primary_category']}\n"
            f"   摘要: {abstract}\n"
            f"   链接: {p['abs_url']}"
        )
    header = f"arXiv 检索「{query}」共 {len(papers)} 条（{added} 条新入库）:\n"
    return header + "\n".join(lines)
