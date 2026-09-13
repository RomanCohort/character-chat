"""微信 → DSH 桥：以 ACP v1 客户端身份驱动 `dsh --profile acp` 常驻进程。

微信侧的消息从 bridge 送到 character_chat 的 /chat，这里的 DshBridge 把它转成
ACP 的 session/prompt，再把 session/update 里的助手正文回给微信。

设计要点：
- 一个 ACP 进程承载全部会话；连接可复用，会话在 DSH 侧是持久化的。
- 每个微信会话（bridge 的 session_id）对应一个 DSH 会话 id，记在 state 文件里，
  进程/服务重启后走 session/resume 接回原来那条线。
- 权限请求一律 allow-once，但把请求内容记下来回给微信——手机上要看得见它想干什么。
- 只回"最终助手正文"和"工具动作摘要"，不回思考过程、不回 reasonning 流。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# --- 定位 DSH（可用环境变量覆盖）---

DSH_NODE = os.environ.get("DSH_NODE") or "node"
DSH_HARNESS_ROOT = os.environ.get("DSH_HARNESS_ROOT", r"D:\deepseek-harness")
DSH_CLI = os.environ.get("DSH_CLI", "") or str(
    Path(DSH_HARNESS_ROOT) / "apps" / "cli" / "lib" / "bin.js"
)
DSH_PROFILE = os.environ.get("DSH_PROFILE", "acp")

ACP_PROTOCOL_VERSION = 1
DEFAULT_CWD = os.environ.get("DSH_WECHAT_CWD", r"D:\character_chat")
PROMPT_TIMEOUT_S = float(os.environ.get("DSH_WECHAT_PROMPT_TIMEOUT", "1800"))

ProgressSink = Callable[[str], Awaitable[None]]


class DshError(RuntimeError):
    """ACP 层的错误，带原始 code/message，便于原样报给用户。"""

    def __init__(self, code: int | str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class TurnResult:
    ok: bool
    text: str
    stop_reason: str = ""
    error: str = ""
    tool_actions: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def _log(msg: str) -> None:
    path = os.environ.get("DSH_WECHAT_LOG", r"D:\ling-muxue-logs\dsh_bridge.log")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


class AcpConnection:
    """一个 `dsh --profile acp` 子进程 + 它的 JSON-RPC 编解码。

    读循环把三类帧分开：response（按 id 唤醒等待者）、notification
    （session/update）、request（服务端反向请求，如 session/request_permission）。
    """

    def __init__(self, cwd: str = DEFAULT_CWD):
        self.cwd = cwd
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notify_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False
        self.initialized = False
        self.agent_info: dict[str, Any] = {}
        self.sessions: set[str] = set()     # 这条连接上已经 active 的会话

    # --- 生命周期 ---

    async def start(self) -> None:
        if not Path(DSH_CLI).exists():
            raise FileNotFoundError(f"DSH CLI 不存在：{DSH_CLI}")
        _log(f"spawn: {DSH_NODE} {DSH_CLI} --profile {DSH_PROFILE} (cwd={self.cwd})")
        self.proc = await asyncio.create_subprocess_exec(
            DSH_NODE,
            DSH_CLI,
            "--profile",
            DSH_PROFILE,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DSH_WECHAT_BRIDGE": "1"},
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """stderr 只进日志，绝不进协议。"""
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            _log("stderr: " + line.decode("utf-8", "replace").rstrip())

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                self._fail_all(EOFError("ACP 连接已关闭"))
                return
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                _log(f"非 JSON 行（丢弃）：{text[:200]}")
                continue
            await self._dispatch(frame)

    async def _dispatch(self, frame: dict) -> None:
        if "id" in frame and ("result" in frame or "error" in frame):
            fut = self._pending.pop(frame["id"], None)
            if fut is not None and not fut.done():
                if "error" in frame:
                    err = frame["error"] or {}
                    fut.set_exception(DshError(err.get("code", "error"), err.get("message", "")))
                else:
                    fut.set_result(frame.get("result"))
            return
        method = frame.get("method")
        if method is None:
            return
        if "id" in frame:
            await self._handle_server_request(frame)
            return
        handler = self._notify_handlers.get(method)
        if handler is not None:
            try:
                await handler(frame.get("params") or {})
            except Exception as e:  # 通知处理失败不能拖垮读循环
                _log(f"notify handler 失败 {method}: {e!r}")

    async def _handle_server_request(self, frame: dict) -> None:
        """服务端反向请求。权限请求按 allow-once 放行，但内容会经 update 回流。"""
        method = frame.get("method")
        rid = frame["id"]
        params = frame.get("params") or {}
        if method == "session/request_permission":
            _log(f"permission request: {json.dumps(params, ensure_ascii=False)[:400]}")
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
                }
            )
            return
        _log(f"未处理的 server request: {method}")
        await self._send_frame(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"client does not implement {method}"},
            }
        )

    def _fail_all(self, exc: BaseException) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _send_frame(self, frame: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise EOFError("ACP 进程未启动")
        self.proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 120.0) -> Any:
        if self._closed:
            raise EOFError("ACP 连接已关闭")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send_frame({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} 超时（{timeout}s）")

    def on_notification(self, method: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._notify_handlers[method] = handler

    async def initialize(self) -> dict:
        res = await self.request(
            "initialize",
            {"protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}},
            timeout=120.0,
        )
        self.initialized = True
        self.agent_info = (res or {}).get("agentInfo") or {}
        _log(f"initialized: {self.agent_info} caps={(res or {}).get('agentCapabilities')}")
        return res or {}

    async def new_session(self, cwd: str) -> str:
        res = await self.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=300.0)
        sid = res["sessionId"]
        self.sessions.add(sid)
        return sid

    def has_session(self, session_id: str) -> bool:
        """这条连接上这个会话是不是已经活着（活着就不能再 resume）。"""
        return session_id in self.sessions

    async def resume_session(self, session_id: str, cwd: str) -> dict:
        res = await self.request(
            "session/resume",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            timeout=300.0,
        )
        self.sessions.add(session_id)
        return res

    async def list_sessions(self, cwd: str) -> list[dict]:
        res = await self.request("session/list", {"cwd": cwd}, timeout=120.0)
        return (res or {}).get("sessions") or []

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                self.proc.kill()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()


# --- session/update 的投影：只取手机上要看的 ---

_TOOL_TITLE_KEYS = ("title", "name", "toolName", "kind", "rawInput")


def _summarize_tool_update(update: dict, max_output_chars: int = 400) -> Optional[str]:
    """把一次工具调用压成给微信看的进度行。

    ## 关于"流式"的真实边界（2026-09-12 查证，别再重新猜一遍）

    DSH 的 ACP 对工具**只发两帧**（`packages/acp/acp/src/updates.ts`）：
      - `tool_call`        → title + kind + rawInput + status='in_progress'
      - `tool_call_update` → status='completed'|'failed' + content（**完整输出**）

    **没有中间帧。** `tool/result` 是一次性提交的完整结果，DSH 的事件模型里
    不存在"命令逐行输出"这种提交点。所以：

      ❌ 拿不到 `pytest` 一行行往外滚的实时输出（协议层就没有）
      ✅ 拿得到每个工具**完成时**的完整输出 ← 这就是这里做的事

    相比之下 ACP 规范允许 `ToolCallUpdate.content` 做增量替换，那要看服务端愿不愿意发；
    DSH 不发。要真正的逐行，只能让 agent 自己把输出 tee 到文件、桥去 tail——
    那是另一套设计，不是改这里几行能到的。
    """
    kind = update.get("kind") or ""
    title = update.get("title") or ""
    status = update.get("status") or ""
    raw = update.get("rawInput")
    detail = ""
    if isinstance(raw, dict):
        detail = str(
            raw.get("command")
            or raw.get("file_path")
            or raw.get("path")
            or raw.get("pattern")
            or raw.get("description")
            or ""
        )
    label = title or kind or "tool"
    if detail:
        label = f"{label}: {detail}"
    label = re.sub(r"\s+", " ", label).strip()
    if len(label) > 120:
        label = label[:117] + "…"
    if not label:
        return None
    mark = {"completed": "✓", "failed": "✗"}.get(status, "…")
    line = f"{mark} {label}"

    # 完成/失败时附上工具输出（截断）——这样长活儿是"边跑边看"，
    # 而不是等最终结果那一条。失败时的输出尤其重要：错误原文要能看见。
    if status in ("completed", "failed"):
        out = _tool_output_text(update.get("content"))
        if out:
            if len(out) > max_output_chars:
                out = out[: max_output_chars - 1] + "…"
            line = f"{line}\n{out}"
    return line


def _tool_output_text(content: Any) -> str:
    """从 ToolCallUpdate.content 里取出可读文本。

    形状是 `[{type: 'content', content: <ContentBlock>}]`（见 updates.ts）。
    ContentBlock 可能是 {type:'text', text:...}，也可能是别的块——取不到就返回空，
    不编。
    """
    if not content:
        return ""
    items = content if isinstance(content, list) else [content]
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        inner = item.get("content", item)
        text = _content_text(inner)
        if text:
            chunks.append(text)
    joined = "\n".join(chunks).strip()
    return joined


def _content_text(content: Any) -> str:
    """从 ACP content 里取纯文本（可能是字符串、块、块数组）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(_content_text(c) for c in content)
    return str(content)


