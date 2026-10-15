"""专业资料查询 — 百科 / 编程问答 / 学术文献。

【为什么不是"通用网页搜索"】
云端是数据中心 IP，通用搜索全军覆没（实测，2026-10-06）：
  · DDG        —— duckduckgo.com 通(200)，但 html/lite 端点 400，ddgs 库直接 No results found
  · Bing       —— 页面拿到了(122KB)，但结果块 b_algo 只出现 1 次：JS 渲染的壳，静态抓不到
  · SearXNG    —— 公共实例(searx.be / priv.au / inetol / searxng.world)全部限流或 result 块 = 0
                  （SearXNG 只是聚合器，上游还是从同一个 IP 抓，绕不开）
  · Mojeek / Marginalia —— 空

能用的只有 API。好在她真正会查的东西（概念/编程/文献）全都有免 key 的垂直 API。

源:
  wikipedia     —— 百科：概念、术语、人物、机构
  stackoverflow —— 编程问答
  scholar       —— 学术文献（OpenAlex，带摘要和引用数）
"""
import html as _html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from loguru import logger

MAILTO = os.environ.get("CONTACT_EMAIL", "18806370529@163.com")
USER_AGENT = f"ling-muxue/1.0 (mailto:{MAILTO})"
TIMEOUT = 25


LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": (
            "查专业资料。source 三选一：\n"
            "  wikipedia     —— 查概念、术语、人物、机构、技术名词的定义\n"
            "  stackoverflow —— 查编程问题、报错、库用法\n"
            "  scholar       —— 查学术文献（返回标题/作者/摘要/引用数）\n"
            "注意：这是查『已存在的资料』。要看实时新闻/最新动态，用不了——那需要通用搜索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["wikipedia", "stackoverflow", "scholar"],
                    "description": "查哪个源",
                },
                "query": {"type": "string", "description": "查询词（英文效果最好）"},
                "limit": {"type": "integer", "description": "返回条数，默认 5"},
            },
            "required": ["source", "query"],
        },
    },
}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _clean(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _wikipedia(query: str, n: int) -> str:
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": n,
    })
    d = _get_json(url)
    hits = ((d.get("query") or {}).get("search")) or []
    if not hits:
        return f"(维基百科上没有「{query}」相关的条目)"
    total = ((d.get("query") or {}).get("searchinfo") or {}).get("totalhits", len(hits))
    lines = [f"维基百科「{query}」（共 {total} 条命中）:"]
    for i, h in enumerate(hits, 1):
        title = h.get("title", "?")
        snippet = _clean(h.get("snippet", ""))[:200]
        lines.append(f"{i}. {title}\n   {snippet}\n   https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")
    return "\n".join(lines)


def _stackoverflow(query: str, n: int) -> str:
    url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode({
        "order": "desc", "sort": "relevance", "q": query,
        "site": "stackoverflow", "pagesize": n, "filter": "default",
    })
    d = _get_json(url)
    items = d.get("items") or []
    if not items:
        return f"(Stack Overflow 上没搜到「{query}」)"
    lines = [f"Stack Overflow「{query}」（{len(items)} 条）:"]
    for i, it in enumerate(items, 1):
        title = _clean(it.get("title", ""))
        score = it.get("score", 0)
        ans = it.get("answer_count", 0)
        accepted = "✓已采纳" if it.get("is_answered") else ""
        link = it.get("link", "")
        lines.append(f"{i}. {title}  (赞 {score} / {ans} 个回答 {accepted})\n   {link}")
    return "\n".join(lines)


def _scholar(query: str, n: int) -> str:
    """OpenAlex —— 学术文献，带摘要和引用数。免 key。"""
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
        "search": query, "per-page": n, "mailto": MAILTO,
    })
    d = _get_json(url)
    items = d.get("results") or []
    if not items:
        return f"(OpenAlex 上没搜到「{query}」)"
    total = (d.get("meta") or {}).get("count", len(items))
    lines = [f"学术文献「{query}」（共 {total} 条命中，显示前 {len(items)}）:"]
    for i, w in enumerate(items, 1):
        title = _clean(w.get("display_name", "?"))
        authors = [a.get("author", {}).get("display_name", "") for a in (w.get("authorships") or [])[:3]]
        a_str = ", ".join(x for x in authors if x) or "—"
        year = w.get("publication_year", "?")
        cited = w.get("cited_by_count", 0)
        doi = w.get("doi") or ""
        # OpenAlex 的摘要是倒排索引，重建一下
        abstract = ""
        inv = w.get("abstract_inverted_index")
        if inv:
            pos = {}
            for word, idxs in inv.items():
                for ix in idxs:
                    pos[ix] = word
            abstract = " ".join(pos[k] for k in sorted(pos))[:280]
        lines.append(
            f"{i}. {title}\n"
            f"   {a_str} ({year})  被引 {cited}\n"
            f"   {abstract}{'…' if len(abstract) >= 280 else ''}\n"
            f"   {doi or 'https://openalex.org/' + str(w.get('id', '')).rsplit('/', 1)[-1]}"
        )
    return "\n".join(lines)


def lookup(source: str, query: str, limit: int = 5) -> str:
    """执行查询，返回精简文本（回传给 LLM）。"""
    if not query or not query.strip():
        return "(查询词是空的)"
    n = max(1, min(int(limit or 5), 10))
    q = query.strip()
    try:
        if source == "wikipedia":
            return _wikipedia(q, n)
        if source == "stackoverflow":
            return _stackoverflow(q, n)
        if source == "scholar":
            return _scholar(q, n)
        return f"(未知 source: {source}，可选 wikipedia / stackoverflow / scholar)"
    except urllib.error.HTTPError as e:
        logger.warning(f"[lookup] {source} HTTP {e.code}")
        return f"({source} 查询失败：HTTP {e.code})"
    except Exception as e:
        logger.warning(f"[lookup] {source} failed: {e}")
        return f"({source} 查询出错了：{type(e).__name__}: {e})"
