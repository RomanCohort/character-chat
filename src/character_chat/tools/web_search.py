"""Web search tool — 给凌暮雪加上网能力。

走 DuckDuckGo（无需 API key），用 duckduckgo_search 库。
LLM 通过 function calling 自决何时调用——问实时信息/外部文档/最新版本时才触发，
日常陪聊不调（省 token、不打断节奏）。

搜索结果精简成 top_k 条摘要回传给 LLM，避免塞太多原文。
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from loguru import logger


# 搜索历史——给主动消息当"搜索源"素材。
# 环形保留最近 50 条，路径相对 character_chat 工作目录（data/）。
SEARCH_HISTORY_FILE = os.environ.get(
    "SEARCH_HISTORY_FILE",
    str(Path(__file__).resolve().parents[3] / "data" / "search_history.json"),
)
SEARCH_HISTORY_MAX = 50

# ── Brave Search API ──────────────────────────────────────────────────────
# 为什么不用 DuckDuckGo: 云服务器(数据中心 IP)上 DDG 会反爬 ——
#   duckduckgo.com 通(200), 但 html/lite 端点返回 400, ddgs 库直接 "No results found";
#   Bing 同理(拿到的是 JS 壳, 静态抓不到 b_algo 结果块)。
# 所以云端走 API。免费档 2000 次/月, key 从环境变量 BRAVE_API_KEY 读。
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "").strip()
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _brave_search(query: str, top_k: int) -> str:
    import urllib.error
    import urllib.parse
    import urllib.request

    url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode(
        {"q": query, "count": max(1, min(top_k, 10))}
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))

    items = ((data.get("web") or {}).get("results")) or []
    if not items:
        return f"(搜了「{query}」，没找到相关结果)"

    lines = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        desc = html_unescape((it.get("description") or "").strip())
        link = (it.get("url") or "").strip()
        if len(desc) > 220:
            desc = desc[:220] + "…"
        lines.append(f"{i}. {title}\n   {desc}\n   {link}")
    result_str = "\n".join(lines)
    _append_search_history(query, (items[0].get("description") or items[0].get("title") or ""))
    return result_str


def html_unescape(s: str) -> str:
    """Brave 的描述里带 <strong> 标签和 HTML 实体，清一下再给 LLM。"""
    import html as _html
    import re as _re
    return _html.unescape(_re.sub(r"<[^>]+>", "", s))


def _append_search_history(query: str, top_result: str):
    """把一次搜索记进历史，供 proactive_scheduler 当话题素材。只存 query + 第一条结果摘要。"""
    try:
        hist_path = Path(SEARCH_HISTORY_FILE)
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist = []
        if hist_path.exists():
            try:
                hist = json.loads(hist_path.read_text(encoding="utf-8"))
                if not isinstance(hist, list):
                    hist = []
            except Exception:
                hist = []
        hist.append({
            "query": query,
            "top_result": top_result[:200],
            "timestamp": datetime.now().isoformat(),
        })
        if len(hist) > SEARCH_HISTORY_MAX:
            hist = hist[-SEARCH_HISTORY_MAX:]
        hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[web_search] append history failed: {e}")


def get_recent_searches(hours: float = 48, limit: int = 5) -> list[dict]:
    """给 proactive_scheduler 用：取最近 N 小时内的搜索历史，按时间倒序。"""
    try:
        hist_path = Path(SEARCH_HISTORY_FILE)
        if not hist_path.exists():
            return []
        hist = json.loads(hist_path.read_text(encoding="utf-8"))
        if not isinstance(hist, list):
            return []
        cutoff = datetime.now() - timedelta(hours=hours)
        recent = []
        for e in hist:
            try:
                if datetime.fromisoformat(e["timestamp"]) >= cutoff:
                    recent.append(e)
            except Exception:
                continue
        recent.sort(key=lambda x: x["timestamp"], reverse=True)
        return recent[:limit]
    except Exception as e:
        logger.warning(f"[web_search] get_recent_searches failed: {e}")
        return []


# OpenAI function-calling tool schema
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索互联网获取实时信息、外部文档、最新版本号、新闻、技术博客等。"
            "当你不确定的事实、需要最新信息、或学长问到外部资源时调用。"
            "日常聊天/情感交流/已掌握的知识不要调用——会打断节奏。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，用最精炼的词（中英文都行）",
                }
            },
            "required": ["query"],
        },
    },
}


def web_search(query: str, top_k: int = 4) -> str:
    """执行 DuckDuckGo 搜索，返回精简后的结果串。

    Args:
        query: 搜索关键词
        top_k: 取前几条结果

    Returns:
        格式化的结果串（title + snippet + url），失败返回错误提示串。
        返回串会作为 tool result 回传给 LLM。
    """
    # 云端优先走 Brave API（DDG 在数据中心 IP 上反爬，见文件头注释）
    if BRAVE_KEY:
        try:
            return _brave_search(query, top_k)
        except Exception as e:
            logger.warning(f"[web_search] Brave 失败，尝试 DDG 兜底: {e}")

    try:
        from ddgs import DDGS
    except ImportError:
        # 旧包名兜底（duckduckgo_search 已重命名为 ddgs）
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return "(搜索功能没装好，跳过)"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=top_k))
        if not results:
            return f"(搜了「{query}」，没找到相关结果)"

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "").strip()
            snippet = r.get("body", r.get("snippet", "")).strip()
            url = r.get("href", r.get("url", "")).strip()
            # 截断超长 snippet，避免吃太多 token
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(f"{i}. {title}\n   {snippet}\n   {url}")
        result_str = "\n".join(lines)
        # 记进搜索历史，供主动消息当"搜索源"话题素材
        top_snippet = results[0].get("body", results[0].get("snippet", "")) if results else ""
        _append_search_history(query, top_snippet or results[0].get("title", ""))
        return result_str
    except Exception as e:
        logger.warning(f"[web_search] failed for '{query}': {e}")
        return f"(搜索出错了：{e})"


def execute_tool(tool_name: str, arguments: dict) -> str:
    """工具分发器 —— 所有工具调用的**唯一咽喉**。

    权限闸和 hook 生命周期挂在这里，而不是散在二十个分支里：
    只要从这里过，就一定被检查过。想绕过得先改这个函数本身。
    """
    from character_chat.tools import permissions as _perm

    allowed, reason = _perm.check(tool_name, arguments)
    if not allowed:
        # 明确说"没执行"，不要含糊成"执行失败"——她要能对学长讲清是哪种情况
        return "（没执行）%s" % reason

    # 计时放在这里，因为只有调用方知道「调用前」是什么时候。
    # PostToolUse 钩子只能拿到结果，拿不到耗时——除非从这里传下去。
    t0 = time.monotonic()
    result = _execute_tool_raw(tool_name, arguments)
    elapsed = time.monotonic() - t0

    _perm.after(tool_name, arguments, result, elapsed)
    return result


def _execute_tool_raw(tool_name: str, arguments: dict) -> str:
    """真正的分发链。不要直接调用它——走 execute_tool，否则绕过权限闸。"""
    if tool_name == "send_sticker":
        from character_chat.tools.stickers import execute as _sticker
        return _sticker(arguments.get("action", "list"), arguments.get("name", ""))
    if tool_name == "web_search":
        return web_search(arguments.get("query", ""))
    if tool_name == "arxiv_search":
        from character_chat.tools.arxiv_search import arxiv_search
        return arxiv_search(
            arguments.get("query", ""),
            arguments.get("category", ""),
            arguments.get("max_results", 5),
        )
    if tool_name == "github_ask":
        from character_chat.tools.github import github_ask
        return github_ask(
            arguments.get("action", ""),
            arguments.get("repo", ""),
            arguments.get("limit", 15),
        )
    if tool_name == "huggingface_ask":
        from character_chat.tools.huggingface import huggingface_ask
        return huggingface_ask(
            arguments.get("action", ""),
            arguments.get("limit", 15),
        )
    if tool_name == "website_manage":
        from character_chat.tools.website import website_manage
        return website_manage(
            arguments.get("action", ""),
            arguments.get("section", ""),
            arguments.get("field", ""),
            arguments.get("value", ""),
        )
    if tool_name == "mirror_update":
        from character_chat.tools.website import mirror_update
        return mirror_update(arguments.get("repo", ""))
    if tool_name == "run_command":
        from character_chat.tools.shell_access import run_command
        return run_command(
            arguments.get("cmd", ""),
            arguments.get("reason", ""),
        )
    if tool_name == "delegate":
        from character_chat.tools.delegate import delegate
        return delegate(
            arguments.get("task", ""),
            arguments.get("max_rounds", 4),
        )
    if tool_name == "lookup":
        from character_chat.tools.lookup import lookup
        return lookup(
            arguments.get("source", ""),
            arguments.get("query", ""),
            arguments.get("limit", 5),
        )
    if tool_name in ("system_status", "read_logs", "ip_action", "read_file", "search_code"):
        from character_chat.tools import server_ops
        if tool_name == "system_status":
            return server_ops.system_status()
        if tool_name == "read_logs":
            return server_ops.read_logs(
                arguments.get("source", ""),
                arguments.get("pattern", ""),
                arguments.get("lines", 50),
            )
        if tool_name == "ip_action":
            return server_ops.ip_action(
                arguments.get("action", ""),
                arguments.get("ip", ""),
                arguments.get("reason", ""),
            )
        if tool_name == "read_file":
            return server_ops.read_file(
                arguments.get("path", ""),
                arguments.get("start", 1),
                arguments.get("lines", 200),
            )
        return server_ops.search_code(
            arguments.get("pattern", ""),
            arguments.get("scope", "all"),
            arguments.get("limit", 30),
        )
    if tool_name in ("remind", "todo", "site_stats"):
        from character_chat.tools import personal
        if tool_name == "remind":
            return personal.remind(
                arguments.get("action", "add"),
                arguments.get("when", ""),
                arguments.get("what", ""),
                arguments.get("id", ""),
            )
        if tool_name == "todo":
            return personal.todo(
                arguments.get("action", "list"),
                arguments.get("text", ""),
                arguments.get("id", ""),
            )
        return personal.site_stats(arguments.get("days", 1))
    return f"(未知工具: {tool_name})"
