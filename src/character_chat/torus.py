"""TorusFold 密钥分发层（服务器侧，FastAPI 路由）。

拓扑：浏览器 → nginx → (本模块只做门) / (数据流直连 127.0.0.1:8010)
      8010 = 本机开发机 serve.py:8877 的 ssh -R 反向隧道

三条硬约束（用户 2026-10-13 明确）：
  · 页面人人可看，**提交才要密钥**（cap=0 不限额度）
  · **并发 1**：一个作业可能跑几小时到几天，第二个提交直接拒绝，不排队
  · **绝不掐死任务**：不对上游设读超时、不对运行中的 job 做 TTL、
    释放槽位靠"向后端对账"，绝不重启 serve.py 或断开任何连接

其它必须守住的（来自开发机 agent 的事实表）：
  · 长度双边界 30..2100 —— 长度校验在 serve.py 是**空的**（4 nt 也会启动），
    而下界 `<30 nt` 会静默返回**非环状结构且无警告**，比报错更糟
  · serve.py 会把未知键**丢弃并写进 notes**，不报错 → 所以白名单校验必须在这里做
  · `/api/*` 里的危险面由 nginx 挡；这里只负责"能不能提交"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request, Response

# ── 配置 ─────────────────────────────────────────────────────────────
UPSTREAM = os.environ.get("TORUS_UPSTREAM", "http://127.0.0.1:8010")
KEY_FILE = os.environ.get("TORUS_KEY_FILE", "/var/lib/ling/torus_keys.json")
SECRET_FILE = os.environ.get("TORUS_SECRET_FILE", "/var/lib/ling/torus_secret")
#: 槽位记账落盘。**为什么必须落盘**：一个作业可能跑几小时，而 app 会因为
#: 部署/改代码重启；不落盘的话重启一次，正在跑的作业就变成"没人认领"，
#: 提交者也就再也结束不了它。
BUSY_FILE = os.environ.get("TORUS_BUSY_FILE", "/var/lib/ling/torus_busy.json")
COOKIE = "torus_sess"

MIN_LEN = int(os.environ.get("TORUS_MIN_LEN", "30"))
MAX_LEN = int(os.environ.get("TORUS_MAX_LEN", "2100"))

#: 短 JSON 调用（/api/predict 与 /api/current 都是立刻返回的），
#: 这里给的是**连接**超时；长作业本身不经过本模块，所以不存在"读超时掐死作业"。
CONNECT_TIMEOUT = float(os.environ.get("TORUS_CONNECT_TIMEOUT", "15"))

#: 旋钮白名单：name -> (kind, min, max)。来自 GET /api/schema（8 组 30 个）。
from torus_schema import KNOBS  # noqa: E402  单独一份，便于随 schema 更新

_lock = threading.Lock()
_busy = {"job_id": None, "key_hash": None, "started": 0.0, "length": 0,
         "stop_requested": False}


def _persist_busy() -> None:
    """落盘当前槽位（调用者需已持锁）。写失败不能让请求失败 —— 记不上账也不能 500。"""
    try:
        _save_json(BUSY_FILE, _busy)
    except OSError:
        pass


def _restore_busy() -> None:
    d = _load_json(BUSY_FILE, None)
    if isinstance(d, dict):
        for k in _busy:
            if k in d:
                _busy[k] = d[k]

router = APIRouter(prefix="/api/torus")


# ── 密钥（独立一份：不让 /slm/ 的密钥顺带能占 GPU）──────────────────
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 去掉易看错的 I O 0 1


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_restore_busy()          # 必须在 _load_json 定义之后（重启后接着算旧账）


def khash(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def mint(label: str) -> dict:
    """发一把密钥。明文只在这一刻返回，之后只留哈希。cap=0 = 不限额度。"""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(24))
    key = "TF-" + "-".join(raw[i:i + 6] for i in range(0, 24, 6))
    rec = {"label": (label or "").strip()[:40] or "未命名",
           "created": int(time.time()), "enabled": True,
           "calls_per_day": 0, "used": 0}
    with _lock:
        d = _load_json(KEY_FILE, {})
        d[khash(key)] = rec
        _save_json(KEY_FILE, d)
    return {"key": key, "record": rec}


def _secret() -> bytes:
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
    with open(SECRET_FILE, "rb") as fh:
        return fh.read()


def _sign(h: str) -> str:
    return hmac.new(_secret(), h.encode(), hashlib.sha256).hexdigest()[:40]


def _check_key(key: str):
    """返回 (hash, record)；不存在返回 (None, None)。"""
    h = khash(key)
    rec = _load_json(KEY_FILE, {}).get(h)
    if not rec or not rec.get("enabled", True):
        return None, None
    return h, rec


def _authed(request: Request):
    raw = request.cookies.get(COOKIE) or ""
    if "." not in raw:
        return None
    h, sig = raw.split(".", 1)
    if not hmac.compare_digest(sig, _sign(h)):
        return None
    rec = _load_json(KEY_FILE, {}).get(h)
    if not rec or not rec.get("enabled", True):
        return None
    return h


# ── 上游（只做短 JSON 调用；长数据流由 nginx 直连 8010）─────────────
def _upstream(path: str, payload=None, method="GET"):
    url = UPSTREAM.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8")) if body else {}


def backend_busy() -> dict:
    """问后端"现在有没有作业在跑"。这是**唯一的真相来源** ——
    我们不靠浏览器回报、也不靠计时，所以浏览器关掉不会把槽位卡住，
    也不会因为超时把长作业判成结束。"""
    return backend_state()


#: 状态缓存。页面是公开的、每个访客都在轮询，而这台机器只有 1 核 —— 不能让
#: 访客数变成对开发机的请求数。缓存 3 秒，上游压力与访客数无关。
_STATE_CACHE = {"at": 0.0, "data": None}
STATE_TTL = float(os.environ.get("TORUS_STATE_TTL", "3"))


def backend_state(force=False) -> dict:
    """后端全量状态（health + status），带 3 秒缓存。

    进度只在这里读 —— 页面要显示"跑到第几级"，但 /api/status 有 6KB，
    不能每个访客每次轮询都去拉一遍。
    """
    now = time.time()
    cached = _STATE_CACHE["data"]
    if cached is not None and not force and (now - _STATE_CACHE["at"]) < STATE_TTL:
        return cached
    try:
        health = _upstream("/api/health")
    except Exception as exc:                     # noqa: BLE001
        data = {"reachable": False, "running": False, "job_status": "",
                "detail": "%s: %s" % (type(exc).__name__, exc)}
    else:
        status = str(health.get("job_status") or "")
        data = {"reachable": True, "running": status == "running",
                "job_status": status, "detail": "",
                "progress": None, "level": None, "level_name": "",
                "message": "", "elapsed": None, "job_id": None}
        if status == "running":
            try:
                st = _upstream("/api/status")
            except Exception:                    # noqa: BLE001
                pass
            else:
                data["progress"] = st.get("progress")
                data["level"] = st.get("current_level")
                data["level_name"] = st.get("level_name") or ""
                data["message"] = st.get("message") or ""
                data["elapsed"] = st.get("elapsed")
                data["job_id"] = st.get("job_id")
    _STATE_CACHE["data"] = data
    _STATE_CACHE["at"] = now
    return data


def _reconcile_locked() -> None:
    """释放已经结束的槽位。**只清我们自己的记账，不碰任何进程。**"""
    st = backend_busy()
    if not st.get("reachable"):
        return                                # 隧道断了？那什么都不知道，别乱改账
    if st.get("running"):
        # 重启后认领：并发只有 1，跑着的那个就是"当前作业"。key_hash 认不回来
        # （我们不存明文/明文哈希的关联），所以这一条**谁也不许结束** —— 页面会
        # 直说"这不是你这次提交的"，而不是假装能停。
        if _busy["job_id"] is None:
            _busy.update({"job_id": st.get("job_id"), "key_hash": None,
                          "started": time.time() - float(st.get("elapsed") or 0.0),
                          "length": 0, "stop_requested": False})
            _persist_busy()
        return
    if _busy["job_id"] is not None:
        _busy.update({"job_id": None, "key_hash": None, "started": 0.0,
                      "length": 0, "stop_requested": False})
        _persist_busy()


# ── 校验 ─────────────────────────────────────────────────────────────
def normalize_sequence(seq: str) -> str:
    s = "".join(str(seq or "").split()).upper().replace("T", "U")
    if not s:
        raise HTTPException(400, "sequence 不能为空。")
    bad = sorted(set(s) - set("ACGNU"))
    if bad:
        raise HTTPException(400, "sequence 里出现了非法字符：%s（只允许 A/C/G/N/U，T 会自动转 U）"
                            % " ".join(bad[:8]))
    if len(s) < MIN_LEN:
        # 下界不是"洁癖"：<30 nt 时管线会返回**非环状结构且不报警**，
        # 也就是悄悄给一个错答案 —— 这比直接拒绝更糟。
        raise HTTPException(400, "序列太短：%d nt，下限 %d nt。"
                                 "短于这个长度管线会返回非环状结构且不给警告。" % (len(s), MIN_LEN))
    if len(s) > MAX_LEN:
        raise HTTPException(400, "序列太长：%d nt，上限 %d nt。" % (len(s), MAX_LEN))
    return s


def validate_params(params: dict) -> dict:
    """只放行白名单里的旋钮，并检查取值域。

    serve.py 对未知键是"丢弃 + 写进 notes"，不报错 —— 那样用户会以为参数生效了。
    所以在这里就把错的挡住。
    """
    out = {}
    unknown = []
    for k, v in (params or {}).items():
        spec = KNOBS.get(k)
        if spec is None:
            unknown.append(k)
            continue
        kind, lo, hi = spec
        try:
            if kind == "int":
                v = int(v)
            elif kind == "float":
                v = float(v)
            elif kind == "bool":
                v = bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            else:
                v = str(v)
        except (TypeError, ValueError):
            raise HTTPException(400, "参数 %s 类型不对（应为 %s）。" % (k, kind))
        if kind in ("int", "float"):
            if lo is not None and v < lo:
                raise HTTPException(400, "参数 %s = %s 小于下限 %s。" % (k, v, lo))
            if hi is not None and v > hi:
                raise HTTPException(400, "参数 %s = %s 超过上限 %s。" % (k, v, hi))
        out[k] = v
    if unknown:
        raise HTTPException(400, "未知参数：%s。可用参数见 /torus/ 页面的参数表。" % "、".join(unknown[:10]))
    return out


# ── 路由 ─────────────────────────────────────────────────────────────
@router.post("/login")
def login(response: Response, key: str = ""):
    h, rec = _check_key(key)
    if not h:
        raise HTTPException(401, "密钥不对。")
    response.set_cookie(COOKIE, "%s.%s" % (h, _sign(h)), httponly=True, samesite="lax",
                        secure=True, max_age=90 * 86400)
    return {"ok": True, "label": rec.get("label")}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    h = _authed(request)
    if not h:
        return {"ok": False}
    rec = _load_json(KEY_FILE, {}).get(h) or {}
    with _lock:
        _reconcile_locked()
        busy = dict(_busy)
    return {"ok": True, "label": rec.get("label"), "unlimited": True,
            "busy": busy["job_id"] is not None, "busy_length": busy["length"],
            "busy_since": busy["started"], "bounds": [MIN_LEN, MAX_LEN],
            # 「这次提交是我自己的吗」—— 页面靠它决定要不要显示「结束任务」。
            # 并发只有 1，所以「当前作业」必然是最新那个提交者的。
            "mine": bool(busy["job_id"] is not None and busy["key_hash"] == h),
            "stop_requested": busy["stop_requested"]}


@router.get("/state")
def state(request: Request):
    """**公开**：页面不登录也能看到后端在不在、忙不忙、有没有作业。"""
    st = backend_busy()
    with _lock:
        _reconcile_locked()
        busy = dict(_busy)
    return {"reachable": st.get("reachable"), "job_status": st.get("job_status"),
            "detail": st.get("detail", ""),
            "busy": busy["job_id"] is not None,
            "busy_length": busy["length"], "busy_since": busy["started"],
            "bounds": [MIN_LEN, MAX_LEN],
            "progress": st.get("progress"), "level": st.get("level"),
            "level_name": st.get("level_name", ""), "message": st.get("message", ""),
            "elapsed": st.get("elapsed"),
            "stop_requested": busy["stop_requested"]}


@router.get("/gate")
def gate(request: Request):
    """给 nginx 的 `auth_request` 用：SSE / 结果 / 结构这些**长连接或字节流**
    不经过本模块（否则 1 核机器要一直替几小时的流做中转），但必须有人验密钥。
    nginx 先问这里；200 才放行到 8010。

    注意：这条只在服务器内部被 nginx 调用，不对外暴露（nginx 不配 /gate 的对外路由）。
    """
    if not _authed(request):
        raise HTTPException(401, "先输入密钥。")
    return {"ok": True}


@router.post("/submit")
def submit(request: Request, payload: dict):
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    seq = normalize_sequence((payload or {}).get("sequence", ""))
    params = validate_params((payload or {}).get("params") or {})

    with _lock:
        _reconcile_locked()
        if _busy["job_id"] is not None:
            raise HTTPException(409, "已经有一个作业在跑（%d nt，已跑 %.1f 小时）。"
                                     "一个作业可能跑几小时到几天，等它结束再提交。"
                                % (_busy["length"] or 0,
                                   (time.time() - (_busy["started"] or time.time())) / 3600.0))
        st = backend_busy()
        if not st.get("reachable"):
            raise HTTPException(503, "开发机上的 TorusFold 服务连不上（隧道断了或服务没起）：%s"
                                % st.get("detail", ""))
        if st.get("running"):
            raise HTTPException(409, "后端已经有一个作业在跑（不在本平台的记账里）。等它结束再提交。")
        # 占位：**只写我们自己的记账**，不重启、不断连、不设超时
        try:
            res = _upstream("/api/predict", {"sequence": seq, **params}, method="POST")
        except urllib.error.HTTPError as exc:
            code = exc.code
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:                    # noqa: BLE001
                pass
            raise HTTPException(502 if code not in (409, 400) else code,
                                "后端拒绝了这次提交（HTTP %d）：%s" % (code, detail))
        except Exception as exc:                 # noqa: BLE001
            raise HTTPException(503, "提交到后端失败（%s: %s）。作业**没有**开始，可稍后重试。"
                                % (type(exc).__name__, exc))
        _busy.update({"job_id": res.get("job_id"), "key_hash": h,
                      "started": time.time(), "length": len(seq),
                      "stop_requested": False})
        _persist_busy()
        _STATE_CACHE["at"] = 0.0                 # 立刻反映"忙"，别让缓存骗页面 3 秒
    notes = [n for n in (res.get("notes") or [])
             if "sequence" not in str(n)]        # serve.py 会把必填的 sequence 误报成"未知参数"
    return {"status": "started", "job_id": res.get("job_id"), "length": len(seq),
            "params": res.get("params"), "notes": notes}


@router.post("/release")
def release(request: Request):
    """放弃等待：**只释放本站的槽位**，不碰远端作业。

    要真的停掉远端作业用 /stop（开发机上的 serve.py 有 /api/stop，"结束任务"走那条）。
    """
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    with _lock:
        st = backend_busy()
        if st.get("reachable") and st.get("running"):
            return {"ok": False, "still_running": True,
                    "note": "远端作业仍在跑。要真的停下它，用「结束任务」（会请开发机的 "
                            "serve.py 执行 /api/stop）；这里只放弃本站这一侧的等待。"}
        _busy.update({"job_id": None, "key_hash": None, "started": 0.0,
                      "length": 0, "stop_requested": False})
        _persist_busy()
    return {"ok": True, "still_running": False}


@router.post("/stop")
def stop(request: Request):
    """**真的结束这次作业** —— 只有提交者本人能按（并发 1，当前作业就是他的）。

    通道：本模块 → 隧道(127.0.0.1:8010) → 开发机上的 serve.py `POST /api/stop`。
    开发机是 Windows，服务器**反向连不进去**（119.185.221.138 的端口全 filtered），
    所以唯一的路就是这条隧道。端点已由开发机侧提供（实测 POST → 200
    {"ok": true, "stopped": ..., "reason": ...}）；万一哪天它不见了，
    下面会退化：说清楚没停成，而不是假装停了。
    """
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    with _lock:
        _reconcile_locked()
        if _busy["job_id"] is None:
            st0 = backend_busy()
            if st0.get("reachable") and not st0.get("running"):
                return {"ok": False, "still_running": False, "mine": False,
                        "note": "现在没有在跑的作业。"}
            raise HTTPException(403, "现在跑着的作业不是这次提交的，不能由你结束。")
        if _busy["key_hash"] != h:
            raise HTTPException(403, "现在跑着的作业不是这次提交的，不能由你结束。")
        if _busy["stop_requested"]:
            return {"ok": True, "still_running": True, "mine": True,
                    "note": "结束请求已经发过了，正在等开发机那边停下来。"}
        job_id = _busy["job_id"]
        _busy["stop_requested"] = True
        _persist_busy()
        try:
            _upstream("/api/stop", {"job_id": job_id}, method="POST")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                _busy["stop_requested"] = False
                _persist_busy()
                raise HTTPException(501,
                    "开发机上的 serve.py 没有响应 /api/stop（它本来有，可能被改动或降级了）。"
                    "服务器连不进那台机器，只能靠隧道请它自己停；这一侧不会做任何事"
                    "（不会重启、不会断连），作业会继续跑完。")
            _busy["stop_requested"] = False
            _persist_busy()
            raise HTTPException(502, "结束请求被开发机拒绝（HTTP %d）。" % exc.code)
        except Exception as exc:                 # noqa: BLE001
            _busy["stop_requested"] = False
            _persist_busy()
            raise HTTPException(503, "结束请求没能送到开发机（%s: %s）。作业还在跑。"
                                % (type(exc).__name__, exc))
    # 给上游几秒钟把作业收尾；判据仍然是**后端自己报的状态**，不是计时器。
    deadline = time.time() + 8.0
    while time.time() < deadline:
        time.sleep(0.7)
        st = backend_state(force=True)
        if st.get("reachable") and not st.get("running"):
            with _lock:
                _busy.update({"job_id": None, "key_hash": None, "started": 0.0,
                              "length": 0, "stop_requested": False})
                _persist_busy()
            return {"ok": True, "still_running": False, "mine": True,
                    "note": "作业已结束，槽位已释放。"}
    return {"ok": True, "still_running": True, "mine": True,
            "note": "结束请求已经送到开发机，作业正在收尾。刷新一下看状态。"}


def cli(argv=None):
    """python torus.py mint "标签"   /   python torus.py list"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "list":
        d = _load_json(KEY_FILE, {})
        for h, rec in d.items():
            print("%s  %-22s enabled=%s" % (h[:12], rec.get("label"), rec.get("enabled", True)))
        print("共 %d 把" % len(d))
        return 0
    if argv[0] == "mint":
        out = mint(argv[1] if len(argv) > 1 else "未命名")
        print(out["key"])
        return 0
    if argv[0] == "revoke":
        prefix = argv[1]
        with _lock:
            d = _load_json(KEY_FILE, {})
            hit = [h for h in list(d) if h.startswith(prefix)]
            for h in hit:
                d.pop(h)
            if hit:
                _save_json(KEY_FILE, d)
        print("吊销 %d 把" % len(hit))
        return 0
    print("用法: mint <标签> | list | revoke <哈希前缀>")
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
