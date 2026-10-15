#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「她不一样」(She-love-me-plus) 网页版 —— 密钥邀请制，本地 Qwen 优先。

和游戏宿主那套完全不是一个性质，所以先说清楚边界：

  · **只有持密钥的人能用**（站主发给朋友的），密钥存 sha256，不存明文；
    每个密钥有每日次数与有效期，另有全站每日上限 —— 一次分析要烧掉几万
    token，密钥万一流出去，烧的是站主的钱。
  · **本地 Qwen 优先，DeepSeek 兜底**：分析走 cfg.local_llm（本机
    Qwen3.6-35B-A3B，经隧道到 127.0.0.1:8001），主端点异常或返回空内容时
    由 LLMClient 自动落到 DeepSeek。想强制只走云端：删掉 config.yaml 里的
    local_llm 整块即可。
  · **不落盘**：访客上传的聊天记录只写进一个临时目录，跑完（无论成败）就删；
    日志里只记条数/字数，不记内容。报告 HTML 只留在内存里等访客取走或过期。
  · **默认脱敏**：默认把手机号/邮箱/链接/长数字串替换掉，并在提示里说明。
    上传的聊天里还有**第三方**的话（对话另一方没同意过），页面必须讲清楚。

管线（尽量复用原仓库自己的代码，而不是我另写一套）：
    messages.json --(stats_analyzer.py)--> stats.json
                  --(LLM，三段式)--> analysis.json  --(analysis_contract 校验)
                  --(generate_html_report.py)--> 报告 HTML（原版长相）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

APP_DIR = os.environ.get("SLM_APP_DIR", "/opt/slm/app")
WORK_ROOT = os.environ.get("SLM_WORK", "/opt/slm/work")
KEY_FILE = os.environ.get("SLM_KEY_FILE", "/var/lib/ling/slm_keys.json")
SECRET_FILE = os.environ.get("SLM_SECRET_FILE", "/var/lib/ling/slm_secret")
PYTHON = os.environ.get("SLM_PYTHON", sys.executable)

# 额度：0 = 不限。站主 2026-10-13 定为「每把密钥 3 次/日」，所以默认 3；
# mint() 不显式给 --calls 时也按这个走。想改回不限：把 SLM_CALLS_PER_DAY 设成 0。
CALLS_PER_DAY = int(os.environ.get("SLM_CALLS_PER_DAY", "3"))
GLOBAL_PER_DAY = int(os.environ.get("SLM_GLOBAL_PER_DAY", "0"))
MAX_MSGS = int(os.environ.get("SLM_MAX_MSGS", "20000"))
MAX_UPLOAD = int(os.environ.get("SLM_MAX_UPLOAD", str(4 * 1024 * 1024)))
LLM_CHARS = int(os.environ.get("SLM_LLM_CHARS", "24000"))   # 每段提示里的转录上限
COOKIE = "slm_sess"
JOB_TTL_S = int(os.environ.get("SLM_JOB_TTL_S", "7200"))
# 追问（拿着刚出的报告继续问）：一条 0.1 次额度，上下文用压缩后的报告数据
FOLLOWUP_COST = float(os.environ.get("SLM_FOLLOWUP_COST", "0.1"))
FOLLOWUP_CHARS = int(os.environ.get("SLM_FOLLOWUP_CHARS", "12000"))
FOLLOWUP_MAX_TOKENS = int(os.environ.get("SLM_FOLLOWUP_MAX_TOKENS", "1200"))
FOLLOWUP_TURNS = int(os.environ.get("SLM_FOLLOWUP_TURNS", "8"))
# 可选引擎与各自额度：本地 Qwen = 1×，云端 DeepSeek = 2×（贵一倍）
COST = {
    "local": {"analysis": 1.0, "followup": 0.1, "label": "本地 Qwen3.6-35B-A3B"},
    "cloud": {"analysis": 2.0, "followup": 0.2, "label": "DeepSeek（云端）"},
}
DEFAULT_ENGINE = os.environ.get("SLM_DEFAULT_ENGINE", "local")
# 上游只有一个本地模型实例（约 30 tok/s），真并发只会让所有人一起变慢 → 默认串行；
# 其余进队列（QUEUE_MAX 是队列长度上限，排满才拒）。
MAX_RUNNING = int(os.environ.get("SLM_MAX_RUNNING", "1"))
QUEUE_MAX = int(os.environ.get("SLM_QUEUE_MAX", "4"))

_lock = threading.Lock()
_jobs = {}


class SlmError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


