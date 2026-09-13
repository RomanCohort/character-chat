"""发件箱：iLink 只在"你给 bot 发过消息之后的一小段"里允许 bot 回话。

窗口一关就是 `errcode=-2 prepare failed`——重取 context_token 也没用，
因为坏的不是 token，是窗口。而长活儿（调研、跑测试）经常跑得比窗口长，
于是**结果生成出来了却发不出去**，用户只看到一串心跳，最后什么都没有。

这不是代码 bug，是平台语义。对策只有一条：**发不出去就存着，等窗口重开再发。**

窗口重开的唯一时机 = 用户又来了一条消息。所以补发的触发点就在 `/chat` 入口，
而不是在后台定时重试（定时重试在窗口关着的时候只是白刷错误日志）。

存盘用 JSON，原子写（同 sender.py 的风格），文件放在 state 旁边。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

DEFAULT_PATH = os.environ.get(
    "DSH_WECHAT_OUTBOX", r"D:\character_chat\data\dsh_wechat_outbox.json"
)
# 最多存这么多条，防止失控（真堆满了说明链路长期坏着，该看日志而不是堆文件）
MAX_ITEMS = int(os.environ.get("DSH_WECHAT_OUTBOX_MAX", "50"))


def _log(msg: str) -> None:
    from .sender import _log as sender_log
    sender_log(msg)


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(path: Path, items: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        _log(f"发件箱写入失败：{e!r}")


def enqueue(text: str, to_user_id: str, reason: str = "", path: Optional[str] = None) -> None:
    """把一条发不出去的消息存进发件箱。"""
    p = Path(path or DEFAULT_PATH)
    items = _load(p)
    items.append({
        "text": text,
        "to": to_user_id,
        "reason": reason[:200],
        "at": time.time(),
    })
    overflow = len(items) - MAX_ITEMS
    if overflow > 0:
        # 丢最旧的：新的更可能是用户正在等的那个结果
        items = items[overflow:]
        _log(f"发件箱超上限，丢了最旧的 {overflow} 条")
    _save(p, items)
    _log(f"发件箱 +1（共 {len(items)} 条待发）原因={reason[:80]}")


async def flush(to_user_id: Optional[str] = None, path: Optional[str] = None) -> int:
    """尝试补发。返回成功发出去的条数。

    只在"用户刚来过"的时候调用——那时窗口才开着。发不出去的留在箱里，下次再试。
    """
    from . import sender

    p = Path(path or DEFAULT_PATH)
    items = _load(p)
    if not items:
        return 0

    _log(f"发件箱补发：待发 {len(items)} 条")
    sent = 0
    remaining: list[dict] = []
    for it in items:
        target = to_user_id or it.get("to")
        text = it.get("text") or ""
        if not text:
            continue
        try:
            ok, detail = await sender.send_text(text, target)
        except Exception as e:
            ok, detail = False, repr(e)
        if ok:
            sent += 1
        else:
            remaining.append(it)
            # 第一条就失败说明窗口还没开（或又关了），剩下的没必要继续试
            if sent == 0:
                _log(f"发件箱补发失败（窗口未开）：{detail[:120]}")
                remaining.extend(items[len(remaining) + 1:])
                break

    _save(p, remaining)
    if sent:
        _log(f"发件箱补发完成：成功 {sent} 条，剩 {len(remaining)} 条")
    return sent


def pending_count(path: Optional[str] = None) -> int:
    return len(_load(Path(path or DEFAULT_PATH)))
