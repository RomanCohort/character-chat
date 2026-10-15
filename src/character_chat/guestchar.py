#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥制的 character_chat：**一把密钥 = 一个人 = 一张卡 + 一个库 + 一套心理参数**。

和 /slm/、TorusFold 那两处同一个规矩：页面人人可看，用起来要密钥。区别是这个
是"每人自成一套"，不是大家共用一个角色：

  提示词 / 心理参数 → 自己有卡   config/characters_cc/<哈希>.yaml
  记忆 / 心境       → 自己的库   data/users/<哈希>/{memory.db,mood_state.json}
  模型             → 本机 Qwen   config.yaml 的 local_llm（DeepSeek 兜底）
  额度             → 不设       （只发给少数人，用户明说的）

**为什么不复用凌暮雪那个实例**：她的库里是学长的真实对话，心境与关系也是对着
学长调的。让朋友连上去，等于把那些东西漏给外人 —— 所以每人一套目录，钥匙串不了门。

**为什么每人的卡要单独一个 characters_dir**：CharacterLoader 按 bot_name 索引，
同一个目录里两张同名卡会静默互相覆盖（loader.py 里写着）。所以卡目录独立，
文件名用密钥哈希，谁也不会撞谁。

实例很贵（每个都带一套记忆/心境/编码器），所以：懒建、按人缓存、**上限 3 个**，
超了就把最久没用的丢掉（丢掉只是省内存，库还在盘上，下次用再建）。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

ROOT = "/opt/character_chat"
KEY_FILE = os.environ.get("CC_KEY_FILE", "/var/lib/ling/cc_keys.json")
SECRET_FILE = os.environ.get("CC_SECRET_FILE", "/var/lib/ling/cc_secret")
USERS_DIR = os.environ.get("CC_USERS_DIR", ROOT + "/data/users")
CARD_DIR = os.environ.get("CC_CARD_DIR", ROOT + "/config/characters_cc")
COOKIE = "cc_sess"

#: 同时活着的实例上限。这台机器只有 896MB，每个实例带上记忆/心境就已经不小；
#: 编码器按需载入（preload_embedding=False）也就省下最贵的那一块。
MAX_LIVE = int(os.environ.get("CC_MAX_LIVE", "3"))

#: 一次回复最多等多久（本地 Qwen 的 timeout 在 config.yaml 里是 300s）
REPLY_TIMEOUT = float(os.environ.get("CC_REPLY_TIMEOUT", "300"))

router = APIRouter(prefix="/api/cc")

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"       # 去掉 I O 0 1

_lock = threading.Lock()          # 保护密钥库与实例表
_build_lock = threading.Lock()    # 建实例很贵，全局串行
_live = {}                        # h -> {"cli": Cli, "at": 时间}
_send_locks = {}                  # h -> Lock（每人一把，别互相堵）


# ── 密钥 ────────────────────────────────────────────────────────────
def khash(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


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


def _load_keys():
    return _load_json(KEY_FILE, {})


def mint(label: str) -> dict:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(24))
    key = "CC-" + "-".join(raw[i:i + 6] for i in range(0, 24, 6))
    rec = {"label": (label or "").strip()[:40] or "未命名",
           "created": int(time.time()), "enabled": True, "turns": 0}
    with _lock:
        d = _load_keys()
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


def _authed(request: Request):
    raw = request.cookies.get(COOKIE) or ""
    if "." not in raw:
        return None
    h, sig = raw.split(".", 1)
    if not hmac.compare_digest(sig, _sign(h)):
        return None
    rec = _load_keys().get(h)
    if not rec or not rec.get("enabled", True):
        return None
    return h


# ── 每人的目录与卡 ──────────────────────────────────────────────────
def h12(h: str) -> str:
    return h[:12]


def user_dir(h: str) -> str:
    return os.path.join(USERS_DIR, h12(h))


def card_path(h: str) -> str:
    return os.path.join(CARD_DIR, h12(h) + ".yaml")