# ── 密钥 ────────────────────────────────────────────────────────────
def _secret() -> bytes:
    try:
        with open(SECRET_FILE, "rb") as fh:
            s = fh.read().strip()
        if s:
            return s
    except Exception:
        pass
    s = base64.b16encode(os.urandom(16))
    try:
        d = os.path.dirname(SECRET_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(SECRET_FILE, "wb") as fh:
            fh.write(s)
    except Exception:
        pass
    return s


def khash(key: str) -> str:
    return hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()


def _load_keys() -> dict:
    try:
        with io.open(KEY_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_keys(d: dict) -> None:
    parent = os.path.dirname(KEY_FILE)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = KEY_FILE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, KEY_FILE)


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # 去掉容易看错的 I O 0 1


def mint(label: str, days: int = 365, calls: int = None) -> dict:
    """发一把新密钥（明文只在这一次返回，之后只留哈希）。

    calls 不传时按 CALLS_PER_DAY（默认 3 次/日）走。
    """
    if calls is None:
        calls = CALLS_PER_DAY
    raw = "".join(ALPHABET[os.urandom(1)[0] % len(ALPHABET)] for _ in range(24))
    key = "SLM-" + "-".join(raw[i:i + 6] for i in range(0, 24, 6))
    rec = {"label": (label or "").strip()[:40] or "未命名",
           "created": int(time.time()),
           "expires": int(time.time()) + int(days) * 86400,
           "calls_per_day": int(calls),
           "used_day": "",
           "used_today": 0,
           "total": 0,
           "enabled": True}
    with _lock:
        d = _load_keys()
        d[khash(key)] = rec
        _save_keys(d)
    return {"key": key, "record": rec}


def list_keys() -> list:
    d = _load_keys()
    out = []
    for h, r in d.items():
        out.append({"hash": h[:12], "label": r.get("label"), "enabled": r.get("enabled"),
                    "expires": r.get("expires"), "used_today": r.get("used_today"),
                    "total": r.get("total"), "calls_per_day": r.get("calls_per_day")})
    return sorted(out, key=lambda x: -(x.get("total") or 0))


def revoke(hash_prefix: str) -> int:
    """按哈希前缀吊销（列表里显示的就是前缀）。"""
    n = 0
    with _lock:
        d = _load_keys()
        for h in list(d):
            if h.startswith(hash_prefix):
                d.pop(h)
                n += 1
        if n:
            _save_keys(d)
    return n


def check_key(key: str) -> dict:
    """按明文密钥校验 + 记账（CLI/测试用）。"""
    h = khash(key)
    with _lock:
        if h not in _load_keys():
            raise SlmError("密钥不对。", 401)
    return spend(h)


def spend(h: str, cost: float = 1.0) -> dict:
    """按密钥哈希校验 + 记一次额度。**额度只有这一处实现**（服务端与 CLI 都走它）。

    额度是**小数**：一次完整分析花 1.0，一条追问花 0.1（所以 3/日 = 3 次分析，
    或 30 条追问，或任意混合）。cap <= 0（密钥上）或 GLOBAL_PER_DAY <= 0（全站）
    表示不限。
    """
    today = time.strftime("%Y-%m-%d")
    with _lock:
        d = _load_keys()
        rec = d.get(h)
        if not rec:
            raise SlmError("密钥不对。", 401)
        if not rec.get("enabled", True):
            raise SlmError("这把密钥已被停用。", 403)
        if int(rec.get("expires") or 0) < time.time():
            raise SlmError("这把密钥过期了。", 403)
        if rec.get("used_day") != today:
            rec["used_day"], rec["used_today"] = today, 0
        used = float(rec.get("used_today") or 0)
        cap = float(rec.get("calls_per_day") or 0)
        if cap > 0 and used + cost > cap + 1e-9:
            raise SlmError("今天额度不够了（还剩 %.1f，这次要 %.1f）。" % (max(0.0, cap - used), cost),
                           429)
        if GLOBAL_PER_DAY > 0:
            tot = sum(float(x.get("used_today") or 0) for x in d.values()
                      if x.get("used_day") == today)
            if tot + cost > GLOBAL_PER_DAY + 1e-9:
                raise SlmError("今天站上的分析额度用完了，明天再来。", 429)
        rec["used_today"] = round(used + cost, 3)
        rec["total"] = int(rec.get("total") or 0) + 1
        _save_keys(d)
        return {"label": rec.get("label"), "hash": h,
                "remaining": (-1 if cap <= 0 else round(cap - rec["used_today"], 2)),
                "cost": cost}


def refund(h: str, cost: float = 1.0) -> None:
    """把刚记上的一次额度还回去。

    路由里是「先记账、再入队」，而入队可能因为「同密钥已有一个在飞」或
    「队列排满」被拒 —— 被拒的那次不该算数，否则用户连点几下就把当天
    3 次额度烧光，却一份报告都没跑。
    """
    with _lock:
        d = _load_keys()
        rec = d.get(h)
        if not rec:
            return
        rec["used_today"] = round(max(0.0, float(rec.get("used_today") or 0) - cost), 3)
        rec["total"] = max(0, int(rec.get("total") or 0) - 1)
        _save_keys(d)


def quota_left(h: str):
    """查剩余额度（不记账）。不限次数返回 -1。"""
    today = time.strftime("%Y-%m-%d")
    with _lock:
        rec = _load_keys().get(h)
    if not rec:
        return None
    cap = float(rec.get("calls_per_day") or 0)
    if cap <= 0:
        return -1
    used = float(rec.get("used_today") or 0) if rec.get("used_day") == today else 0.0
    return round(cap - used, 2)


def session_cookie(key: str) -> str:
    h = khash(key)
    sig = hmac.new(_secret(), h.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return "%s.%s" % (sig, h[:16])


def cookie_key_hash(tok: str):
    """cookie -> 密钥哈希（或 None）。cookie 里不出现密钥本身。"""
    try:
        sig, short = (tok or "").split(".", 1)
    except Exception:
        return None
    with _lock:
        d = _load_keys()
    for h in d:
        if h.startswith(short):
            want = hmac.new(_secret(), h.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
            if hmac.compare_digest(want, sig):
                return h
    return None


# ── 输入解析与脱敏 ──────────────────────────────────────────────────
def parse_input(raw: str) -> dict:
    """吃三种输入，尽量宽容：

      1. 原项目 `extract_messages.py` 的 messages.json
         （sender 已是 me/them/unknown，type 是字符串，content + timestamp）
      2. 本地 GUI / `export_messages.py` 的导出
         （字段名不同：display_content / type_name / time_str / display_name，
          可能只有 is_send 布尔）
      3. 「发送者<TAB>时间<TAB>内容」的纯文本（手动复制粘贴用）

    盲区提醒：**unknown 的 sender 不能当成"对方"** —— 原导出里它表示"分不清是谁"，
    硬塞给对方会让统计层把无关消息算进关系里。所以保留原样，让统计层自己跳过。
    """
    raw = raw or ""
    if len(raw) > MAX_UPLOAD:
        raise SlmError("文件太大了（上限 %d MB）。" % (MAX_UPLOAD // 1024 // 1024))
    data = None
    s = raw.lstrip()
    if s.startswith("{") or s.startswith("["):
        try:
            obj = json.loads(raw)
        except Exception as e:
            raise SlmError("这看着像 JSON，但解析不了：%s" % str(e)[:60])
        if isinstance(obj, list):
            obj = {"messages": obj}
        if not isinstance(obj, dict) or not isinstance(obj.get("messages"), list):
            raise SlmError("JSON 里没有 messages 数组。")
        data = {"contact_display": (obj.get("contact_display")
                                   or obj.get("display_name")
                                   or obj.get("nickname")
                                   or obj.get("contact_username") or "对方"),
                "messages": []}
        for m in obj["messages"]:
            if not isinstance(m, dict):
                continue
            s_raw = str(m.get("sender") or "").strip().lower()
            if s_raw in ("me", "我", "self", "own", "true"):
                sender = "me"
            elif s_raw in ("them", "ta", "他", "她", "对方", "other", "false"):
                sender = "them"
            elif s_raw in ("unknown", "未知", ""):
                if isinstance(m.get("is_send"), bool):
                    sender = "me" if m["is_send"] else "them"
                else:
                    sender = "unknown"
            else:
                # 微信原生导出里 sender 可能是昵称/wxid：非"我"就按对方处理
                sender = "them"
            ts = m.get("timestamp") or m.get("create_time") or m.get("time") or 0
            try:
                ts = int(float(ts))
            except Exception:
                ts = 0
            if not ts:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        ts = int(time.mktime(time.strptime(str(m.get("time_str") or "").strip(), fmt)))
                        break
                    except Exception:
                        continue
            content = (m.get("content") or m.get("display_content") or m.get("text") or "")
            kind = m.get("type")
            if kind is None or isinstance(kind, (int, float)):
                kind = m.get("type_name") or "text"
            data["messages"].append({"sender": sender,
                                     "type": str(kind).strip().lower() or "text",
                                     "content": str(content),
                                     "timestamp": ts})
    else:
        # 文本格式：每行 发送者 <TAB> 时间 <TAB> 内容（时间可省）
        contact = "对方"
        msgs = []
        for line in raw.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            parts = re.split(r"\t+", line, maxsplit=2)
            if len(parts) < 2:
                continue
            who, rest = parts[0].strip(), parts[1:]
            ts = 0
            text = rest[-1] if rest else ""
            if len(rest) >= 2:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                            "%Y-%m-%d %H:%M", "%m-%d %H:%M"):
                    try:
                        t = time.strptime(rest[0].strip(), fmt)
                        if fmt.startswith("%m"):
                            t = time.struct_time((time.localtime().tm_year,) + tuple(t)[1:])
                        ts = int(time.mktime(t))
                        break
                    except Exception:
                        continue
            if re.search(r"^(我|me|self)$", who, re.I):
                sender = "me"
            else:
                sender = "them"
                if who and contact == "对方" and not re.search(r"^(对方|她|他|them)$", who):
                    contact = who
            msgs.append({"sender": sender, "type": "text", "content": text,
                         "timestamp": ts})
        data = {"contact_display": contact, "messages": msgs}
    if not data["messages"]:
        raise SlmError("没解析出任何消息。")
    if len(data["messages"]) > MAX_MSGS:
        data["messages"] = data["messages"][-MAX_MSGS:]
    no_ts = [m for m in data["messages"] if not m["timestamp"]]
    if len(no_ts) > len(data["messages"]) * 0.5:
        raise SlmError("超过一半的消息没有时间戳（timestamp），统计层算不了 —— "
                       "请用项目里的工具导出，别自己手搓 JSON。")
    # 时间戳缺失的少部分：按邻居插值（原地补，不改顺序）
    for i, m in enumerate(data["messages"]):
        if not m["timestamp"]:
            m["timestamp"] = (data["messages"][i - 1]["timestamp"] + 60) if i else int(time.time())
    return data


_REDACT = (
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
    (re.compile(r"https?://\S+"), "[链接]"),
    (re.compile(r"\b\d{15,}\b"), "[长数字]"),
    (re.compile(r"\b\d{6,14}\b"), "[号码]"),
)


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


def transcript(data: dict, limit: int = LLM_CHARS) -> tuple:
    """把消息拼成给模型看的转录。超长就**按时间等距抽样**，而不是掐掉后半段。"""
    msgs = [m for m in data["messages"] if m["type"] == "text" and m["content"].strip()]
    if not msgs:
        msgs = [m for m in data["messages"] if m["content"].strip()]
    # 三种 sender 都要如实标：unknown 是"分不清谁发的"，不能混进"对方"里
    label = {"me": "我", "them": "对方", "unknown": "未知"}
    lines = ["%s: %s" % (label.get(m["sender"], "未知"),
                         re.sub(r"\s+", " ", m["content"]).strip())
             for m in msgs]
    body = "\n".join(lines)
    sampled = False
    if len(body) > limit:
        step = max(2, int(len(lines) / max(1, limit // 40)))
        keep = lines[::step]
        body = "\n".join(keep)
        sampled = True
    return body, {"messages": len(msgs), "chars": len(body), "sampled": sampled}


# ── 提示词：直接读原仓库的参考文档，不另编一套 ─────────────────────
def _doc(rel: str, cap: int = 20000) -> str:
    try:
        with io.open(os.path.join(APP_DIR, rel), encoding="utf-8") as fh:
            return fh.read()[:cap]
    except Exception:
        return ""


_DOCS = {}


def docs() -> dict:
    if not _DOCS:
        _DOCS["skill"] = _doc(".agents/skills/she-love-me/SKILL.md")
        _DOCS["schema"] = _doc(".agents/skills/she-love-me/references/report-schema.md")
        _DOCS["rubrics"] = _doc(".agents/skills/she-love-me/references/scoring-rubrics.md")
    return _DOCS


def _stage_prompts(kind: str, tr: str, digest: str, stats_brief: str) -> list:
    d = docs()
    base = ("你是「她不一样」的首席分析师兼关系心理顾问。按下面的框架与评分细则分析聊天记录。\n"
            "**只输出一个 JSON 对象**，不要解释、不要 markdown 代码块。\n\n"
            "【评分细则】\n%s\n\n【报告字段契约（必须遵守的键名与取值）】\n%s\n"
            % (d.get("rubrics", ""), d.get("schema", "")))
    ctx = "【统计层给出的硬数字】\n%s\n\n" % stats_brief
    if kind == "A":
        ask = ("\n请输出这些键：meta, relationship_type, relationship_label, relationship_trend, "
               "relationship_stage, emotional_asymmetry, personality_portrait, language_patterns, "
               "key_findings, verdict, simp_description, love_description, "
               '"_digest"（给后续分析用的 800 字以内关系速写，不是给人看的字段）\n'
               "情绪/风险类的不必给（后面单独出）。")
        return [{"role": "system", "content": base},
                {"role": "user", "content": ctx + "【聊天记录】\n" + tr + ask}]
    if kind == "B":
        ask = ("\n这份记录已经过一轮分析，先给你它的关系速写；请**只输出**这些键："
               "sternberg, gottman, personality, danger_warnings。"
               "分数要落在细则给的区间里；danger_warnings 没有就给 []。")
        return [{"role": "system", "content": base},
                {"role": "user", "content": ctx + "【关系速写】\n" + digest + ask}]
    ask = ("\n这份记录已经过一轮分析，先给你它的关系速写；请**只输出**这些键："
           "strategist, patriarch_wisdom, affinity, moments, intent_profile, reply_grades。"
           "moments（关键时刻时间线）、intent_profile（动作分类）、reply_grades（回复评分）"
           "按契约的结构给；给不出就 null。")
    return [{"role": "system", "content": base},
            {"role": "user", "content": ctx + "【关系速写】\n" + digest + ask}]


ALL_KEYS = ("meta", "relationship_type", "relationship_label", "relationship_trend",
            "relationship_stage", "emotional_asymmetry", "personality_portrait",
            "language_patterns", "sternberg", "gottman", "personality",
            "danger_warnings", "strategist", "key_findings", "patriarch_wisdom",
            "simp_description", "love_description", "verdict",
            "affinity", "moments", "intent_profile", "reply_grades")
NULL_OK = ("affinity", "moments", "intent_profile", "reply_grades", "danger_warnings")


def _json_out(txt: str):
    s = (txt or "").strip()
    s = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    m = re.search(r"\{.*\}", s, re.S)
    for cand in ((m.group(0) if m else None), s):
        if not cand:
            continue
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    return None


# ── 管线 ────────────────────────────────────────────────────────────
def _llm():
    """默认客户端：**本地 Qwen 优先，DeepSeek 兜底**（engine="local"）。"""
    return _llm_for("local")


def _llm_for(engine: str = "local"):
    """按访客选的引擎建客户端。

    engine="local"  → cfg.local_llm（本机 Qwen，经隧道到 127.0.0.1:8001），
                      主端点异常/返空时由 LLMClient 自动落到 DeepSeek；
    engine="cloud"  → 直连 cfg.llm（DeepSeek），不带兜底（它本身就是兜底）。
    两条路的额度不同：本地 1×，云端 2×（见 COST）。
    """
    engine = engine if engine in COST else "local"

    import dataclasses
    from pathlib import Path
    from character_chat.config import load_config
    from character_chat.llm.client import LLMClient
    proj = Path(__file__).resolve().parents[2]
    cwd = os.getcwd()
    try:
        os.chdir(str(proj))
        cfg = load_config()
    finally:
        os.chdir(cwd)
    if engine == "cloud":
        return LLMClient(dataclasses.replace(cfg.llm, timeout=600.0, max_tokens=8000))
    base = cfg.local_llm or cfg.llm
    # 本地实测约 30 tok/s，单段最多 8000 token 输出 ≈ 4.5 分钟，600s 不宽裕
    return LLMClient(dataclasses.replace(base, timeout=900.0, max_tokens=8000))


def engine_available(engine: str) -> tuple:
    """提交前探一下所选引擎在不在（避免"选了本地、实际落云端还按 1× 收费"）。

    本地：发一个 1 token 的小请求探测（顺便把空闲被卸载的模型叫起来）；
    云端：只看 key 有没有配。
    """
    if engine == "cloud":
        try:
            from character_chat.config import load_config
            cfg = load_config()
            return (bool(cfg.llm.api_key), "" if cfg.llm.api_key else "云端没有配 API key")
        except Exception as exc:
            return False, "读不到云端配置（%s）" % type(exc).__name__
    cli = _llm_for("local")
    if not cli.config.fallback_base_url:
        return False, "config 里没有配 local_llm（本地端点）"
    t0 = time.time()
    try:
        cli.client.chat.completions.create(
            model=cli.model_id, messages=[{"role": "user", "content": "hi"}],
            max_tokens=1, stream=False)
        return True, "本地模型就绪（%.1fs）" % (time.time() - t0)
    except Exception as exc:
        return False, "本地模型连不上（%s）—— 隧道断了或模型没起" % type(exc).__name__


def _run(cmd, cwd, timeout):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def _warmup(cli, log) -> None:
    """先把本地模型叫醒。

    本地端点空闲一段时间会把模型卸载，**第一个请求会返回 503 Loading model**；
    那一下会被 LLMClient 判为失败、悄悄落到 DeepSeek —— 报告照出，但换了模型。
    所以正式分析前先发一个 max_tokens=1 的小请求把它加载起来。
    """
    if not cli.config.fallback_base_url:
        return
    t0 = time.time()
    try:
        cli.client.chat.completions.create(
            model=cli.model_id, messages=[{"role": "user", "content": "hi"}],
            max_tokens=1, stream=False)
        log("本地模型已就绪（%.1fs）" % (time.time() - t0))
    except Exception as exc:
        log("模型预热没成功（%s），按兜底逻辑继续" % type(exc).__name__)


def _chat_json(cli, messages: list, temperature: float = 0.3) -> str:
    """让模型出 JSON。

    关思维链这件事两边机制不同，实测踩过：
      · DeepSeek（直连、无 fallback）—— 必须显式传 extra_body={"thinking":...}，
        否则它先把 8000 token 的输出预算烧在 reasoning 上，拿到半截 JSON。
      · 本地 Qwen —— **不能**用上面那个字段：实测它不报错，但同样白烧推理预算
        （一句话答案用掉 500 token），预算吃光后 HTTP 200 返回 content=""，
        然后被 LLMClient 判为空内容、静默落到 DeepSeek。正确姿势是
        chat_template_kwargs.enable_thinking=False —— LLMClient._create 在
        配了 fallback 时会自动注入，所以这里只要**别传 thinking** 就行。

    JSON 模式两个后端都支持，都留着（少一点「我这就给你分析」的前言）。
    """
    kwargs = dict(model=cli.model_id, messages=messages, temperature=temperature,
                  max_tokens=cli.config.max_tokens, stream=False,
                  response_format={"type": "json_object"})
    if not cli.config.fallback_base_url:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    resp = cli._create(**kwargs)
    return (resp.choices[0].message.content or "")


def _stats_brief(stats: dict) -> str:
    """把统计层的关键数字摘成几行给模型（不把整个 stats.json 塞进去）。"""
    keys = ("total_messages", "me_messages", "them_messages", "date_start", "date_end",
            "total_days", "avg_reply_seconds_me", "avg_reply_seconds_them",
            "initiation_ratio_me", "initiation_ratio_them", "my_avg_length",
            "their_avg_length", "sessions", "cold_war_days")
    lines = []
    for k in keys:
        v = stats.get(k)
        if isinstance(v, (int, float, str)) and v != "":
            lines.append("· %s = %s" % (k, v))
    for k in ("top_words_me", "top_words_them", "risk_terms"):
        v = stats.get(k)
        if isinstance(v, list) and v:
            lines.append("· %s = %s" % (k, ", ".join(str(x)[:20] for x in v[:12])))
    return "\n".join(lines) or "（统计层没给出可摘的数字）"


# null 归一化：**与桌面流程共用同一份实现**（slm app 的 scripts/null_guard.py）。
# 找不到就退化为"不归一化"，不让主流程挂掉。
try:
    sys.path.insert(0, os.path.join(APP_DIR, "scripts"))
    from null_guard import de_null as _de_null   # noqa: F401
except Exception:                                 # pragma: no cover
    def _de_null(obj):
        return obj


def _fill_and_validate(analysis: dict) -> tuple:
    """补齐契约要求的 20 个顶层键，再用原仓库的校验器过一遍。"""
    # 先把数值型字段里的 null 归零，再补键：
    # 模型会把"算不出来"的字段写成 null，而生成器里的 `.get(k, 0)` 遇到
    # **显式 null** 拿到的是 None（不是默认值），紧接着 `None / 10` 就
    # TypeError —— 线上表现是"报告生成失败（TypeError: unsupported operand
    # type(s) for /: 'NoneType' and 'int'）"。生成器里这类算术有十处
    # （positive_negative_ratio / symmetry_score / passion / intimacy /
    # commitment / simp_index / loved_index / cold_index / total_days /
    # speed_ratio），在源头一次归零比在生成器里逐个打补丁可靠。
    for k in list(analysis.keys()):
        analysis[k] = _de_null(analysis[k])
    for k in ALL_KEYS:
        if k not in analysis:
            # 显式 null 会让渲染器 AttributeError 的四个容器，给空 dict
            analysis[k] = {} if k in ("sternberg", "gottman", "personality", "strategist") \
                else ([] if k in NULL_OK else None)
    sys.path.insert(0, os.path.join(APP_DIR, "scripts"))
    issues = []
    try:
        import analysis_contract
        issues = analysis_contract.validate(analysis)
    except Exception as e:
        issues = [{"level": "info", "where": "<validator>", "what": "校验器没跑起来: %s" % e}]
    errors = [i for i in issues if i.get("level") == "error"]
    return errors, issues


def _analyze(data: dict, work: str, log, engine: str = "local") -> dict:
    """跑完整条管线，返回 {analysis, stats, report_html, meta}。"""
    msgs_path = os.path.join(work, "messages.json")
    stats_path = os.path.join(work, "stats.json")
    with io.open(msgs_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))
    log("统计层…")
    rc, out = _run([PYTHON, os.path.join(APP_DIR, "scripts", "stats_analyzer.py"),
                    "--input", msgs_path, "--output", stats_path, "--affect", "off"],
                   os.path.join(APP_DIR, "scripts"), 300)
    if rc != 0 or not os.path.exists(stats_path):
        raise SlmError("统计层跑失败了（%s）" % (out.strip().splitlines()[-1:] or ["?"])[0][:120], 500)
    with io.open(stats_path, encoding="utf-8") as fh:
        stats = json.load(fh)

    tr, tinfo = transcript(data)
    # 送出去的那份（DeepSeek 是第三方）先脱敏；统计层留在本地、用原始文本
    tr = redact(tr)
    brief = _stats_brief(stats)
    cli = _llm_for(engine)
    _warmup(cli, log)
    calls, tin, tout = 0, 0, 0

    def ask(kind, transcript_text, digest):
        nonlocal calls, tin, tout
        msgs = _stage_prompts(kind, transcript_text, digest, brief)
        log("分析（%s 段，%s）…" % (kind, "本地 Qwen" if cli.config.fallback_base_url
                                   else "DeepSeek"))
        out = _chat_json(cli, msgs, 0.3)
        calls += 1
        tin += sum(len(m["content"]) for m in msgs)
        tout += len(out)
        j = _json_out(out)
        if not isinstance(j, dict):
            raise SlmError("模型（%s 段）没给出合法 JSON。" % kind, 502)
        return j

    j1 = ask("A", tr, "")
    digest = str(j1.pop("_digest", "") or "")[:2000]
    j2 = ask("B", "", digest)
    j3 = ask("C", "", digest)
    analysis = {}
    for j in (j1, j2, j3):
        analysis.update(j)

    errs, issues = _fill_and_validate(analysis)
    if errs:
        log("契约校验发现 %d 个错误，回头让模型修一次…" % len(errs))
        try:
            fix = _chat_json(cli, [
                {"role": "system", "content":
                 "你在修一份 JSON 报告的结构错误。**只输出修好的完整 JSON 对象**，不要解释。"},
                {"role": "user", "content":
                 "下面是当前 JSON 与校验器报的错误，请按错误修好并原样返回完整 JSON（不要删字段）：\n\n"
                 + json.dumps(issues[:40], ensure_ascii=False) + "\n\n"
                 + json.dumps(analysis, ensure_ascii=False)[:24000]},
            ], 0.0)
            calls += 1
            tin += 4000
            tout += len(fix)
            j4 = _json_out(fix)
            if isinstance(j4, dict) and j4.get("meta") is not None:
                analysis.update(j4)
                errs, issues = _fill_and_validate(analysis)
        except Exception as e:
            log("修一次没成：%s" % type(e).__name__)
        analysis["_issues"] = issues[:60]

    # 据实标注：选了本地但中途落到兜底，也要写清楚（别让报告冒充本地跑的）
    eng = engine if engine in COST else "local"
    if eng == "cloud":
        analyzed_by = COST["cloud"]["label"]
    elif cli.last_backend == "primary":
        analyzed_by = COST["local"]["label"]
    else:
        analyzed_by = "%s（本地不可用，自动兜底）" % (cli._fb_model or COST["cloud"]["label"])
    analysis["_meta"] = {"analyzed_by": analyzed_by, "messages": tinfo["messages"],
                         "engine": eng,
                         "sampled": tinfo["sampled"], "report": "She-love-me-plus"}
    contact = re.sub(r"[^\w\u4e00-\u9fa5-]", "", str(data.get("contact_display") or "对方"))[:20] \
        or "对方"
    log("生成报告…")
    outdir = os.path.join(work, "report")
    os.makedirs(outdir, exist_ok=True)
    with io.open(os.path.join(work, "analysis.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(analysis, ensure_ascii=False))
    rc, out = _run([PYTHON, os.path.join(APP_DIR, "scripts", "generate_html_report.py"),
                    "--stats", stats_path,
                    "--analysis", os.path.join(work, "analysis.json"),
                    "--contact", contact, "--output", outdir],
                   os.path.join(APP_DIR, "scripts"), 300)
    if rc != 0:
        raise SlmError("报告生成失败（%s）" % out.strip().splitlines()[-1:][0][:120], 500)
    names = [f for f in os.listdir(outdir) if f.endswith(".html")]
    if not names:
        raise SlmError("报告没生成出来。", 500)
    with io.open(os.path.join(outdir, names[0]), encoding="utf-8") as fh:
        html = fh.read()
    return {"analysis": analysis, "stats": stats, "html": _fix_assets(html),
            "brief": brief,
            "filename": "%s_%s.html" % (contact, time.strftime("%Y%m%d_%H%M")),
            "meta": {"calls": calls, "in_chars": tin, "out_chars": tout,
                     "est_tokens": int((tin + tout) / 2.2), "messages": tinfo["messages"],
                     "sampled": tinfo["sampled"], "errors_left": len(errs)}}


def _fix_assets(html: str) -> str:
    """把报告里的 CDN 依赖换成本站自己的：国内直连 jsdelivr / Google Fonts 基本打不开。"""
    html = html.replace(
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js",
        "/assets/chart.umd.min.js")
    html = re.sub(r'<link[^>]*fonts\.googleapis\.com[^>]*>', "", html)
    return html


# ── 任务表（内存，不落盘）──────────────────────────────────────────
def _gc():
    now = time.time()
    for jid, j in list(_jobs.items()):
        # 排队中/跑着的绝不回收（否则会凭空消失）
        if j.get("state") in ("queued", "running"):
            continue
        if now - j["created"] > JOB_TTL_S:
            _jobs.pop(jid, None)


# 并发模型：**排队串行**，而不是"满了就拒"。
# 上游只有一个本地模型实例（约 30 tok/s，一份记录 3 分钟），真并发只会让
# 所有人一起变慢，所以同时只跑 MAX_RUNNING 个，其余排队；队列排满才拒。
_q = None
_q_lock = threading.Lock()


def _drain():
    """消费队列的常驻线程（按 MAX_RUNNING 起几个）。"""
    while True:
        jid, raw = _q.get()
        job = _jobs.get(jid)
        if job is None:                      # 已过期被回收
            _q.task_done()
            continue
        engine = job.get("engine") or DEFAULT_ENGINE
        with _lock:
            job["state"] = "running"
            job["progress"] = (job.get("progress") or [])[-6:] + [
                "开始分析（%s）…" % COST[engine]["label"]]
        log = job["_log"]
        work = tempfile.mkdtemp(prefix="slm-",
                                dir=WORK_ROOT if os.path.isdir(WORK_ROOT) else None)
        try:
            data = parse_input(raw)
            log("解析出 %d 条消息，对方是「%s」" % (len(data["messages"]),
                                                    data["contact_display"]))
            res = _analyze(data, work, log, engine=engine)
            with _lock:
                job["state"] = "done"
                job["html"] = res["html"]
                job["filename"] = res["filename"]
                job["meta"] = res["meta"]
                # 留住报告数据与统计摘要：追问要拿它当上下文
                job["analysis"] = res.get("analysis") or {}
                job["brief"] = res.get("brief") or ""
                job["chat"] = []
            log("完成（%d 次模型调用，约 %d token）" % (res["meta"]["calls"],
                                                     res["meta"]["est_tokens"]))
        except SlmError as e:
            with _lock:
                job["state"] = "error"
                job["error"] = str(e)
        except Exception as e:
            with _lock:
                job["state"] = "error"
                job["error"] = "内部错误（%s）" % type(e).__name__
        finally:
            # 无论成败：聊天记录、统计、报告全部随临时目录删掉
            shutil.rmtree(work, ignore_errors=True)
            job.pop("_log", None)
            _q.task_done()


def _ensure_drainers():
    global _q
    with _q_lock:
        if _q is not None:
            return
        _q = queue.Queue()
        for _ in range(max(1, MAX_RUNNING)):
            threading.Thread(target=_drain, daemon=True).start()


def _queue_pos(jid: str) -> int:
    """排队位置（从 1 开始）。dict 保持插入序，所以按序数一遍就是 FIFO 位置。"""
    pos = 0
    for j in _jobs.values():
        if j.get("state") == "queued":
            pos += 1
            if j["id"] == jid:
                return pos
    return 0


def start(raw: str, key_rec: dict, engine: str = None) -> dict:
    """入队（不立刻执行）。状态先给 queued，前端据 queue_pos 显示前面还有几个。"""
    engine = engine if engine in COST else DEFAULT_ENGINE
    _ensure_drainers()
    _gc()
    h = key_rec.get("hash")
    with _lock:
        active = [j for j in _jobs.values() if j.get("state") in ("queued", "running")]
        # 同一把密钥只允许一个在飞：否则一人同时开几份，别人排到很后面
        if h and any(j.get("key_hash") == h for j in active):
            raise SlmError("你这把密钥已经有一个分析在跑了，等它出完报告再提交下一份。", 429)
        if len(active) - len([j for j in active if j.get("state") == "running"]) >= QUEUE_MAX:
            raise SlmError("排队的人太多了（前面 %d 份），过一会儿再来。" % QUEUE_MAX, 429)
        jid = base64.b16encode(os.urandom(8)).decode("ascii").lower()
        job = {"id": jid, "state": "queued", "created": time.time(),
               "key_hash": h, "label": key_rec.get("label"),
               "engine": engine,
               "progress": ["已排队（%s），准备解析…" % COST[engine]["label"]],
               "html": None, "error": None, "meta": None}

        def log(msg):
            with _lock:
                job["progress"] = (job.get("progress") or [])[-6:] + [msg]

        job["_log"] = log
        _jobs[jid] = job
        pos = _queue_pos(jid)
    _q.put((jid, raw))
    return {"id": jid, "state": "queued", "queue_pos": pos}


def status(jid: str) -> dict:
    j = _jobs.get((jid or "").strip())
    if not j:
        raise SlmError("这个任务不在了（可能已过期）。", 410)
    out = {"id": j["id"], "state": j["state"], "error": j["error"],
           "progress": j.get("progress") or [], "meta": j.get("meta"),
           "ready": j["state"] == "done"}
    if j["state"] == "queued":
        out["queue_pos"] = _queue_pos(j["id"])
    out["started"] = int(j.get("created") or 0)
    return out


def report(jid: str):
    j = _jobs.get((jid or "").strip())
    if not j or j["state"] != "done" or not j.get("html"):
        raise SlmError("报告还没好（或已过期）。", 409)
    return j["html"], j.get("filename") or "report.html"


# ── 追问（带着报告上下文继续问）──────────────────────────────────────
FOLLOWUP_SYSTEM = (
    "你是「她不一样」的分析师。下面是你刚为这位访客生成的关系分析报告数据。\n"
    "访客会就这份报告追问细节 —— **只依据下面的数据回答**，不要编造、不要泛泛而谈；\n"
    "尽量引用具体证据（时间点、原话、统计数字）。中文回答，直接给结论 + 依据，\n"
    "300 字以内；不要复述整份报告，也不要自我介绍。\n\n"
    "报告数据：\n%s"
)


def _condense_analysis(analysis: dict, limit: int = None) -> str:
    """把报告数据压成一次追问塞得下的上下文：逐键截断、总量封顶。"""
    limit = limit or FOLLOWUP_CHARS
    parts, used = [], 0
    for k, v in (analysis or {}).items():
        if str(k).startswith("_"):
            continue
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if len(s) > 800:
            s = s[:800] + "…"
        piece = "%s: %s" % (k, s)
        if used + len(piece) > limit:
            break
        parts.append(piece)
        used += len(piece)
    return "\n".join(parts) or "（报告数据为空）"


def job_engine(jid: str) -> str:
    """这个任务当初选的引擎（追问要按同一个引擎、同一个价）。"""
    j = _jobs.get((jid or "").strip()) or {}
    return j.get("engine") or DEFAULT_ENGINE


def followup(jid: str, question: str) -> dict:
    """拿着已完成的报告回答一条追问。额度由调用方记账（0.1/条）。"""
    j = _jobs.get((jid or "").strip())
    if not j:
        raise SlmError("这个任务不在了（可能已过期）。", 410)
    if j.get("state") != "done":
        raise SlmError("报告还没出来，等它出完再追问。", 409)
    q = (question or "").strip()
    if not q:
        raise SlmError("问题不能为空。", 400)
    if len(q) > 1000:
        raise SlmError("问题太长了（1000 字以内）。", 400)

    eng = j.get("engine") or DEFAULT_ENGINE
    cli = _llm_for(eng)
    msgs = [{"role": "system",
             "content": FOLLOWUP_SYSTEM % _condense_analysis(j.get("analysis") or {})}]
    for m in (j.get("chat") or [])[-(FOLLOWUP_TURNS * 2):]:
        msgs.append(m)
    msgs.append({"role": "user", "content": q})

    resp = cli._create(model=cli.model_id, messages=msgs, temperature=0.4,
                       max_tokens=FOLLOWUP_MAX_TOKENS, stream=False)
    answer = (resp.choices[0].message.content or "").strip()
    if not answer:
        raise SlmError("模型这次没给出内容，稍后再试。", 502)

    with _lock:
        chat = j.setdefault("chat", [])
        chat.append({"role": "user", "content": q})
        chat.append({"role": "assistant", "content": answer})
        del chat[:-(FOLLOWUP_TURNS * 2)]
    backend = COST[eng]["label"]
    if eng == "local" and cli.last_backend != "primary":
        backend = "%s（本地不可用，自动兜底）" % (cli._fb_model or COST["cloud"]["label"])
    return {"answer": answer, "turns": len(j.get("chat") or []) // 2,
            "backend": cli.last_backend, "engine": eng, "engine_label": backend}
