# -*- coding: utf-8 -*-
"""表情包素材库 —— 凌暮雪发表情包/图片的出口。

【机制：为什么不让模型自己输出标记】
工具**不直接发**。它把选中素材的绝对路径塞进模块级 pending 队列，
cli.send() 拿到回复后由**代码**把 `[[media:<path>]]` 追加到回复末尾。
微信桥（命令行的 command-handler.js::reply）认得这个标记，摘掉它、改走
已有的图片发送链路（replyFile → 微信 CDN）。

让模型自己输出标记的问题是：它可能漏、可能改写、可能加空格 ——
标记一坏图就发不出去，而且没人知道为什么。放进队列由代码追加，
模型的行为不影响送达。

【素材】
把图片丢进 /opt/character_chat/data/stickers/ 就行（png/jpg/gif/webp）。
文件名会原样列给她看，建议用「情绪_名字.png」这种（如 happy_笑.png），
她按名字挑。没有 manifest，文件名即描述 —— 少一层要维护的东西。

本地补丁（2026-10-09）
"""
from __future__ import annotations

import os
from typing import List

STICKER_DIR = "/opt/character_chat/data/stickers"
MAX_BYTES = 8 * 1024 * 1024          # 微信侧对图片有上限，先卡 8MB
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# 本回合待发的素材（绝对路径）。cli.send() 结束时 drain。
_PENDING: List[str] = []

SEND_STICKER_TOOL = {
    "type": "function",
    "function": {
        "name": "send_sticker",
        "description": (
            "发表情包。素材库里的图会**直接出现在你这条微信回复后面**，学长看得到。"
            "\n· action=list       先看有哪些（不知道有什么就先 list）"
            "\n· action=send name=文件名   发出去"
            "\n**别在文字里描述这张图** —— 图会自己发出去，你只要正常说话就行。"
            "\n**克制**：不是每句都配图。真的想表达某个情绪、或者学长先发了图，才用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "send"], "description": "默认 list"},
                "name": {"type": "string", "description": "action=send 时的素材文件名"},
            },
            "required": ["action"],
        },
    },
}


def _iter_files():
    if not os.path.isdir(STICKER_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(STICKER_DIR)):
        p = os.path.join(STICKER_DIR, fn)
        if os.path.isfile(p) and fn.lower().endswith(IMAGE_EXT):
            out.append((fn, p, os.path.getsize(p)))
    return out


def _resolve(name: str):
    """把名字解析成一个安全路径。只允许库目录**直接**底下的图片。"""
    name = (name or "").strip()
    if not name:
        return None, "没给文件名"
    base = os.path.basename(name)                     # 砍掉任何目录成分
    if base != name or base.startswith("."):
        return None, "只接受素材库里的文件名（不能带路径）"
    cand = os.path.join(STICKER_DIR, base)
    if not os.path.isfile(cand):
        return None, "素材库里没有这个文件"
    if not base.lower().endswith(IMAGE_EXT):
        return None, "不是图片格式"
    if os.path.getsize(cand) > MAX_BYTES:
        return None, "这张超过 8MB，微信发不出去"
    # 路径确实落在库目录里（防御性）
    if os.path.dirname(os.path.realpath(cand)) != os.path.realpath(STICKER_DIR):
        return None, "路径越界"
    return cand, ""


def execute(action: str, name: str = "") -> str:
    action = (action or "list").strip().lower()
    files = _iter_files()

    if action == "list" or not name:
        if not files:
            return ("（表情包素材库是空的 —— %s 里还没有图片。"
                    "告诉学长往那儿丢几张就行。）" % STICKER_DIR)
        lines = ["素材库（%d 张）：" % len(files)]
        for fn, _p, sz in files:
            lines.append("  · %s  (%.0fKB)" % (fn, sz / 1024.0))
        return "\n".join(lines)

    if action != "send":
        return "action 只支持 list / send。"

    path, err = _resolve(name)
    if err:
        avail = ", ".join(fn for fn, _p, _s in files) or "（空）"
        return "发不出去：%s。现有素材：%s" % (err, avail)

    _PENDING.append(path)
    return "（表情包「%s」已经附在这条回复后面了。你别再描述它，正常说话就行。）" % os.path.basename(path)


def drain_pending() -> List[str]:
    """取出并清空本回合待发素材。cli.send() 调用。"""
    out = list(_PENDING)
    del _PENDING[:]
    return out