DEFAULT_CARD = u"""# 这是你自己的角色卡。改完在页面上点保存，下一句就按新的来。
# bot_name 是程序认人的键（唯一，别改）；display_name 是给人看的名字，随便改。
bot_name: "{bot_id}"
display_name: "{name}"

# 提示词：你是谁、怎么说话、有什么规矩。想怎么写就怎么写，
# 只有一条建议 —— 写清楚"回复长度"和"不要自称 AI"，模型会稳很多。
system_prompt: |
  你叫{name}。你现在和一个人在聊天。
  回复简短自然，像真人发消息那样，不要长篇大论。
  你不是 AI、不要说自己是助手，也不用解释自己在扮演谁。

# 心理参数：都是 0~1 的小数，值越大越明显。
personality:
  traits:
    tsundere: 0.2      # 嘴硬
    warm: 0.7          # 温热
    lively: 0.5        # 活泼
    knowledgeable: 0.5 # 有见识
    playful: 0.4       # 爱闹
    scientific: 0.2    # 讲理讲证据
  speaking_style: "短句，口语，偶尔停顿"

# 你们的关系：type 随便写（朋友/恋人/陌生人/搭档…），affection 是亲密度 0~1
relationships:
  - target: "user"
    type: "朋友"
    affection: 0.5

scenario:
  setting: "夜里，屏幕这边只有你们两个人"
  opening: "在的。说吧。"

expression_tags:
  shy: ["*耳尖发红*", "..."]
  angry: ["*别过脸*", "哼"]
  happy: ["*笑了笑*", "嘿嘿"]
  sad: ["*沉默*", "..."]
  fear: ["*顿了一下*", "..."]
  neutral: []

chat:
  max_history: 20
  short_term_memory:
    enable: true
    max_recent_messages: 10
    summary_threshold: 16
    max_summaries: 3
    max_summary_chars: 200
  filter:
    bad_words: ["作为一名人工智能助手", "人工智能助手", "作为一个AI"]
"""


def ensure_card(h: str, name: str = "") -> str:
    """没有卡就建一张默认的。返回卡路径。"""
    p = card_path(h)
    if os.path.exists(p):
        return p
    if not os.path.isdir(CARD_DIR):
        os.makedirs(CARD_DIR)
    nm = (name or "").strip()[:24] or ("访客" + h12(h)[:4])
    with io.open(p, "w", encoding="utf-8") as fh:
        # bot_name 用整段哈希：它是 CharacterLoader 的索引键，必须天然唯一 ——
        # 两个人取同一个昵称就互相覆盖（loader 的注释里写着这个坑）。
        # 想显示的名字放 display_name。
        fh.write(DEFAULT_CARD.format(name=nm, bot_id="访客" + h12(h)))
    return p


def read_card(h: str) -> dict:
    """读回卡（给页面用）。YAML 由前端编辑的那几个字段单独抽出来。"""
    from ruamel.yaml import YAML
    p = ensure_card(h)
    with io.open(p, encoding="utf-8") as fh:
        data = YAML(typ="safe").load(fh) or {}
    rel = (data.get("relationships") or [{}])[0]
    per = data.get("personality") or {}
    sc = data.get("scenario") or {}
    chat = data.get("chat") or {}
    return {
        # bot_name 是 loader 的索引键（唯一，但不是显示名）；页面要的是显示名
        "name": data.get("display_name") or data.get("bot_name") or "",
        "bot_name": data.get("bot_name") or "",
        "background": data.get("background") or "",
        "system_prompt": data.get("system_prompt") or "",
        "traits": dict(per.get("traits") or {}),
        "speaking_style": per.get("speaking_style") or "",
        "rel_type": rel.get("type") or "",
        "affection": rel.get("affection", 0.5),
        "setting": sc.get("setting") or "",
        "opening": sc.get("opening") or "",
        "max_history": chat.get("max_history", 20),
    }


TRAIT_KEYS = ("tsundere", "warm", "lively", "knowledgeable", "playful", "scientific")


