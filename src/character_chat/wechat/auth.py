"""连接认证：账号密码 + 会话 cookie。

为什么需要它
------------
8000 服务原本对所有来源开放，而 `/chat` 带上 `*work` 就等于"在这台机器上执行命令"。
只要浏览器开着，任何网页都能跨域打进来（CORS 还是 `*`）。所以"能连上机器"这件事
本身必须有门槛。

两道门
------
1. **本机免登录。** 微信桥（`wechat-ai-code-bridge`）跑在同一台机器上，走 loopback
   调 `/chat`。它没有账号密码，也不该有——它已经在机器里了，再让它持一个密码只是
   多一个泄漏点。所以 loopback 来源直接放行。
2. **其它来源要登录。** 拿到会话 cookie 才放行，cookie 是 HMAC 签名的，带过期。

密码存储
--------
PBKDF2-HMAC-SHA256，每个用户独立 salt，迭代次数写在记录里（以后能加，老密码还能验）。
**不存明文，不存可逆哈希。** 用户库落在 `data/auth_users.json`，权限上属于凭据文件，
别提交、别外发。

会话
----
cookie 名 `dsh_session`，值是 `base64(payload).base64(hmac)`。签名密钥落在
`data/auth_secret.key`（首次运行自动生成，32 字节随机）。**删掉它 = 所有人被登出**，
这正好是"紧急踢人"的手段。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

# --- 参数 ---

# 迭代次数。20 万次在普通笔记本上约 100ms——登录时能接受，暴力破解代价高。
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
SESSION_DAYS = int(os.environ.get("DSH_AUTH_SESSION_DAYS", "30"))
COOKIE_NAME = "dsh_session"

_DATA_DIR = Path(os.environ.get("DSH_AUTH_DIR", str(Path(__file__).resolve().parents[3] / "data")))
USERS_FILE = _DATA_DIR / "auth_users.json"
SECRET_FILE = _DATA_DIR / "auth_secret.key"


# --- 用户库 ---


def _load_users() -> dict:
    try:
        raw = USERS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 库损坏时不能当成"没有用户"静默放行——那等于把门拆了。
        # 也不能当成"有用户"——那会把主人自己锁在外面。这里抛出去，让调用方看见。
        raise


def _save_users(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)  # 原子替换：写一半断电不会留下半个用户库
    try:
        os.chmod(USERS_FILE, 0o600)
    except Exception:
        pass  # Windows 上 chmod 语义有限，失败不算错


def hash_password(password: str, *, salt: Optional[bytes] = None,
                  iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`。"""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码。格式不对、算不出来都返回 False（不抛，避免把存在性暴露成 500）。"""
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def create_user(username: str, password: str) -> None:
    """建用户或改密码。用户名做基本清洗（去空白、限长）。"""
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 64:
        raise ValueError("用户名太长")
    if not password or len(password) < 6:
        raise ValueError("密码至少 6 位")
    users = _load_users()
    users[username] = {
        "password": hash_password(password),
        "created_at": int(time.time()),
    }
    _save_users(users)


def list_users() -> list[str]:
    return sorted(_load_users().keys())


def delete_user(username: str) -> bool:
    users = _load_users()
    if username not in users:
        return False
    del users[username]
    _save_users(users)
    return True


def check_credentials(username: str, password: str) -> bool:
    users = _load_users()
    rec = users.get((username or "").strip())
    if not isinstance(rec, dict):
        # 用户不存在也要跑一次哈希，别让响应时间泄漏"这个用户名存不存在"。
        hash_password(password or "", salt=b"0" * SALT_BYTES, iterations=1000)
        return False
    return verify_password(password, str(rec.get("password") or ""))


# --- 会话 ---


def _secret() -> bytes:
    """签名密钥。没有就生成一个，落盘。"""
    try:
        raw = SECRET_FILE.read_bytes()
        if len(raw) >= 32:
            return raw
    except FileNotFoundError:
        pass
    except Exception:
        pass
    key = secrets.token_bytes(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(key)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return key


def _sign(body: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(_secret(), body, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def issue_session(username: str) -> str:
    """发一个会话值：`<payload_b64>.<sig>`。"""
    payload = {
        "u": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_DAYS * 86400,
        "jti": secrets.token_hex(8),  # 让每次登录的 cookie 都不同，便于以后做单会话吊销
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{body}.{_sign(body.encode('ascii'))}"


def verify_session(value: str) -> Optional[str]:
    """校验会话值，返回用户名；无效或过期返回 None。"""
    if not value or "." not in value:
        return None
    body, _, sig = value.rpartition(".")
    if not body or not sig:
        return None
    if not hmac.compare_digest(_sign(body.encode("ascii")), sig):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    now = int(time.time())
    try:
        if int(payload["exp"]) <= now or int(payload["iat"]) > now + 60:
            return None
    except Exception:
        return None
    # 用户被删掉后，他手里的 cookie 要立刻失效——不能只等过期。
    if str(payload.get("u")) not in _load_users():
        return None
    return str(payload.get("u"))


# --- 来源判定 ---


def is_local(host: Optional[str], client_host: Optional[str]) -> bool:
    """是不是从本机来的。

    两个判据都要满足：
      - 连接来源（client_host）是回环地址
      - Host 头是回环地址（挡 DNS rebinding：攻击者页面把域名解析到 127.0.0.1
        时，Host 会是他的域名，这里就能识破）

    只看 client_host 不够——浏览器从本机发起时 client_host 也是 127.0.0.1，
    所以 Host 那一半是必需的。
    """
    def _loopback_only(addr: str) -> bool:
        a = (addr or "").strip().lower()
        if a in ("localhost", "::1", "[::1]"):
            return True
        # 允许 127.x.x.x 整个段
        return a.startswith("127.")

    def _host_only(h: str) -> bool:
        # Host 可能带端口，剥掉
        h = (h or "").strip().lower()
        if h.startswith("["):  # [::1]:8000
            return h.startswith("[::1]")
        return _loopback_only(h.rsplit(":", 1)[0] if ":" in h else h)

    return _loopback_only(client_host or "") and _host_only(host or "")
