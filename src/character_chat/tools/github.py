"""GitHub 查询工具 — 让凌暮雪能看学长的 GitHub。

学长的账号: RomanCohort (= 颜子壹 / Ziyi Yan, 吉林大学计科)。
走公开 API，无需 token。**只能看到公开仓库** —— 私有仓库要 token，这里没有。

纯 stdlib，按需 import，不占常驻内存。
"""
import json
import os
import urllib.error
import urllib.request

from loguru import logger

GITHUB_USER = os.environ.get("GITHUB_USER", "RomanCohort")
USER_AGENT = "ling-muxue/1.0"
API = "https://api.github.com"
TIMEOUT = 20


GITHUB_TOOL = {
    "type": "function",
    "function": {
        "name": "github_ask",
        "description": (
            "查看学长 GitHub 上的仓库列表、某个仓库的详情、最近更新。"
            "当学长提到他的项目、问你「我最近在写什么」、或你想看看某个仓库时调用。"
            "注意：只看得到公开仓库，私有的看不到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_repos", "repo_detail", "recent"],
                    "description": "list_repos=列全部仓库 / repo_detail=看某个仓库详情 / recent=最近在更新的",
                },
                "repo": {
                    "type": "string",
                    "description": "仓库名，action=repo_detail 时必填",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 15",
                },
            },
            "required": ["action"],
        },
    },
}


def _get(path: str):
    req = urllib.request.Request(
        API + path,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _fmt_repo(r: dict, with_desc: bool = True) -> str:
    name = r.get("name", "?")
    desc = (r.get("description") or "").strip()
    lang = r.get("language") or "—"
    stars = r.get("stargazers_count", 0)
    pushed = (r.get("pushed_at") or "")[:10]
    line = f"  · {name}  [{lang}]"
    if stars:
        line += f" ★{stars}"
    if pushed:
        line += f"  (最后更新 {pushed})"
    if with_desc and desc:
        line += f"\n      {desc[:110]}"
    return line


def github_ask(action: str, repo: str = "", limit: int = 15) -> str:
    """执行 GitHub 查询，返回精简文本（回传给 LLM）。"""
    n = max(1, min(int(limit or 15), 50))
    try:
        if action == "list_repos":
            data = _get(f"/users/{GITHUB_USER}/repos?per_page={n}&sort=pushed")
            if not data:
                return f"(学长的 GitHub @{GITHUB_USER} 上没有公开仓库)"
            lines = [f"学长的 GitHub @{GITHUB_USER}（公开仓库 {len(data)} 个，按最近更新排）:"]
            lines += [_fmt_repo(r) for r in data]
            return "\n".join(lines)

        if action == "recent":
            data = _get(f"/users/{GITHUB_USER}/repos?per_page={n}&sort=pushed")
            lines = [f"学长最近在动的仓库（top {min(len(data), n)}）:"]
            lines += [_fmt_repo(r) for r in data]
            return "\n".join(lines)

        if action == "repo_detail":
            if not repo:
                return "(repo_detail 需要给仓库名)"
            r = _get(f"/repos/{GITHUB_USER}/{repo}")
            topics = ", ".join(r.get("topics") or []) or "—"
            return (
                f"仓库 {r.get('full_name')}:\n"
                f"  描述: {(r.get('description') or '—')[:200]}\n"
                f"  语言: {r.get('language') or '—'}   星标: {r.get('stargazers_count', 0)}\n"
                f"  创建: {(r.get('created_at') or '')[:10]}   最后更新: {(r.get('pushed_at') or '')[:10]}\n"
                f"  话题: {topics}\n"
                f"  链接: {r.get('html_url')}"
            )

        return f"(未知 action: {action})"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"(GitHub 上没有找到 {GITHUB_USER}/{repo or ''}——可能是私有仓库，或者名字不对)"
        logger.warning(f"[github] HTTP {e.code}")
        return f"(GitHub 查询失败：HTTP {e.code})"
    except Exception as e:
        logger.warning(f"[github] failed: {e}")
        return f"(GitHub 查询出错了：{e})"
