"""Web search tool — 给凌暮雪加上网能力。

走 DuckDuckGo（无需 API key），用 duckduckgo_search 库。
LLM 通过 function calling 自决何时调用——问实时信息/外部文档/最新版本时才触发，
日常陪聊不调（省 token、不打断节奏）。

搜索结果精简成 top_k 条摘要回传给 LLM，避免塞太多原文。
"""
import json
import os
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
    """工具分发器：根据 tool_name 执行对应工具，返回结果串。"""
    if tool_name == "web_search":
        return web_search(arguments.get("query", ""))
    return f"(未知工具: {tool_name})"
