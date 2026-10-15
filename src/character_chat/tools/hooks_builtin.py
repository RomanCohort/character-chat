"""内置钩子 —— 挂在 permissions 的 PreToolUse / PostToolUse 上。

【为什么单独一个文件】
`permissions.py` 是**机制**（模式、风险表、闸门、注册表）；
这里放**策略**（具体拦什么、告什么）。分开的好处是加钩子不用动机制，
而且一眼能看全「她此刻被挂了哪些约束」。

【为什么不照搬通用安全检查】
翻这台机器的历史，出过的事故有共同的形状，钩子就该对着形状打：

  · 9 个 bridge 实例  → 学长微信里收到 6-9 条重复回复（2026-07-13）
  · 17 个 scheduler 实例
        ↑ 两个都是【重复执行】
  · bridge 静默丢消息         → 【静默失效】
  · fail2ban 能封掉自己的 IP  → 【自伤】

通用安全检查（防注入、防越权）在这套系统里价值有限 ——
shell_access 的正则白名单 + shlex 已经把那条路堵死了。
真正缺的是上面四个形状的护栏。
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from character_chat.tools.permissions import pre_hook, post_hook, resolve_risk

TZ = timezone(timedelta(hours=8))
GUARD_DB = Path(os.environ.get("LING_TOOL_GUARD_DB",
                               "/var/lib/ling-muxue/tool-guard.db"))
AUDIT = Path("/var/log/ling-muxue/hooks.log")

# 重复窗口。取 120s 而不是更长：
#   · 要抓的是「同一个动作在几秒内被重复执行」这种突发（堆实例的形状）
#   · 不能长到把「故意再做一次」也拦掉——隔两分钟再跑同一个命令是正常的
DUP_WINDOW_S = 120

# 只有会改状态/跑命令/有危险的工具才防重复。
# 只读工具（read_file / web_search …）重复调用完全无害，拦了只会添乱——
# 这跟 shell_access「不是拦坏的，是只放行的」是同一种克制。
GUARDED_RISKS = ("write", "exec", "danger")


def _log(msg):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _fingerprint(tool_name, arguments):
    """同一工具 + 相同参数 = 同一个指纹。参数先规范化，避免键序不同就当成两次。"""
    try:
        canon = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    except Exception:
        canon = repr(arguments)
    return "%s|%s" % (tool_name, hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16])


@pre_hook
def block_duplicate_write(tool_name, arguments):
    """PreToolUse：拦住「同一个动作在 2 分钟内被重复执行」。

    为什么必须是**跨进程**的：那两个事故的重复都来自**多个进程**各做一次
    （9 个 bridge 各处理同一条微信消息），不是单个进程里循环调两次。
    所以状态不能放进程内存，得放共享存储。这里用 sqlite：
    标准库自带、并发安全、不会像 JSON 那样写坏。
    """
    # 用 resolve_risk（动作级覆盖优先），不要直接读 TOOL_RISK ——
    # 否则 website_manage 的读 action 会被当成写来防重，误伤只读用法。
    if resolve_risk(tool_name, arguments) not in GUARDED_RISKS:
        return None

    key = _fingerprint(tool_name, arguments)
    now = time.time()
    seen = None
    try:
        GUARD_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(GUARD_DB), timeout=5)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, ts REAL)")
            row = con.execute("SELECT ts FROM calls WHERE key = ?", (key,)).fetchone()
            seen = row[0] if row else None
            con.execute("INSERT OR REPLACE INTO calls (key, ts) VALUES (?, ?)", (key, now))
            con.execute("DELETE FROM calls WHERE ts < ?", (now - DUP_WINDOW_S,))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        # 护栏自己坏了不能变成「全部放行」也不能变成「全部拒绝」——
        # 宁可放过这一次（记一笔），也不要因为磁盘/权限问题让她彻底不能干活。
        _log("护栏异常，本次放行: %s: %s" % (type(e).__name__, e))
        return None

    if seen is not None and (now - seen) < DUP_WINDOW_S:
        gap = now - seen
        _log("拦下重复: %s (%.0fs 前刚做过) args=%s"
             % (tool_name, gap, json.dumps(arguments or {}, ensure_ascii=False)[:120]))
        return ("这个动作 %.0f 秒前刚做过一次（`%s`，参数完全一样），所以我没再执行。\n"
                "重复执行是这套系统出过事故的地方（曾经堆出 9 个 bridge 实例，"
                "学长的微信里收到 6-9 条重复回复）。\n"
                "如果你确实要再做一次，跟学长说一声，让他确认。" % (gap, tool_name))
    return None


# ═══════════════════════════════════════════════════════════════════════
# 防自伤
# ═══════════════════════════════════════════════════════════════════════
#
# 【为什么先堵「切断自己的生命线」而不是「把自己关在门外」】
# 两种自伤的兜底能力不对称：
#   · 把自己关在门外（封掉自己 IP）—— 有兜底：学长能去阿里云控制台 VNC 救
#   · 切断生命线（停掉 character_chat / bridge）—— **没有兜底**：
#     她一旦停掉自己，连"求救"这条路都断了，而且 ling-bridge 有
#     Requires=ling-character-chat.service，停一个会连带带走另一个。
# 所以先堵没有兜底的那个。

# 停掉就等于自伤 —— 这两条是她的命
LIFELINE = ("ling-character-chat", "ling-bridge")
# 停掉会让她对外失能（网站/代理没了），但不会弄死自己
USER_VISIBLE = ("nginx", "xray", "ling-scheduler")
# 停掉等于卸掉防护
SECURITY = ("fail2ban", "egress-filter")

_SYSTEMCTL_RE = r"^systemctl\s+(?P<verb>status|is-active|is-enabled|restart|reload|start|stop)\s+(?P<svc>\S+)$"


@pre_hook
def block_self_harm(tool_name, arguments):
    """PreToolUse：拦住会切断自己生命线 / 拆掉防护的动作。

    注意这里拦的是 **stop 和 restart**，不是所有 systemctl：
      · stop    —— 停掉了就没人把它拉回来（supervisor 只重启 bridge，
                  不负责 start 一个被 stop 的 character_chat）
      · restart —— 会掐断正在处理的这一轮；character_chat 重启还会因
                   Requires= 连带重启 bridge，等于自己把自己打断
      · status / is-active / is-enabled —— 纯只读，照常放行
    """
    if tool_name != "run_command":
        return None
    cmd = (arguments or {}).get("cmd", "")
    m = re.match(_SYSTEMCTL_RE, cmd.strip())
    if not m:
        return None
    verb, svc = m.group("verb"), m.group("svc")
    if verb in ("status", "is-active", "is-enabled"):
        return None

    if svc in LIFELINE:
        _log("拦下自伤: %s" % cmd)
        return ("这条我不能跑 —— `%s` 是我的生命线（%s）。\n"
                "停掉它我就没法回你消息了，而且 ling-bridge 有 "
                "Requires=ling-character-chat.service，停一个会连带把另一个带走。\n"
                "重启也一样：会掐断我正在处理的这一轮。bridge 的重启现在由 "
                "bridge-supervisor 盯着（它看轮询游标的 mtime），不需要手动来。\n"
                "真需要动的话，请学长在服务器上执行。" % (svc, "character_chat" if svc == "ling-character-chat" else "微信桥"))

    if verb == "stop" and svc in USER_VISIBLE:
        _log("拦下 stop: %s" % cmd)
        return ("`systemctl stop %s` 我先不跑 —— 停掉会让学长那边看到的东西直接没了"
                "（网站/代理/主动消息任一个）。\n"
                "如果它是卡住了，`restart` 更合适；确实要停的话请学长确认。" % svc)

    if verb == "stop" and svc in SECURITY:
        _log("拦下 stop: %s" % cmd)
        return ("`systemctl stop %s` 我先不跑 —— 那是在把服务器身上的防护卸掉。\n"
                "如果它误封了谁，用 ip_action 解封具体那个 IP，别整个停掉。" % svc)

    if verb == "stop" and svc.endswith(".timer"):
        _log("拦下 stop: %s" % cmd)
        return ("`%s` 是个定时器。停掉它等于让那件定期的事从此不再发生"
                "（而且不会有任何报错提醒）。要临时不做的话告诉学长。" % svc)

    return None


@pre_hook
def block_self_lockout(tool_name, arguments):
    """PreToolUse：拦住「封掉一个正连着这台机器的 IP」。

    形态一的兜底是阿里云控制台，比形态二好救，所以放在后面。
    但有一条**可以精确判定**的危险：封掉的 IP 上正挂着 SSH 会话 —— 那基本就是学长本人。

    判断依据用实际连接，不用猜：
    permission 层已把 ip_action 划成 danger（default 下不放行），这里是第二道。
    """
    if tool_name != "ip_action":
        return None
    action = str((arguments or {}).get("action", "")).lower()
    ip = str((arguments or {}).get("ip", "")).strip()
    if action not in ("ban", "block") or not ip:
        return None

    live = _live_ssh_peers()
    if ip in live:
        _log("拦下自锁: ip_action %s %s（该 IP 有活跃 SSH 会话）" % (action, ip))
        return ("`%s` 上有正在建立的 SSH 连接 —— 那很可能就是学长本人。\n"
                "封掉它会把学长关在门外。真要是恶意 IP，请学长自己确认后再封。" % ip)
    return None


# ═══════════════════════════════════════════════════════════════════════
# 调用可观测
# ═══════════════════════════════════════════════════════════════════════
#
# 【为什么值得单独记】
# 改之前权限审计只有一行 `OK <tool>`。出了事回答不了三个问题：
#   · 跑了多久      —— 慢的调用会拖住整轮对话
#   · 返回了多大    —— 调用方会截断，截断之后她看到的就残缺了
#   · 是不是出错    —— 工具返回错误串时她可能照常往下说
#
# 【只记账，不告警】（按学长 2026-10-07 的决定）
# 「慢」和「被截断」在这套系统里是**常态**：website_manage 每次都要重新
# 生成整站、read_logs 每次都会碰到长日志。现在挂告警只会淹掉真正的异常。
# 先攒几天基线，再回来决定「多少才算异常」。

CALL_LOG = Path(os.environ.get("LING_TOOL_CALL_LOG",
                               "/var/log/ling-muxue/tool-calls.log"))

# 调用方（llm/client.py）的截断线：`if len(result) > 1500: result = result[:1500] + "…(截断)"`
# 那个 1500 是硬编码在调用方里的字面量，不是常量，所以这里只能跟着抄一份。
# 一旦那边改了，这里会漂移——所以只拿它标「有没有被截断」这个粗判断，
# 不做精确边界断言。真要改就两边一起改。
CLIENT_TRUNCATE_CHARS = 1500

# 判定「工具其实失败了」的标记。工具约定是返回字符串，失败用括号包起来，
# 所以这里只能靠前缀特征认——**这是启发式，不是契约**。
# 认不出的失败会被记成 ok，这是已知的漏报面。
#
# 标记取的是**词干**不是整句：各工具写法不一致，早期版本写成 "出错：" 就漏掉了
# web_search 的 "搜索出错了："（含 "出错了" 不含 "出错："）。实测踩过。
_ERROR_MARKERS = ("（没执行）", "命令被拒", "命令超时", "执行失败", "出错", "失败")


@post_hook
def log_tool_call(tool_name, arguments, result, elapsed=None):
    """PostToolUse：把每次工具调用的实际表现记一行。只观察、不干涉。"""
    try:
        try:
            in_bytes = len(json.dumps(arguments or {}, ensure_ascii=False).encode("utf-8"))
        except Exception:
            in_bytes = -1

        if isinstance(result, str):
            out_chars = len(result)
            head = result[:80]
        else:
            out_chars = len(str(result))
            head = str(result)[:80]

        flags = []
        if out_chars > CLIENT_TRUNCATE_CHARS:
            flags.append("TRUNCATED>%d" % CLIENT_TRUNCATE_CHARS)   # 她看到的是残缺的
        if any(m in head for m in _ERROR_MARKERS):
            flags.append("ERR")

        dur = "  ?  " if elapsed is None else "%6.2fs" % elapsed
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write("%s  %-18s %s  in=%dB out=%dc %s\n" % (
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), tool_name,
                dur, in_bytes, out_chars, " ".join(flags) or "ok"))
    except Exception:
        # 观测钩子绝不能因为自己出错而影响工具调用本身
        pass


# ═══════════════════════════════════════════════════════════════════════
# 危险动作告警
# ═══════════════════════════════════════════════════════════════════════
#
# 【粒度：只报「对外可见」的改动】（2026-10-07 学长定的）
# 不是所有写类动作都报 —— 她一天能跑几十次工具调用，`restart xray` 也报
# 会把人烦到直接关通知，那这个钩子就等于没有。
# 划的这条线是：**改成学长能直接看到的东西**（网站 / 代码镜像 / 对外服务的起停）。
# 服务器内部的改动不报，留在 tool-calls.log 和 permission.log 里，需要时能翻。
#
# 跟 opcheck 的分工：opcheck 管「服务器本身有没有毛病」，
# 这个管「她动了什么你能看见的东西」。两者不重叠。

NTFY_URL = os.environ.get("LING_NTFY_URL", "http://127.0.0.1:2586/sg-tunnel")
NTFY_TOKEN_FILE = Path("/etc/opcheck/ntfy.token")

# website_manage 的四个 action 里，只有这两个是写
WEBSITE_WRITE_ACTIONS = ("edit_section", "regenerate")
# 对外可见的服务（起停它们 = 学长能直接感知）
VISIBLE_SERVICES = ("nginx", "xray")

_SYSTEMCTL_ACT_RE = r"^systemctl\s+(?P<verb>restart|reload|start|stop)\s+(?P<svc>\S+)$"


def _push_ntfy(title, body):
    """推一条 ntfy。失败只记日志，绝不抛——告警是副业，不能影响工具调用本身。"""
    try:
        token = NTFY_TOKEN_FILE.read_text(encoding="utf-8").strip()
        # 标题走 HTTP 头，头默认是 latin-1，中文塞不进去。所以标题保持 ASCII，
        # 中文放正文第一行——宁可在正文里重复一次，也不要推出一条乱码头。
        req = urllib.request.Request(
            NTFY_URL, data=body.encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Title": title.encode("ascii", "ignore").decode() or "ling-muxue",
                     "Tags": "pencil"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as e:
        _log("可见变更告警推送失败: %s: %s" % (type(e).__name__, e))
        return False


@post_hook
def alert_visible_change(tool_name, arguments, result, elapsed=None):
    """PostToolUse：她动了「对外可见」的东西时，推一条给学长。"""
    args = arguments or {}
    what = None

    if tool_name == "website_manage":
        action = str(args.get("action", ""))
        # 用 resolve_risk 判「是不是写」，跟权限闸/防重钩子同一套判据。
        if action in WEBSITE_WRITE_ACTIONS and resolve_risk(tool_name, args) == "write":
            sec = str(args.get("section", "") or "")
            fld = str(args.get("field", "") or "")
            what = "改了网站"
            if sec or fld:
                what += "（%s）" % ".".join(x for x in (sec, fld) if x)
            if action == "regenerate":
                what = "重新生成了整站"

    elif tool_name == "mirror_update":
        repo = str(args.get("repo", "") or "")
        what = "更新了代码镜像" + ("（%s）" % repo if repo else "（全量）")

    elif tool_name == "run_command":
        m = re.match(_SYSTEMCTL_ACT_RE, str(args.get("cmd", "")).strip())
        if m and m.group("svc") in VISIBLE_SERVICES:
            what = "动了对外服务：systemctl %s %s" % (m.group("verb"), m.group("svc"))

    if not what:
        return

    stamp = datetime.now(TZ).strftime("%m-%d %H:%M")
    _log("可见变更: %s" % what)
    _push_ntfy("Ling Muxue changed something visible",
               "学长，我刚动了对外可见的东西：\n\n· %s\n\n"
               "时间 %s。如果你没预期到这次改动，可以看一眼。\n"
               "（服务器内部的改动不在这里报，需要时翻 tool-calls.log）" % (what, stamp))


def _live_ssh_peers():
    """当前正连着 22 端口的对端 IP 集合。读不到就返回空集（宁可少拦，别误拦）。"""
    peers = set()
    try:
        out = subprocess.run(["ss", "-tn", "state", "established"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=10).stdout
    except Exception:
        return peers
    for line in out.splitlines():
        # 数据行长这样（只有 4 列）：
        #   0      0      172.20.57.114:22      119.185.221.138:54794
        #   [0]    [1]    [2]=Local:Port        [3]=Peer:Port
        # 表头行是 6 列（Recv-Q Send-Q Local Address:Port Peer Address:Port），
        # 下面的 endswith(':22') 自然把它筛掉。
        parts = line.split()
        if len(parts) < 4:
            continue
        local, peer = parts[2], parts[3]
        if local.endswith(":22") and ":" in peer:
            peers.add(peer.rsplit(":", 1)[0])
    return peers
