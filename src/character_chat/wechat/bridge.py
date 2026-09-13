"""微信侧 DSH 命令处理：路由、派发、回执。

`!dsh <任务>` 走 DSH（常驻 ACP 会话），其它消息照旧走凌暮雪。
派发是异步的：/chat 立刻回一句「收到」，真正的结果由本模块用 sender 直接发回微信。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Callable, Optional

from . import outbox, sender
from .acp_client import DEFAULT_CWD, DshBridge, TurnResult
from .sender import _log

PREFIX = os.environ.get("DSH_WECHAT_PREFIX", "*work")
# 心跳：一轮跑得久时，每隔这么多秒推一条"还在跑"。0 = 关。
HEARTBEAT_S = float(os.environ.get("DSH_WECHAT_HEARTBEAT_S", "90"))
# 单轮进度消息上限（工具动作 + 心跳一起算）。跑得久也别把手机刷爆。
PROGRESS_CAP = int(os.environ.get("DSH_WECHAT_PROGRESS_CAP", "30"))

# 触发词。不用 /dsh 这类斜杠命令：微信桥自己占了 /cwd /new /sessions /resume /model
# /mode /exec /file /status /help /yes /no，子命令撞上去会走错门
# （比如"让她换工作目录"变成"改了整个 bot 的 cwd"）。*work 不碰那套。
_ALIASES = ("*work",)

_HELP = """在电脑上干活：*work <任务>

*work 看一下这个测试为什么挂     交给 DSH 跑（连续会话）
*work cwd D:\\某个项目            换工作目录（绝对路径，每个目录各一条会话）
*work new                       开一条新会话（清上下文）
*work list                      当前目录下最近的会话
*work resume <前几位>            接回某条会话
*work status                    会话 / 工作目录 / 忙不忙
*work help                      这条