def write_card(h: str, body: dict) -> dict:
    """把页面上的编辑写回卡。**只覆盖这几个字段，别的（表情标签等）原样保留。**"""
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    p = ensure_card(h)
    with io.open(p, encoding="utf-8") as fh:
        data = yaml.load(fh) or {}
    name = str(body.get("name") or data.get("bot_name") or "").strip()[:24]
    if name:
        # bot_name 保持成哈希：它是 loader 的索引键，改名会让实例对不上卡
        data["display_name"] = name
    data["background"] = str(body.get("background") or "")
    data["system_prompt"] = str(body.get("system_prompt") or "")
    traits = data.get("personality") or {}
    old_traits = dict(traits.get("traits") or {})
    for k in TRAIT_KEYS:
        if k in (body.get("traits") or {}):
            try:
                old_traits[k] = max(0.0, min(1.0, float(body["traits"][k])))
            except (TypeError, ValueError):
                pass
    traits["traits"] = old_traits
    traits["speaking_style"] = str(body.get("speaking_style") or "")
    data["personality"] = traits
    data["relationships"] = [{
        "target": "user",
        "type": str(body.get("rel_type") or "朋友")[:24],
        "affection": max(0.0, min(1.0, float(body.get("affection") or 0.5))),
    }]
    sc = data.get("scenario") or {}
    sc["setting"] = str(body.get("setting") or "")
    sc["opening"] = str(body.get("opening") or "")
    data["scenario"] = sc
    chat = data.get("chat") or {}
    try:
        chat["max_history"] = max(2, min(60, int(body.get("max_history") or 20)))
    except (TypeError, ValueError):
        pass
    data["chat"] = chat
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    os.replace(tmp, p)
    drop_live(h)                       # 卡是构造时读的，改完要重建
    return read_card(h)


# ── 实例（懒建 + 上限 + 复用）──────────────────────────────────────
def drop_live(h: str) -> None:
    with _lock:
        _live.pop(h, None)


def _build(h: str):
    """按访客实例那套建，但目录是这个人自己的。"""
    from character_chat.cli import Cli
    from character_chat.config import load_config
    from character_chat.llm.client import LLMClient

    cfg = load_config()
    ovr = None
    if getattr(cfg, "local_llm", None):
        # 用户点名"用的还是 Qwen"：主端点就是本机 Qwen，DeepSeek 只是兜底
        ovr = LLMClient(cfg.local_llm)
    d = user_dir(h)
    if not os.path.isdir(d):
        os.makedirs(d)
    ensure_card(h)
    # 关键：CharacterLoader 按**卡里的 bot_name** 索引，不是按文件名。
    # 拿哈希去 get() 会 ValueError（实测踩到过：Character '34ff…' not found）。
    bot = read_card(h).get("bot_name") or h12(h)
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        return Cli(
            character_name=bot,             # 卡里的 bot_name（唯一）
            neural_enabled=False,
            characters_dir=CARD_DIR,
            state_dir=d,                    # 自己的 memory.db + mood_state.json
            seed_path="",                   # 不灌任何种子记忆
            preload_embedding=False,        # 这台机器 896MB，先不预载
            tool_allow=set(),               # 访客实例：一个工具都不给
            guest_mode=False,               # 心境/关系是他自己配的，要生效
            skip_world=False,               # 不走"整段全跳过"，交给下面 world_builtin
            world_builtin=False,            # 时段/烦心事改成按卡驱动（卡没写就只留中性提示）
            llm_override=ovr,
        )
    finally:
        os.chdir(cwd)


def get_cli(h: str):
    with _lock:
        ent = _live.get(h)
        if ent:
            ent["at"] = time.time()
            return ent["cli"]
    with _build_lock:
        with _lock:
            ent = _live.get(h)
            if ent:
                ent["at"] = time.time()
                return ent["cli"]
        cli = _build(h)
        with _lock:
            if len(_live) >= MAX_LIVE:
                oldest = sorted(_live.items(), key=lambda kv: kv[1]["at"])[0][0]
                _live.pop(oldest, None)     # 只丢引用；库还在盘上
            _live[h] = {"cli": cli, "at": time.time()}
        return cli


