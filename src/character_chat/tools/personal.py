"""个人事务工具 — 提醒 / 待办 / 网站统计。

【为什么是这三个】
  remind      她有两半能力（scheduler 定时 + weixin_sender 发送），偏偏没有
              "学长让我记着的事"这个数据结构。拼起来她就真能到点来敲门。
  todo        昨天她说"这就加"却没做——根因是她脑子里没有"未完成的事"。
              有了待办，"答应过但没做"这件事会**存在**，主动消息时她自己能看见。
  site_stats  她管着网站却看不到效果。最后那条跟防御连着：她能自己发现
              "今天有个 IP 在疯狂撞 .env"，然后走 ip_action 封掉。

【落盘位置】/var/lib/ling/ —— scheduler 每轮会读 reminders 和 todos。
"""
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

LIB_DIR = Path(os.environ.get("LING_LIB_DIR", "/var/lib/ling"))
REMINDERS = LIB_DIR / "reminders.json"
TODOS = LIB_DIR / "todos.json"
ACCESS_LOG = Path("/var/log/nginx/access.log")

TZ = timezone(timedelta(hours=8))          # Asia/Singapore == UTC+8
MAX_ITEMS = 200
MAX_STATS_LINES = 200000                   # access.log 最多扫这么多行


# ── 工具 schema ─────────────────────────────────────────────────────────
REMIND_TOOL = {
    "type": "function",
    "function": {
        "name": "remind",
        "description": (
            "给学长设一个定时提醒。到点后会由调度器主动发微信给他。"
            "\n【必须自己算好时间】when 要写成绝对时间（ISO 8601，带时区），"
            "比如「11月15号投稿、提前三天」→ when='2026-11-12T20:00:00+08:00'。"
            "\n如果涉及日期计算，先按当前时间算清楚再填，不要直接把相对说法塞进 when。"
            "\n删除提醒用 action=remove + id。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "remove"], "description": "默认 add"},
                "when": {"type": "string", "description": "提醒时间，ISO 8601 带时区，如 2026-11-12T20:00:00+08:00"},
                "what": {"type": "string", "description": "提醒什么事（会被写进发给他的消息里）"},
                "id": {"type": "string", "description": "action=remove 时的提醒 id"},
            },
            "required": ["action"],
        },
    },
}

TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "待办清单。**凡是你答应学长要做的事，都要先落到这里** —— 答应完就忘是最糟的。"
            "\n用法：action=add（加一条）/ list（看还没做的）/ done（标记完成，给 id）。"
            "\n主动消息时如果清单里有没做完的，可以主动提一句。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "done"], "description": "默认 list"},
                "text": {"type": "string", "description": "待办内容（action=add 时必填）"},
                "id": {"type": "string", "description": "action=done 时给 id"},
            },
            "required": ["action"],
        },
    },
}

SITE_STATS_TOOL = {
    "type": "function",
    "function": {
        "name": "site_stats",
        "description": (
            "看网站 www.romancohort.cloud 的访问情况：总请求、独立 IP、热门页面、"
            "可疑扫描（撞 .env/.git/wp-login 的），以及最活跃的来源。"
            "学长问「网站有人看吗」「最近有没有异常访问」时用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "看最近几天，默认 1，最多 7"},
            },
            "required": [],
        },
    },
}


