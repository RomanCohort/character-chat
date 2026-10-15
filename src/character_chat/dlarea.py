# -*- coding: utf-8 -*-
"""资源存放点（/dl/）的密钥门。

照 torus.py 那套抄的：自己的密钥库、签名 cookie、一个只给 nginx
`auth_request` 用的 /gate。文件本身仍然由 nginx 直发（不走 app），
所以大文件不经过这个 1 核机器中转 —— 只是"进门先验票"。

为什么密钥库跟 /ling/ 分开：那套是给角色对话用的，这套是给下载用的。
分开你才能单独发"只下载、不能玩角色"的密钥（slm / torus / cc 也都是各管各的）。

命令行（跟 guestchar.py 一样，直接跑文件就行）：
    venv/bin/python src/character_chat/dlarea.py mint "给谁"
    venv/bin/python src/character_chat/dlarea.py list
    venv/bin/python src/character_chat/dlarea.py revoke <哈希前缀>
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response

KEY_FILE = os.environ.get("DL_KEY_FILE", "/var/lib/ling/dl_keys.json")
SECRET_FILE = os.environ.get("DL_SECRET_FILE", "/var/lib/ling/dl_secret")
COOKIE = "dl_sess"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"       # 去掉易看错的 I O 0 1

router = APIRouter(prefix="/api/dl")
_lock = threading.Lock()


def _load_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def khash(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def mint(label: str) -> dict:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(24))
    key = "DL-" + "-".join(raw[i:i + 6] for i in range(0, 24, 6))
    rec = {"label": (label or "").strip()[:40] or "未命名",
           "created": int(time.time()), "enabled": True, "downloads": 0}
    with _lock:
        d = _load_json(KEY_FILE, {})
        d[khash(key)] = rec
        _save_json(KEY_FILE, d)
    return {"key": key, "record": rec}


def _secret() -> bytes:
    if not os.path.exists(SECRET_FILE):
        with io.open(SECRET_FILE, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
    with io.open(SECRET_FILE, "rb") as fh:
        return fh.read()


def _sign(h: str) -> str:
    return hmac.new(_secret(), h.encode(), hashlib.sha256).hexdigest()[:40]


def _check_key(key: str):
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


@router.post("/login")
def login(response: Response, key: str = ""):
    h, rec = _check_key(key)
    if not h:
        raise HTTPException(401, "密钥不对。")
    response.set_cookie(COOKIE, "%s.%s" % (h, _sign(h)), httponly=True,
                        samesite="lax", secure=True, max_age=3650 * 86400)
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
    return {"ok": True, "label": rec.get("label"), "id": h[:12]}


@router.get("/gate")
def gate(request: Request):
    """只给 nginx 的 auth_request 用（nginx 不对外暴露这条）。

    nginx 先问这里，200 才把 /dl/ 下面的字节放出去。所以文件仍然由 nginx
    直接发，不经过 app —— 只是"进门先验票"。
    """
    if not _authed(request):
        raise HTTPException(401, "先输入密钥。")
    return {"ok": True}


def cli(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "list":
        d = _load_json(KEY_FILE, {})
        for h, rec in d.items():
            print("%s  %-18s enabled=%s downloads=%s"
                  % (h[:12], rec.get("label"), rec.get("enabled", True),
                     rec.get("downloads", 0)))
        print("共 %d 把" % len(d))
        return 0
    if argv[0] == "mint":
        print(mint(argv[1] if len(argv) > 1 else "未命名")["key"])
        return 0
    if argv[0] == "revoke":
        pre = argv[1]
        with _lock:
            d = _load_json(KEY_FILE, {})
            hit = [h for h in list(d) if h.startswith(pre)]
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