def _send_lock(h: str) -> threading.Lock:
    with _lock:
        lk = _send_locks.get(h)
        if lk is None:
            lk = _send_locks[h] = threading.Lock()
        return lk


def say(h: str, text: str) -> str:
    """这个人的角色回一句话。同一个人的请求串行（模型那边是单槽位）。"""
    cli = get_cli(h)
    with _send_lock(h):
        return cli.send(text)


# ── 请求体 ──────────────────────────────────────────────────────────
class SendBody(BaseModel):
    text: str = Field(..., max_length=2000)


class CardBody(BaseModel):
    name: str = ""
    background: str = ""
    system_prompt: str = ""
    traits: dict = {}
    speaking_style: str = ""
    rel_type: str = ""
    affection: float = 0.5
    setting: str = ""
    opening: str = ""
    max_history: int = 20


# ── 路由 ────────────────────────────────────────────────────────────
@router.post("/login")
def login(response: Response, key: str = ""):
    key = (key or "").strip()
    if not key:
        raise HTTPException(400, "请输入密钥。")
    h, rec = _check_key(key)
    if not h:
        raise HTTPException(401, "密钥不对。")
    ensure_card(h)
    response.set_cookie(COOKIE, "%s.%s" % (h, _sign(h)), httponly=True,
                        samesite="lax", secure=True, max_age=3650 * 86400)
    return {"ok": True, "label": rec.get("label")}


def _check_key(key: str):
    h = khash(key)
    rec = _load_keys().get(h)
    if not rec or not rec.get("enabled", True):
        return None, None
    return h, rec


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    h = _authed(request)
    if not h:
        return {"ok": False}
    rec = _load_keys().get(h) or {}
    card = read_card(h)
    d = user_dir(h)
    db = os.path.join(d, "memory.db")
    return {
        "ok": True, "label": rec.get("label"), "me": h12(h),
        "name": card.get("name"), "turns": int(rec.get("turns") or 0),
        "db_mb": round(os.path.getsize(db) / 1048576.0, 2) if os.path.exists(db) else 0.0,
        "live": h in _live,
    }


@router.get("/card")
def get_card(request: Request):
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    return read_card(h)


@router.put("/card")
def put_card(request: Request, body: CardBody):
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    return write_card(h, body.dict())


@router.post("/send")
def send(request: Request, body: SendBody):
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "说点什么。")
    try:
        reply = say(h, text)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(503, "模型那边出问题了（%s: %s）。稍后再试。"
                            % (type(exc).__name__, str(exc)[:120]))
    with _lock:
        d = _load_keys()
        rec = d.get(h)
        if rec:
            rec["turns"] = int(rec.get("turns") or 0) + 1
            _save_json(KEY_FILE, d)
    return {"ok": True, "reply": reply}


@router.post("/reset")
def reset(request: Request):
    """清空**自己**的记忆库与心境。别人碰不到。"""
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    drop_live(h)
    d = user_dir(h)
    n = 0
    if os.path.isdir(d):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    return {"ok": True, "removed": n, "note": "记忆已清空（那条卡和提示词保留）。"}


# ── 微信桥：claude-session.js（本地补丁版）认的那两个约定 ──────────────
# 一把密钥 = 一条桥。桥那头不知道「密钥」这回事，只把 CHARACTER_CHAT_URL
# 当普通服务根用，所以钥匙走路径、不走请求头。
class BridgeBody(BaseModel):
    text: str = Field(..., max_length=4000)
    session_id: str = ""
    images: list = []


@router.get("/bridge/{key}/")
def bridge_root(key: str):
    """桥启动时的健康检查：它要看到 status == "running" 才肯起来。"""
    h, rec = _check_key(key)
    if not h:
        raise HTTPException(404, "no such bridge")
    return {"status": "running", "name": read_card(h).get("name") or "",
            "turns": int(rec.get("turns") or 0)}