不带 *work 的消息照旧是聊天，不会碰电脑。"""

_bridge: Optional[DshBridge] = None
_bridge_lock = asyncio.Lock()
_running: dict[str, float] = {}   # chat_id -> 开始时间，防重复派发


def is_command(text: str) -> bool:
    """这条消息是不是 DSH 命令。"""
    t = (text or "").strip()
    for alias in _ALIASES:
        if t == alias or t.startswith(alias + " ") or t.startswith(alias + "\n"):
            return True
    return False


def strip_prefix(text: str) -> str:
    t = (text or "").strip()
    for alias in _ALIASES:
        if t == alias:
            return ""
        if t.startswith(alias):
            return t[len(alias):].strip()
    return t


def _fmt_elapsed(started: float) -> str:
    secs = int(time.time() - started)
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}s"


def _fmt_result(chat_id: str, res: TurnResult, started: float) -> str:
    session = _bridge.dsh_session_of(chat_id) if _bridge else None
    head = f"DSH · {_fmt_elapsed(started)}"
    if session:
        head += f" · {session[:8]}"
    if res.error:
        body = f"✗ {res.error}"
    elif not res.text:
        body = f"（没有正文输出，stop={res.stop_reason or 'unknown'}）"
    else:
        body = res.text
    if res.tool_actions and res.error:
        body += "\n\n动作：\n" + "\n".join(res.tool_actions[-6:])
    return f"{head}\n\n{body}"


async def _get_bridge() -> DshBridge:
    global _bridge
    async with _bridge_lock:
        if _bridge is None:
            _bridge = DshBridge(cwd=DEFAULT_CWD)
        return _bridge


async def shutdown() -> None:
    global _bridge
    if _bridge is not None:
        await _bridge.shutdown()
        _bridge = None


# --- 元命令 ---


async def _handle_meta(chat_id: str, arg: str) -> str:
    bridge = await _get_bridge()
    verb, rest = _split_meta(arg)
    if verb in ("", "help"):
        return _HELP
    if verb == "new":
        sid = await bridge.new_chat_session(chat_id)
        return f"新会话：{sid}\n工作目录：{bridge.cwd_of(chat_id)}"
    if verb == "status":
        sid = bridge.dsh_session_of(chat_id)
        busy = chat_id in _running
        return (
            f"会话：{sid or '（还没开）'}\n"
            f"工作目录：{bridge.cwd_of(chat_id)}\n"
            f"ACP：{'运行中' if bridge.conn and bridge.conn.proc and bridge.conn.proc.returncode is None else '未启动'}\n"
            f"当前：{'在跑任务' if busy else '空闲'}"
        )
    if verb == "cwd":
        if not rest:
            return f"当前工作目录：{bridge.cwd_of(chat_id)}\n换：*work cwd D:\\某个项目"
        try:
            cwd, existing = await bridge.switch_cwd(chat_id, rest)
        except Exception as e:
            return f"换不了：{e}"
        tail = "接回该目录原有会话" if existing else "该目录还没有会话，下一轮建"
        return f"工作目录：{cwd}\n{tail}"
    if verb in ("list", "sessions"):
        sessions = await bridge.list_recent(chat_id)
        if not sessions:
            return f"（{bridge.cwd_of(chat_id)} 下没有历史会话）"
        lines = [f"最近 {min(len(sessions), 10)} 条（{bridge.cwd_of(chat_id)}）："]
        for s in sessions[:10]:
            sid = str(s.get("sessionId") or "")
            title = str(s.get("title") or "").strip().replace("\n", " ")[:24]
            lines.append(f"· {sid[:8]}  {title}")
        lines.append("\n接回：*work resume <前几位>")
        return "\n".join(lines)
    if verb == "resume":
        if not rest:
            return "用法：*work resume <会话id前几位>"
        try:
            sid = await bridge.resume(chat_id, rest)
        except Exception as e:
            return f"接不回去：{e}"
        return f"接回会话：{sid}"
    return _HELP


# --- 转述（凌暮雪的名义）---
# server.py 启动时把凌暮雪的 cli.send 注进来；离线脚本不注，走 fallback 原样输出。
# 铁律：转述只准把话说得像她，不准改事实——失败必须照实说失败。

_narrator: Optional[Callable[[str], str]] = None


def set_narrator(fn: Optional[Callable[[str], str]]) -> None:
    """注册转述器：同步函数，收一段系统提示，回一句她的话。"""
    global _narrator
    _narrator = fn


async def narrate(prompt: str, fallback: str) -> str:
    """把一段系统提示交给凌暮雪说。没注册转述器、或她那边出错，就原样返回。"""
    if _narrator is None:
        return fallback
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, _narrator, prompt)
    except Exception as e:
        _log(f"转述失败，回退原文：{e!r}")
        return fallback
    text = (text or "").strip()
    return text or fallback


def _receipt_prompt(task: str) -> str:
    return (
        f"[系统提示：学长在微信里给你派了一个活，让 DSH（跑在电脑上的编码 agent）去做："
        f"「{task}」。现在已经派下去了，还没结果。]\n"
        f"[用你的语气回一句，一句就够，让他知道你收到了、在等。"
        f"不要用感叹号，不要提 DSH 是什么，不要承诺你做不到的事。]"
    )


def _progress_prompt(action: str) -> str:
    return (
        f"[系统提示：你派的那个活正在跑，电脑那边刚做了一步：{action}]\n"
        f"[用你的语气说一句，一句话，不超过 20 个字。不要感叹号，不要复述原文。]"
    )


def _result_prompt(task: str, detail: str, elapsed: str) -> str:
    return (
        f"[系统提示：学长刚才让你做的「{task}」已经跑完了，耗时 {elapsed}。"
        f"以下是执行结果原文：]\n{detail}\n"
        f"[用你的语气把结果告诉他，简短自然。"
        f"铁律：结果里写着失败/报错/没做完，就必须照实说没做成——不许美化、不许含糊过去；"
        f"技术内容（文件名、路径、报错文本、数字）原样保留，不要改写、不要翻译成别的说法。"
        f"不要用感叹号，不要照搬整段原始输出。]"
    )


# --- 派发 ---

# 子命令写成斜杠或不写斜杠都认：`*work cwd D:\x` 与 `*work /cwd D:\x` 等价。
# 斜杠形式留着只是为了肌肉记忆——**不带斜杠是正路**，免得跟桥自己的 /cwd 混起来。
_META_VERBS = ("help", "new", "status", "list", "cwd", "resume", "sessions")


def _split_meta(arg: str) -> tuple[str, str]:
    """把 `cwd D:\\x` / `/status` 拆成 (verb, 余下)。非元命令返回 ('', arg)。"""
    head, _, rest = arg.partition(" ")
    verb = head.lstrip("/").lower()
    if verb in _META_VERBS:
        return verb, rest.strip()
    return "", arg


async def dispatch(chat_id: str, text: str, to_user_id: Optional[str] = None) -> str:
    """处理一条 DSH 命令，返回立刻要给微信的那句回执。

    真正的结果由后台任务发出去，不占 /chat 的响应。
    """
    arg = strip_prefix(text)

    # 用户刚来过 = iLink 的回复窗口刚开。这是补发发件箱的唯一时机。
    # 放在最前面：先把欠人家的结果还上，再处理这一条。
    try:
        n = await outbox.flush(to_user_id or chat_id)
        if n:
            _log(f"本次入口补发了 {n} 条积压结果")
    except Exception as e:
        _log(f"发件箱补发异常（不影响本轮）：{e!r}")

    # 元命令是同步的（快），且是技术输出——原样回，不转述
    verb, _rest = _split_meta(arg)
    if not arg or verb:
        try:
            return await _handle_meta(chat_id, arg)
        except Exception as e:
            return f"✗ {e!r}"

    if chat_id in _running:
        return f"上一轮还在跑（{_fmt_elapsed(_running[chat_id])}），等它完再发。"

    # 回执**先发**，再想措辞。
    # 原因（2026-09-12 实测）：iLink 只在你给 bot 发过消息之后的一小段内允许 bot 回话。
    # 窗口一关就是 `errcode=-2 prepare failed`，重取 context_token 也没用——坏的不是 token。
    # 而 dispatch 以前是"转述完再回"，遇到 ACP 冷启动 / 凌暮雪实例构造（embedding 联网超时），
    # 一拖就是几分钟，回执必然死在窗口外。所以这条必须是纯文本、立刻发。
    plain = "收到，跑着呢……好了我发你。"
    asyncio.create_task(_send_plain(to_user_id or chat_id, plain))

    _running[chat_id] = time.time()
    asyncio.create_task(_run(chat_id, arg, to_user_id or chat_id))
    return plain


async def _send_plain(to_user_id: str, text: str) -> None:
    """立刻发一句纯文本。不转述、不等任何东西。"""
    try:
        await sender.send_text(text, to_user_id)
    except Exception as e:
        _log(f"回执发送失败：{e!r}")


# 结果归档目录：微信发不出去时，用户可以直接来这儿拿
RESULT_DIR = os.environ.get("DSH_WECHAT_RESULT_DIR", r"D:\ling-muxue-logs\results")


def _archive_result(task: str, raw: str, started: float) -> None:
    """把一条结果原样落盘，文件名带时间戳 + 任务摘要。

    为什么存两份（微信 + 本地）：平台会抽风（回复窗口、errcode、抖动），
    而"结果本身"不该依赖平台脸色。今天实况：一条 3954 字的调研结果发不出去，
    只能从 DSH 会话日志里解压捞回来——有了这个就不用那么费劲。
    """
    import re as _re
    import time as _time
    from pathlib import Path as _Path

    d = _Path(RESULT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    # 任务摘要做文件名：去掉路径分隔符和怪字符，限长
    slug = _re.sub(r"[^\w\u4e00-\u9fff]+", "-", task or "task").strip("-")[:40]
    stamp = _time.strftime("%Y%m%d-%H%M%S", _time.localtime(started))
    f = d / f"{stamp}-{slug}.txt"
    f.write_text(raw, encoding="utf-8")
    _log(f"结果已归档：{f}")


async def _run(chat_id: str, task: str, to_user_id: str) -> None:
    started = _running.get(chat_id, time.time())
    bridge = await _get_bridge()
    notified = 0
    last_ping = time.time()

    async def send_progress(text: str) -> None:
        nonlocal notified
        if notified >= PROGRESS_CAP:
            return
        notified += 1
        await sender.send_text(text, to_user_id)

    async def on_progress(line: str) -> None:
        nonlocal last_ping
        last_ping = time.time()
        await send_progress(await narrate(_progress_prompt(line), fallback=line))

    async def heartbeat() -> None:
        """一轮跑得久，中间得有动静——否则手机上跟死了一样。"""
        nonlocal last_ping
        while True:
            await asyncio.sleep(15)
            if HEARTBEAT_S <= 0 or notified >= PROGRESS_CAP:
                continue
            idle = time.time() - last_ping
            if idle < HEARTBEAT_S:
                continue
            last_ping = time.time()
            mins = int((time.time() - started) // 60)
            await send_progress(f"……还在跑，{mins} 分钟了。")

    hb = asyncio.create_task(heartbeat())
    try:
        res = await bridge.run_turn(chat_id, task, on_progress=on_progress)
        elapsed = _fmt_elapsed(started)
        detail = res.error and f"失败：{res.error}" or (res.text or f"（没有正文输出，stop={res.stop_reason or 'unknown'}）")
        session = bridge.dsh_session_of(chat_id) or ""
        head = f"DSH · {elapsed}" + (f" · {session[:8]}" if session else "")
        raw = f"{head}\n\n{detail}"

        # 结果**先原样发**，再让凌暮雪出个声。
        #
        # 为什么不能只发转述版（2026-09-12 实测的教训）：
        # 以前是 `narrate(..., fallback=raw)`——fallback 只在转述**失败**时兜底。
        # 可转述没失败，它只是按我 prompt 里"简短、不要照搬原始输出"的要求，
        # 产出了一句空话（「……好了。」）。结果正文就这么被吞了。
        #
        # 也**不**走 send_progress：那条有 PROGRESS_CAP 上限，心跳吃掉额度后
        # 结果会被静默丢弃。结果比进度重要，必须直发。
        #
        # 发失败**入发件箱**：iLink 只在"用户来过之后的一小段"里允许回话，
        # 长活儿跑完时窗口常常已经关了（实测 23:29 发的命令，23:31 结果就发不出去）。
        # 发件箱会在用户下一条消息进来时补发。
        ok, detail = await sender.send_text(raw, to_user_id)
        if not ok:
            outbox.enqueue(raw, to_user_id, reason=detail)
            _log(f"结果发不出（{detail[:80]}），已入发件箱")

        # 无论发没发出去，都在本地留一份。
        # 理由：平台侧怎么抽风都不该影响"东西还在"。微信发不出去时，
        # 用户可以直接去目录里拿；我也能从这儿捞回来补发。
        try:
            _archive_result(task, raw, started)
        except Exception as e:
            _log(f"结果归档失败（不影响发送）：{e!r}")
    except Exception as e:
        await sender.send_text(f"✗ 桥断了：{e!r}", to_user_id)
    finally:
        hb.cancel()
        _running.pop(chat_id, None)