class DshBridge:
    """面向微信的会话管理器：微信 session → DSH 会话，跨重启可恢复。"""

    def __init__(self, cwd: str = DEFAULT_CWD, state_file: Optional[str] = None):
        self.cwd = cwd
        self.state_file = Path(
            state_file
            or os.environ.get(
                "DSH_WECHAT_STATE", r"D:\character_chat\data\dsh_bridge_state.json"
            )
        )
        self.conn: Optional[AcpConnection] = None
        self._lock = asyncio.Lock()          # 一个连接同时只跑一轮对话
        self._state: dict[str, Any] = self._load_state()
        self._live: dict[str, str] = {}      # chat_id -> 这条连接上已 active 的会话

    # --- state ---
    # 结构：chats[chat_id] = {"cwd": 当前工作目录, "sessions": {cwd: sessionId}}
    # 一个微信对话可以换目录，每个目录各自一条 DSH 会话——换回去时还能接回原来那条。

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            _log(f"state 写入失败：{e!r}")

    def _chat_rec(self, chat_id: str) -> dict[str, Any]:
        chats = self._state.setdefault("chats", {})
        rec = chats.get(chat_id)
        if not isinstance(rec, dict):
            rec = {}
            chats[chat_id] = rec
        # 兼容旧结构：{"sessionId": ...} 没有 cwd
        if "sessions" not in rec:
            old = rec.get("sessionId")
            rec["sessions"] = {rec.get("cwd") or self.cwd: old} if old else {}
        rec.setdefault("cwd", self.cwd)
        return rec

    def cwd_of(self, chat_id: str) -> str:
        return self._chat_rec(chat_id)["cwd"]

    def set_cwd(self, chat_id: str, cwd: str) -> None:
        self._chat_rec(chat_id)["cwd"] = cwd
        self._chat_rec(chat_id)["updatedAt"] = time.time()
        self._save_state()

    def dsh_session_of(self, chat_id: str) -> Optional[str]:
        rec = self._chat_rec(chat_id)
        return (rec.get("sessions") or {}).get(rec["cwd"])

    def bind(self, chat_id: str, session_id: str, cwd: Optional[str] = None) -> None:
        rec = self._chat_rec(chat_id)
        target = cwd or rec["cwd"]
        rec.setdefault("sessions", {})[target] = session_id
        rec["cwd"] = target
        rec["updatedAt"] = time.time()
        self._save_state()

    def unbind(self, chat_id: str) -> None:
        chats = self._state.get("chats") or {}
        chats.pop(chat_id, None)
        self._state["chats"] = chats
        self._save_state()

    # --- connection ---

    async def _ensure_conn(self, cwd: Optional[str] = None) -> AcpConnection:
        cwd = cwd or self.cwd
        if (
            self.conn is not None
            and self.conn.cwd == cwd
            and self.conn.proc is not None
            and self.conn.proc.returncode is None
        ):
            return self.conn
        if self.conn is not None:
            _log(f"切换工作目录 {self.conn.cwd} → {cwd}，换一个 ACP 进程")
            await self.conn.close()
        conn = AcpConnection(cwd)
        await conn.start()
        await conn.initialize()
        self.conn = conn
        self._live.clear()   # 新连接上没有任何 active 会话，下次要走 resume
        return conn

    async def _ensure_session(self, chat_id: str, conn: AcpConnection) -> tuple[str, str]:
        """返回 (sessionId, 说明)。

        顺序很重要：先看这条连接上是不是已经活着一个本 chat 的会话——ACP 对
        "已经 active 的会话" 再 resume 会报 -32602，之前就是栽在这里：明明同一个
        ACP 进程里会话好好的，却每次都去 resume，失败后又新开一条，多轮全断。
        """
        cwd = self.cwd_of(chat_id)
        bound = self.dsh_session_of(chat_id)
        own = self._live.get(chat_id)
        if bound and own == (cwd, bound) and conn.has_session(bound):
            return bound, "live"
        if bound:
            try:
                await conn.resume_session(bound, cwd)
                self._live[chat_id] = (cwd, bound)
                return bound, "resumed"
            except DshError as e:
                _log(f"resume 失败（{e}），在 {cwd} 新开一条会话")
        sid = await conn.new_session(cwd)
        self.bind(chat_id, sid, cwd)
        self._live[chat_id] = (cwd, sid)
        return sid, "new"

    # --- 主入口 ---

    async def run_turn(
        self,
        chat_id: str,
        text: str,
        on_progress: Optional[ProgressSink] = None,
    ) -> TurnResult:
        started = time.time()
        async with self._lock:
            cwd = self.cwd_of(chat_id)
            try:
                conn = await self._ensure_conn(cwd)
                sid, how = await self._ensure_session(chat_id, conn)
            except Exception as e:
                return TurnResult(False, "", error=f"ACP 启动失败：{e!r}", duration_s=time.time() - started)

            answer_chunks: list[str] = []
            tool_actions: list[str] = []
            last_tool_mark: dict[str, str] = {}
            notified: list[str] = []

            async def on_update(params: dict) -> None:
                update = params.get("update") or {}
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    piece = _content_text(update.get("content"))
                    if piece:
                        answer_chunks.append(piece)
                    return
                if kind in ("tool_call", "tool_call_update"):
                    line = _summarize_tool_update(update)
                    if not line:
                        return
                    tool_id = update.get("toolCallId") or line
                    # 同一工具的状态变化各报一次（避免刷屏）
                    status = update.get("status") or ""
                    if last_tool_mark.get(tool_id) == status:
                        return
                    last_tool_mark[tool_id] = status
                    if status in ("completed", "failed") or not status:
                        tool_actions.append(line)
                    if on_progress is not None:
                        # 手机上看"实时"，靠的就是这一步：工具**刚开始**就推一条，
                        # 而不是等它跑完才说。ACP 不流式给你命令的逐行输出，
                        # 所以能给的最细粒度就是"开始了 / 结束了 / 失败了"。
                        notified.append(line)
                        await on_progress(line)
                    return
                # thoughts / available_commands / current_mode 等一律不打扰手机
                return

            conn.on_notification("session/update", on_update)
            try:
                res = await asyncio.wait_for(
                    conn.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                                 timeout=PROMPT_TIMEOUT_S),
                    timeout=PROMPT_TIMEOUT_S + 30,
                )
                stop_reason = (res or {}).get("stopReason") or ""
            except (TimeoutError, asyncio.TimeoutError):
                return TurnResult(False, "", error=f"超时（{PROMPT_TIMEOUT_S:.0f}s）",
                                  tool_actions=tool_actions, duration_s=time.time() - started)
            except DshError as e:
                return TurnResult(False, "", error=f"{e.code}: {e.message}",
                                  tool_actions=tool_actions, duration_s=time.time() - started)
            except Exception as e:
                return TurnResult(False, "", error=f"{e!r}",
                                  tool_actions=tool_actions, duration_s=time.time() - started)

            text_out = "".join(answer_chunks).strip()
            return TurnResult(
                ok=stop_reason not in ("refusal", "cancelled") and bool(text_out or stop_reason),
                text=text_out,
                stop_reason=stop_reason,
                tool_actions=tool_actions,
                duration_s=time.time() - started,
            )

    async def new_chat_session(self, chat_id: str) -> str:
        """显式开一条新会话（微信发 /new 时）。"""
        async with self._lock:
            cwd = self.cwd_of(chat_id)
            conn = await self._ensure_conn(cwd)
            sid = await conn.new_session(cwd)
            self.bind(chat_id, sid, cwd)
            self._live[chat_id] = (cwd, sid)
            return sid

    async def switch_cwd(self, chat_id: str, cwd: str) -> tuple[str, str]:
        """换工作目录。该目录若已有会话就接回，没有就等下一轮新建。

        返回 (cwd, sessionId 或 "")。DSH 的会话在创建时钉死 workspace，
        所以换目录 = 换会话，不能原地改。
        """
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            raise DshError("invalid-cwd", f"要给绝对路径：{cwd}")
        if not path.is_dir():
            raise DshError("invalid-cwd", f"目录不存在：{path}")
        resolved = str(path.resolve())
        async with self._lock:
            previous = self.cwd_of(chat_id)
            self.set_cwd(chat_id, resolved)
            self._live.pop(chat_id, None)
            # 旧目录的会话留在 state 里，换回去还能接上
            existing = (self._chat_rec(chat_id).get("sessions") or {}).get(resolved) or ""
            if existing:
                try:
                    conn = await self._ensure_conn(resolved)
                    await conn.resume_session(existing, resolved)
                    self._live[chat_id] = (resolved, existing)
                except DshError as e:
                    _log(f"切到 {resolved} 后 resume 失败（{e}），下一轮新开")
                    existing = ""
            _log(f"chat {chat_id[:12]}… 工作目录 {previous} → {resolved}")
            return resolved, existing

    async def list_recent(self, chat_id: str) -> list[dict]:
        async with self._lock:
            cwd = self.cwd_of(chat_id)
            conn = await self._ensure_conn(cwd)
            return await conn.list_sessions(cwd)

    async def resume(self, chat_id: str, session_id: str) -> str:
        async with self._lock:
            cwd = self.cwd_of(chat_id)
            conn = await self._ensure_conn(cwd)
            sessions = await conn.list_sessions(cwd)
            known = [s.get("sessionId", "") for s in sessions]
            match = next((k for k in known if k == session_id), None) or next(
                (k for k in known if k.startswith(session_id)), None
            )
            if match is None:
                raise DshError("not-found", f"在 {cwd} 下没找到会话 {session_id}")
            await conn.resume_session(match, cwd)
            self.bind(chat_id, match, cwd)
            self._live[chat_id] = (cwd, match)
            return match

    async def shutdown(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
            self._live.clear()