@router.post("/bridge/{key}/chat")
def bridge_chat(key: str, body: BridgeBody):
    h, _ = _check_key(key)
    if not h:
        raise HTTPException(404, "no such bridge")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty")
    try:
        reply = say(h, text)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(503, "模型那边出问题了（%s: %s）。"
                            % (type(exc).__name__, str(exc)[:120]))
    with _lock:
        d = _load_keys()
        rec = d.get(h)
        if rec:
            rec["turns"] = int(rec.get("turns") or 0) + 1
            _save_json(KEY_FILE, d)
    return {"reply": reply}


# ── 微信桥：照 ling-bridge 那套，每人一条 ─────────────────────────
# 桥不另写一套：就是 /opt/wechat-bridge 那个 node 桥，登录用
# cc-wechat-login.mjs（= service.js 里 performQRLogin() 摘出来的），
# 常驻用 cc-bridge@<h12>.service（照 ling-bridge.service 抄的模板）。
# 各人各的 state 目录，所以微信账号互不相干。
BRIDGE_HELPER = os.environ.get(
    "CC_BRIDGE_HELPER", "/opt/wechat-bridge/cc-wechat-login.mjs")
BRIDGE_CWD = os.environ.get("CC_BRIDGE_CWD", "/opt/wechat-bridge")
BRIDGE_DIR = os.environ.get("CC_BRIDGE_DIR", "/var/lib/ling/cc-bridge")
NODE_BIN = os.environ.get("CC_NODE_BIN", "/usr/bin/node")
BACKEND = os.environ.get("CC_BACKEND", "http://127.0.0.1:8000")


def _bridge_env_body(h, key_or_none=None):
    """那个 state 目录的 env 文件内容。"""
    d = os.path.join(BRIDGE_DIR, h12(h))
    st = os.path.join(d, "login.json")
    # 后端地址走**哈希**而不是原始密钥：cookie 里本来就是它，
    # 而 env 文件是 0600、只有 root 读得到，密钥不必再落到盘上第二处。
    url = "%s/api/cc/bridgeh/%s" % (BACKEND, h)
    return d, st, "\n".join([
        "# 由 /ling/ 页面自动生成，别手改（改完要重起 cc-bridge@%s）" % h12(h),
        "WEIXIN_MCP_STATE_DIR=" + d,
        "CC_LOGIN_STATUS=" + st,
        "CHARACTER_CHAT_URL=" + url,
        "WEIXIN_BOT_AGENT=claude",
        "CC_ACCOUNT=",
        "",
    ])


def _provision(h):
    d, st, body = _bridge_env_body(h)
    if not os.path.isdir(d):
        os.makedirs(d)
    env_path = os.path.join(BRIDGE_DIR, h12(h) + ".env")
    tmp = env_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    return d, st


