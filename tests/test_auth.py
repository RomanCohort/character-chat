"""连接认证的测试。

这是安全边界：本机免登录那一条如果写错，等于把所有来源都当成本机放行；
会话签名如果写错，等于谁都能伪造身份。所以这里覆盖得细一些。
"""
from __future__ import annotations

import time

import pytest

from character_chat.wechat import auth


@pytest.fixture(autouse=True)
def _isolated_auth(tmp_path, monkeypatch):
    """把用户库和密钥指到临时目录，别碰真库。"""
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "auth_users.json")
    monkeypatch.setattr(auth, "SECRET_FILE", tmp_path / "auth_secret.key")


# --- 密码哈希 ---


def test_hash_is_salted_and_not_reversible():
    h1 = auth.hash_password("hunter2")
    h2 = auth.hash_password("hunter2")
    assert h1 != h2, "同一密码两次哈希必须不同（salt 必须随机）"
    assert "hunter2" not in h1
    assert h1.startswith("pbkdf2_sha256$")


def test_verify_accepts_correct_and_rejects_wrong():
    h = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h) is True
    assert auth.verify_password("correct hors", h) is False
    assert auth.verify_password("", h) is False


def test_verify_rejects_garbage_without_raising():
    for junk in ("", "not-a-hash", "pbkdf2_sha256$abc", "md5$1$x$y", "a$b$c$d$e"):
        assert auth.verify_password("x", junk) is False


def test_iterations_are_recorded_so_old_hashes_still_verify():
    old = auth.hash_password("pw", iterations=1000)
    assert old.split("$")[1] == "1000"
    assert auth.verify_password("pw", old) is True


# --- 用户库 ---


def test_create_user_then_check_credentials():
    auth.create_user("yaya", "s3cret-pw")
    assert auth.list_users() == ["yaya"]
    assert auth.check_credentials("yaya", "s3cret-pw") is True
    assert auth.check_credentials("yaya", "wrong") is False
    assert auth.check_credentials("nobody", "s3cret-pw") is False


def test_create_user_is_upsert_so_it_also_changes_password():
    auth.create_user("yaya", "old-pw")
    auth.create_user("yaya", "new-pw")
    assert auth.list_users() == ["yaya"]
    assert auth.check_credentials("yaya", "new-pw") is True
    assert auth.check_credentials("yaya", "old-pw") is False


def test_username_is_trimmed_and_password_length_enforced():
    auth.create_user("  yaya  ", "abcdef")
    assert auth.list_users() == ["yaya"]
    with pytest.raises(ValueError):
        auth.create_user("x", "short")
    with pytest.raises(ValueError):
        auth.create_user("", "abcdef")


def test_delete_user():
    auth.create_user("a", "abcdef")
    auth.create_user("b", "abcdef")
    assert auth.delete_user("a") is True
    assert auth.delete_user("a") is False
    assert auth.list_users() == ["b"]


# --- 会话 ---


def test_session_roundtrip():
    auth.create_user("yaya", "abcdef")
    value = auth.issue_session("yaya")
    assert auth.verify_session(value) == "yaya"


def test_session_rejects_tampering():
    auth.create_user("yaya", "abcdef")
    value = auth.issue_session("yaya")
    body, _, sig = value.rpartition(".")

    # 改签名
    assert auth.verify_session(f"{body}.{sig[:-2]}xx") is None
    # 改 payload（把用户名换掉）但保留原签名
    import base64
    import json

    forged = base64.urlsafe_b64encode(
        json.dumps({"u": "attacker", "iat": int(time.time()), "exp": int(time.time()) + 999}).encode()
    ).decode().rstrip("=")
    assert auth.verify_session(f"{forged}.{sig}") is None, "改 payload 必须验不过"
    # 垃圾
    assert auth.verify_session("") is None
    assert auth.verify_session("nodot") is None
    assert auth.verify_session("a.b.c") is None


def test_session_expiry(monkeypatch):
    auth.create_user("yaya", "abcdef")
    value = auth.issue_session("yaya")
    assert auth.verify_session(value) == "yaya"

    real_time = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real_time() + auth.SESSION_DAYS * 86400 + 10)
    assert auth.verify_session(value) is None, "过期会话必须失效"


def test_session_dies_when_user_deleted():
    """删了人，他手里的 cookie 不能还继续用。"""
    auth.create_user("yaya", "abcdef")
    value = auth.issue_session("yaya")
    assert auth.verify_session(value) == "yaya"
    auth.delete_user("yaya")
    assert auth.verify_session(value) is None


def test_two_logins_produce_different_tokens():
    auth.create_user("yaya", "abcdef")
    assert auth.issue_session("yaya") != auth.issue_session("yaya")


# --- 本机判定（最关键的一条） ---


def test_local_requires_both_loopback_client_and_host():
    # 真本机
    assert auth.is_local("127.0.0.1:8000", "127.0.0.1") is True
    assert auth.is_local("localhost:8000", "127.0.0.1") is True
    assert auth.is_local("[::1]:8000", "::1") is True
    assert auth.is_local("127.0.0.5:8000", "127.0.0.5") is True

    # 远程来源 + 伪造 Host: 不能算本机
    assert auth.is_local("127.0.0.1:8000", "10.66.200.82") is False, \
        "远程连接伪造 Host 为 loopback 时必须拒绝"

    # 本机来源 + 攻击者域名 Host（DNS rebinding）: 不能算本机
    assert auth.is_local("evil.example.com", "127.0.0.1") is False, \
        "DNS rebinding：Host 不是 loopback 时必须拒绝"

    # 局域网地址
    assert auth.is_local("10.66.200.82:8000", "10.66.200.82") is False

    # 缺失
    assert auth.is_local(None, "127.0.0.1") is False
    assert auth.is_local("127.0.0.1:8000", None) is False


def test_no_users_means_remote_is_locked_but_script_tells_you_how():
    assert auth.list_users() == []
    assert auth.check_credentials("anyone", "anything") is False