# ── 存储 ────────────────────────────────────────────────────────────────
def _load(path: Path, key: str) -> dict:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return {key: []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        d.setdefault(key, [])
        return d
    except Exception as e:
        logger.warning(f"[personal] 读取 {path} 失败: {e}")
        return {key: []}


def _save(path: Path, data: dict):
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now() -> datetime:
    return datetime.now(TZ)


# ── remind ──────────────────────────────────────────────────────────────
def remind(action: str = "add", when: str = "", what: str = "", id: str = "") -> str:
    data = _load(REMINDERS, "reminders")
    items = data["reminders"]

    if action == "list" or (action == "add" and not what):
        pend = [r for r in items if not r.get("fired")]
        if not pend:
            return "(目前没有待发的提醒)"
        pend.sort(key=lambda r: r.get("when", ""))
        lines = [f"待发提醒 {len(pend)} 条："]
        for r in pend[:20]:
            lines.append(f"  [{r.get('id')}] {r.get('when')}  {r.get('what')}")
        return "\n".join(lines)

    if action == "remove":
        before = len(items)
        data["reminders"] = [r for r in items if r.get("id") != id]
        _save(REMINDERS, data)
        return f"删了 {before - len(data['reminders'])} 条（id={id}）"

    # add
    if not when or not what:
        return "(add 需要 when 和 what 都给。when 要 ISO 8601 带时区，如 2026-11-12T20:00:00+08:00)"
    try:
        dt = datetime.fromisoformat(when)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
    except Exception:
        return f"(when 格式不对：{when!r}。要 ISO 8601，如 2026-11-12T20:00:00+08:00)"

    if dt < _now():
        return f"(这个时间已经过去了：{when}。现在服务器时间是 {_now().isoformat(timespec='seconds')}，算一下再填)"

    rid = f"r{int(_now().timestamp())}"
    items.append({
        "id": rid, "when": dt.isoformat(), "what": what,
        "created": _now().isoformat(), "fired": False,
    })
    if len(items) > MAX_ITEMS:
        data["reminders"] = items[-MAX_ITEMS:]
    _save(REMINDERS, data)

    delta = dt - _now()
    hrs = delta.total_seconds() / 3600
    eta = f"{hrs:.1f} 小时后" if hrs < 48 else f"{hrs/24:.1f} 天后"
    return (f"设好了 [{rid}]：{dt.strftime('%Y-%m-%d %H:%M')} 提醒「{what}」（{eta}）\n"
            f"到点我会主动发微信给他。当前服务器时间 {_now().strftime('%Y-%m-%d %H:%M %Z')}")


# ── todo ────────────────────────────────────────────────────────────────
def todo(action: str = "list", text: str = "", id: str = "") -> str:
    data = _load(TODOS, "todos")
    items = data["todos"]

    if action == "add":
        if not text:
            return "(add 需要 text)"
        tid = f"t{int(_now().timestamp())}"
        items.append({"id": tid, "text": text, "done": False, "created": _now().isoformat()})
        if len(items) > MAX_ITEMS:
            data["todos"] = items[-MAX_ITEMS:]
        _save(TODOS, data)
        return f"记下了 [{tid}]：{text}"

    if action == "done":
        hit = [t for t in items if t.get("id") == id]
        if not hit:
            return f"(没有 id={id} 的待办)"
        hit[0]["done"] = True
        hit[0]["done_at"] = _now().isoformat()
        _save(TODOS, data)
        return f"标记完成：{hit[0]['text']}"

    pend = [t for t in items if not t.get("done")]
    if not pend:
        return "(待办清空了)"
    lines = [f"没做完的 {len(pend)} 条："]
    for t in pend[:20]:
        lines.append(f"  [{t.get('id')}] {t.get('text')}")
    return "\n".join(lines)


# ── site_stats ──────────────────────────────────────────────────────────
_SCAN_RE = re.compile(r"\.(env|git|aws|ssh|DS_Store)|wp-login|wp-admin|phpmyadmin|xmlrpc\.php", re.I)
_LOG_RE = re.compile(r'^(\S+) - - \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "([^"]*)" "([^"]*)"')


def site_stats(days: int = 1) -> str:
    if not ACCESS_LOG.exists():
        return "(拿不到 nginx access.log)"
    n = max(1, min(int(days or 1), 7))
    cutoff = _now() - timedelta(days=n)

    total = 0
    ips = Counter()
    paths = Counter()
    scans = Counter()
    uas = Counter()
    status = Counter()

    try:
        with open(ACCESS_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-MAX_STATS_LINES:]
    except Exception as e:
        return f"(读日志失败：{e})"

    for ln in lines:
        m = _LOG_RE.match(ln)
        if not m:
            continue
        ip, ts, method, path, code, _, ua = m.groups()
        try:
            t = datetime.strptime(ts.split()[0], "%d/%b/%Y:%H:%M:%S")
            t = t.replace(tzinfo=TZ)
        except Exception:
            continue
        if t < cutoff:
            continue
        total += 1
        ips[ip] += 1
        paths[path.split("?")[0]] += 1
        status[code] += 1
        uas[ua[:60]] += 1
        if _SCAN_RE.search(path):
            scans[ip] += 1

    if not total:
        return f"(最近 {n} 天没有访问记录)"

    out = [f"网站最近 {n} 天：{total} 次请求，{len(ips)} 个独立 IP"]
    out.append("状态码：" + "  ".join(f"{c}×{k}" for c, k in status.most_common(5)))
    out.append("")
    out.append("最活跃来源：")
    for ip, c in ips.most_common(5):
        out.append(f"  {ip}  {c} 次")
    out.append("")
    out.append("热门页面：")
    for p, c in paths.most_common(6):
        out.append(f"  {c:>5}  {p[:70]}")
    if scans:
        out.append("")
        out.append(f"⚠️ 扫描行为：{len(scans)} 个 IP 撞敏感路径")
        for ip, c in scans.most_common(5):
            out.append(f"  {ip}  {c} 次")
        out.append("（可以用 ip_action 封掉，记得先跟学长说理由）")
    return "\n".join(out)