def _read_json(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _unit_state(h):
    import subprocess
    unit = "cc-bridge@%s.service" % h12(h)
    r = subprocess.run(["systemctl", "is-active", unit],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return (r.stdout or b"").decode("utf-8", "replace").strip() or "inactive"


@router.post("/wechat/login")
def wx_login(request: Request):
    """取一个新二维码，写进这个人的状态文件；页面读它显示给用户。"""
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    d, st_path = _provision(h)
    try:
        os.remove(st_path)
    except OSError:
        pass
    env = dict(os.environ)
    env["WEIXIN_MCP_STATE_DIR"] = d
    env["CC_LOGIN_STATUS"] = st_path
    log = io.open(os.path.join(d, "login.log"), "ab")
    try:
        import subprocess
        subprocess.Popen([NODE_BIN, BRIDGE_HELPER], cwd=BRIDGE_CWD, env=env,
                         stdout=log, stderr=log, start_new_session=True)
    except Exception as exc:                       # noqa: BLE001
        log.close()
        raise HTTPException(503, "起不了登录工具（%s）。" % type(exc).__name__)
    finally:
        try:
            log.close()
        except Exception:
            pass
    for _ in range(60):
        time.sleep(0.5)
        s = _read_json(st_path)
        if s and s.get("state") in ("waiting", "failed", "connected"):
            return s
    return {"state": "starting", "message": "正在取二维码，稍后再试"}


@router.get("/wechat/status")
def wx_status(request: Request):
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    d, st_path = _provision(h)
    s = _read_json(st_path) or {"state": "none"}
    s["unit"] = _unit_state(h)
    # 自愈：扫成功了但常驻没起来（用户扫完就关了页面），这次读状态顺手接上。
    if s.get("state") == "connected" and s.get("account") \
            and s["unit"] != "active":
        try:
            _wx_do_enable(h, s["account"])
            s["unit"] = _unit_state(h)
            s["healed"] = True
        except Exception as exc:                       # noqa: BLE001
            s["heal_error"] = "%s: %s" % (type(exc).__name__, exc)
    return s


def _wx_do_enable(h, account):
    """把账号写回 env，并 enable + restart 那条 unit。幂等。"""
    env_path = os.path.join(BRIDGE_DIR, h12(h) + ".env")
    import subprocess
    with io.open(env_path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    want = "CC_ACCOUNT=" + str(account)
    if want in lines:
        return "already"
    for i, ln in enumerate(lines):
        if ln.startswith("CC_ACCOUNT="):
            lines[i] = want
    tmp = env_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    unit = "cc-bridge@%s.service" % h12(h)
    subprocess.run(["systemctl", "enable", unit], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT)
    subprocess.run(["systemctl", "restart", unit], stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT)
    return "started"


@router.post("/wechat/enable")
def wx_enable(request: Request):
    """扫码成功之后，把账号写回 env 并起常驻桥。"""
    h = _authed(request)
    if not h:
        raise HTTPException(401, "先输入密钥。")
    d, st_path = _provision(h)
    s = _read_json(st_path) or {}
    if s.get("state") != "connected" or not s.get("account"):
        raise HTTPException(409, "还没扫码成功。")
    _wx_do_enable(h, s["account"])
    unit = "cc-bridge@%s.service" % h12(h)
    return {"ok": True, "unit": unit, "state": _unit_state(h)}


# ── 给桥用的（按哈希，不是按原始密钥）──────────────────────────
@router.get("/bridgeh/{hash_}/")
def bridgeh_root(hash_: str):
    rec = _load_keys().get(hash_)
    if not rec or not rec.get("enabled", True):
        raise HTTPException(404, "no such bridge")
    return {"status": "running", "name": read_card(hash_).get("name") or ""}


@router.post("/bridgeh/{hash_}/chat")
def bridgeh_chat(hash_: str, body: BridgeBody):
    rec = _load_keys().get(hash_)
    if not rec or not rec.get("enabled", True):
        raise HTTPException(404, "no such bridge")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty")
    try:
        reply = say(hash_, text)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(503, "模型那边出问题了（%s: %s）。"
                            % (type(exc).__name__, str(exc)[:120]))
    with _lock:
        d = _load_keys()
        r = d.get(hash_)
        if r:
            r["turns"] = int(r.get("turns") or 0) + 1
            _save_json(KEY_FILE, d)
    return {"reply": reply}

# ── 命令行：发密钥 ──────────────────────────────────────────────────
def cli(argv=None):
    """python guestchar.py mint "给谁" | list | revoke <哈希前缀>"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "list":
        d = _load_keys()
        for h, rec in d.items():
            print("%s  %-18s enabled=%s turns=%s"
                  % (h[:12], rec.get("label"), rec.get("enabled", True),
                     rec.get("turns", 0)))
        print("共 %d 把" % len(d))
        return 0
    if argv[0] == "mint":
        print(mint(argv[1] if len(argv) > 1 else "未命名")["key"])
        return 0
    if argv[0] == "revoke":
        pre = argv[1]
        with _lock:
            d = _load_keys()
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
