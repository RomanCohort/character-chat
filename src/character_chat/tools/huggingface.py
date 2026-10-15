"""HuggingFace 查询工具 — 让凌暮雪能看学长的 HF 主页。

学长的账号: RomanCohort。
走公开 API，无需 token。纯 stdlib，按需 import。
"""
import json
import os
import urllib.error
import urllib.request

from loguru import logger

HF_USER = os.environ.get("HF_USER", "RomanCohort")
USER_AGENT = "ling-muxue/1.0"
API = "https://huggingface.co/api"
TIMEOUT = 20


HF_TOOL = {
    "type": "function",
    "function": {
        "name": "huggingface_ask",
        "description": (
            "查看学长 HuggingFace 上的模型和数据集。"
            "当学长提到他传了模型/数据集、或你想看看他放出来了什么时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_models", "list_datasets"],
                    "description": "list_models=模型 / list_datasets=数据集",
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
    req = urllib.request.Request(API + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def huggingface_ask(action: str, limit: int = 15) -> str:
    """执行 HF 查询，返回精简文本。"""
    n = max(1, min(int(limit or 15), 50))
    try:
        if action == "list_models":
            data = _get(f"/models?author={HF_USER}&limit={n}&sort=lastModified&direction=-1")
            if not data:
                return f"(学长的 HuggingFace @{HF_USER} 上还没有公开模型)"
            lines = [f"学长的 HuggingFace 模型（{len(data)} 个）:"]
            for m in data:
                mid = m.get("modelId") or m.get("id", "?")
                dl = m.get("downloads", 0)
                likes = m.get("likes", 0)
                mod = (m.get("lastModified") or "")[:10]
                lines.append(f"  · {mid}   下载 {dl} / 赞 {likes}   (更新 {mod})")
            return "\n".join(lines)

        if action == "list_datasets":
            data = _get(f"/datasets?author={HF_USER}&limit={n}&sort=lastModified&direction=-1")
            if not data:
                return f"(学长的 HuggingFace @{HF_USER} 上还没有公开数据集)"
            lines = [f"学长的 HuggingFace 数据集（{len(data)} 个）:"]
            for d in data:
                did = d.get("id", "?")
                dl = d.get("downloads", 0)
                likes = d.get("likes", 0)
                mod = (d.get("lastModified") or "")[:10]
                lines.append(f"  · {did}   下载 {dl} / 赞 {likes}   (更新 {mod})")
            return "\n".join(lines)

        return f"(未知 action: {action})"
    except urllib.error.HTTPError as e:
        logger.warning(f"[hf] HTTP {e.code}")
        return f"(HuggingFace 查询失败：HTTP {e.code})"
    except Exception as e:
        logger.warning(f"[hf] failed: {e}")
        return f"(HuggingFace 查询出错了：{e})"
