"""微信出站消息：直接调 ilink bot sendmessage，不经 bridge 进程。

凭据从 bridge 的 state dir 读（token / baseUrl / context_token）。
与 D:\\ling-muxue-daemon\\weixin_sender.py 同一套协议，这里只做两件事：
长回复分块、context_token 过期重取。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

WEIXIN_STATE_DIR = Path(
    os.environ.get("WEIXIN_MCP_STATE_DIR", "").strip() or (Path.home() / ".weixin-mcp")
) / "weixin"

ILINK_BASE_URL_DEFAULT = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 3)  # 2.4.3
CHANNEL_VERSION = "2.4.3-standalone"
BOT_AGENT = "WeixinMCPServer"

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TYPE_TEXT = 1

SEND_TIMEOUT = 15
SEND_MAX_RETRIES = 3
CHUNK_CHARS = int(os.environ.get("DSH_WECHAT_CHUNK_CHARS", "1800"))

LOG_FILE = os.environ.get("DSH_WECHAT_LOG", r"D:\ling-muxue-logs\dsh_bridge.log")


def _log(msg: str) -> None:
    try:
        p = Path(LOG_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _account_ids() -> list[str]:
    p = WEIXIN_STATE_DIR / "accounts.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, str) and x.strip()]
    except Exception:
        return []


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def resolve_target(to_user_id: Optional[str] = None) -> dict:
    """解析发送目标。

    账号选择：`accounts.json` 可能同时躺着几个账号——重新扫码后旧的死账号还在里面
    （bridge 的 `clearStaleAccountsForUserId` 没清掉它），而它取的是**第一个**。
    之前就栽在这：发送一直挑旧账号，永远 -14。所以这里按 `savedAt` 取最新的那个；
    另一个判据是 `accounts.json` 末尾 = 最近注册（bridge 自己的 `promptAccountSelection` 也这么认）。
    """
    ids = _account_ids()
    if not ids:
        raise RuntimeError(f"{WEIXIN_STATE_DIR} 下没有已登录的微信账号——先扫码登录 bridge")

    forced = os.environ.get("WEIXIN_ACCOUNT", "").strip()
    if forced:
        account_id = forced
    else:
        def saved_at(aid: str) -> str:
            return str(_load_json(WEIXIN_STATE_DIR / "accounts" / f"{aid}.json").get("savedAt") or "")

        account_id = max(ids, key=lambda a: (saved_at(a), ids.index(a)))

    acct = _load_json(WEIXIN_STATE_DIR / "accounts" / f"{account_id}.json")
    token = str(acct.get("token") or "").strip()
    base_url = str(acct.get("baseUrl") or "").strip() or ILINK_BASE_URL_DEFAULT
    if not token:
        raise RuntimeError(f"账号 {account_id} 没有 token")

    tokens = _load_json(WEIXIN_STATE_DIR / "accounts" / f"{account_id}.context-tokens.json")
    if not tokens:
        raise RuntimeError(
            f"账号 {account_id} 还没有 context-tokens——"
            f"先给这个 bot 发一条微信消息（那一下会把窗口和 token 带回来）"
        )
    to = to_user_id if to_user_id in tokens else next(iter(tokens.keys()))
    return {
        "account_id": account_id,
        "token": token,
        "base_url": base_url,
        "to_user_id": to,
        "context_token": tokens.get(to, ""),
    }


def _build_body(to: str, text: str, context_token: str) -> str:
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": "dsh-wechat-" + _b64(os.urandom(9)),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT},
    }
    if context_token:
        payload["msg"]["context_token"] = context_token
    return json.dumps(payload, ensure_ascii=False)


def _post(base_url: str, token: str, body: str, endpoint: str = "ilink/bot/sendmessage") -> tuple[bool, str]:
    """发一个请求，**并且看响应体里的 errcode/ret**。

    ilink 的失败是 HTTP 200 + JSON 里带错误码的形态：
        {"errcode":-14,"errmsg":"session timeout"}
    只看"HTTP 没报错"就会把这种当成功——之前 20 条消息全部沉在这儿，
    日志里却写着「发送成功」。所以这里必须解析响应体。
    """
    url = base_url.rstrip("/") + "/" + endpoint
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _b64(str(random.randint(0, 0xFFFFFFFF)).encode("utf-8")),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        return False, f"HTTP {e.code}: {raw}"
    except Exception as e:
        return False, str(e)

    try:
        data = json.loads(raw)
    except Exception:
        return True, raw  # 不是 JSON（比如空体），当成功——没有错误码可判
    if isinstance(data, dict):
        code = data.get("errcode", data.get("ret"))
        if code not in (0, None):
            return False, f"errcode={code} errmsg={data.get('errmsg')!r}"
    return True, raw


def _err_code(detail: str) -> Optional[str]:
    """从 _post 的错误串里抠出错误码，供重试判断。"""
    m = re.search(r"errcode=(-?\d+)|ret=(-?\d+)", detail or "")
    if m is None:
        return None
    return m.group(1) or m.group(2)


def _fingerprint(token: str) -> str:
    """token 指纹：只留 sha1 前 8 位。

    诊断用——需要在日志里区分"换了 token"和"同一个 token 反复失败"，
    但不能把凭据本身写进日志文件。
    """
    return hashlib.sha1((token or "").encode("utf-8")).hexdigest()[:8]


def _reload_token(account_id: str, to: str, current: str) -> str:
    """从磁盘重取 context_token。

    bridge 每收到一条消息就会刷新它；任务跑几分钟期间它可能已经变过。
    发送前 + 每次重试前都重取，避免拿一个已经作废的旧值反复撞。
    """
    fresh = _load_json(WEIXIN_STATE_DIR / "accounts" / f"{account_id}.context-tokens.json")
    got = fresh.get(to)
    return str(got) if got else current


# --- "正在输入" 指示器 ---
# 手机上等待时间长了要靠这个。协议：getconfig 拿 typing_ticket，再每 5 秒 sendtyping(1)，
# 结束时 sendtyping(2) 取消。拿不到 ticket 就静默降级——不影响正文发送。

TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2
TYPING_REFRESH_S = 5.0

_ticket_cache: dict[str, tuple[str, float]] = {}


def _fetch_typing_ticket(to: str) -> Optional[str]:
    cached = _ticket_cache.get(to)
    if cached and time.time() - cached[1] < 60 * 60 * 23:
        return cached[0]
    info = resolve_target(to)
    body = json.dumps(
        {
            "ilink_user_id": to,
            "context_token": info["context_token"],
            "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT},
        },
        ensure_ascii=False,
    )
    ok, raw = _post(info["base_url"], info["token"], body, "ilink/bot/getconfig")
    if not ok:
        _log(f"取 typing_ticket 失败：{raw[:160]}")
        return None
    try:
        ticket = str(json.loads(raw).get("typing_ticket") or "").strip()
    except Exception:
        return None
    if ticket:
        _ticket_cache[to] = (ticket, time.time())
        return ticket
    return None


def _send_typing(to: str, status: int) -> None:
    ticket = _fetch_typing_ticket(to)
    if not ticket:
        return
    info = resolve_target(to)
    body = json.dumps(
        {
            "ilink_user_id": to,
            "typing_ticket": ticket,
            "status": status,
            "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT},
        },
        ensure_ascii=False,
    )
    _post(info["base_url"], info["token"], body, "ilink/bot/sendtyping")


class typing:
    """异步上下文管理器：进入即开始"正在输入"，退出即取消。

        async with sender.typing(to):
            ... 干活 ...
    """

    def __init__(self, to_user_id: Optional[str] = None):
        self.to = to_user_id
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "typing":
        to = self.to
        if to is None:
            try:
                to = resolve_target()["to_user_id"]
            except Exception:
                return self
        self.to = to
        self._task = asyncio.create_task(self._loop(to))
        return self

    async def _loop(self, to: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _send_typing, to, TYPING_STATUS_TYPING)
            while True:
                await asyncio.sleep(TYPING_REFRESH_S)
                await loop.run_in_executor(None, _send_typing, to, TYPING_STATUS_TYPING)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _log(f"typing 循环异常：{e!r}")

    async def __aexit__(self, *exc) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        if self.to:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _send_typing, self.to, TYPING_STATUS_CANCEL
                )
            except Exception:
                pass


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """按行切块，尽量不切在句子中间。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # 单行就超长：硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf.strip():
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def send_text_sync(text: str, to_user_id: Optional[str] = None) -> tuple[bool, str]:
    """同步发送（分块）。返回 (ok, detail)。"""
    chunks = chunk_text(text)
    if not chunks:
        return False, "空消息，不发"
    info = resolve_target(to_user_id)
    to, token, base_url = info["to_user_id"], info["token"], info["base_url"]
    # 发送前先重取一次：resolve_target 读到的可能已经是几分钟前的旧值。
    context_token = _reload_token(info["account_id"], to, info["context_token"])

    for idx, chunk in enumerate(chunks, start=1):
        body_prefix = f"({idx}/{len(chunks)})\n" if len(chunks) > 1 else ""
        ok = False
        detail = ""
        last_code: Optional[str] = None
        for attempt in range(1, SEND_MAX_RETRIES + 1):
            if attempt > 1:
                # 换 token 再试：正常情况下 -2 是 context_token 过期，重取能救。
                # 但 2026-09-12 23:30 那次连续 12 条全 -2 'prepare failed'，
                # 当时旧代码**也**在每次重试前重取 token，重取了照样失败——
                # 说明那次不是 token 陈旧，是平台侧 prepare 没恢复（原因至今未知）。
                # 这里仍然重取（成本低、覆盖常见情况），真正的改动是**拉开重试间隔**：
                # 旧代码是每 0.5s 硬撞三次，撞不出结果；现在退避到 0.5/1/2s。
                before = context_token
                context_token = _reload_token(info["account_id"], to, context_token)
                backoff = min(0.5 * (2 ** (attempt - 2)), 4.0)
                _log(
                    f"重试 chunk={idx}/{len(chunks)} 第 {attempt}/{SEND_MAX_RETRIES} 次"
                    f"（上次 errcode={last_code}，token {_fingerprint(before)}"
                    f"->{_fingerprint(context_token)}，等 {backoff:.1f}s）"
                )
                time.sleep(backoff)

            body = _build_body(to, body_prefix + chunk, context_token)
            ok, detail = _post(base_url, token, body)
            if ok:
                break
            last_code = _err_code(detail)
            _log(
                f"发送失败 chunk={idx}/{len(chunks)} 第 {attempt}/{SEND_MAX_RETRIES} 次: "
                f"{detail[:200]} | token={_fingerprint(context_token)}"
            )
        if not ok:
            return False, detail
    _log(f"发送成功 {len(chunks)} 块，共 {sum(len(c) for c in chunks)} 字")
    return True, "ok"


async def send_text(text: str, to_user_id: Optional[str] = None) -> tuple[bool, str]:
    """异步包装：urllib 是阻塞的，丢线程池，别卡住 event loop。"""
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: send_text_sync(text, to_user_id)
    )
