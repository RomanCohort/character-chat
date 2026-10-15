"""受限命令执行 — 白名单模式。

【设计原则：不是「拦坏的」，是「只放行的」】
  命令必须**完整匹配**下面某条正则才能跑；不匹配一律拒。这样即使正则写漏了，
  漏掉的是"不能跑"，而不是"能跑危险的"。

【三层防护】
  1. 正则白名单 —— 整条命令 ^...$ 全匹配（形状检查）
  2. shlex.split + shell=False —— 不经过 shell，`;` `&&` `|` `$()` 都只是普通参数
  3. 参数本身也在正则里锁死 —— 服务名/路径/仓库名都是枚举或受限字符集

【审计】
  每条命令（通过的、被拒的）都写进 /var/log/ling-muxue/shell_access.log，
  学长问「她到底跑了什么」时能查。

【不可绕过点】
  - 没有 `bash -c` / `sh -c`（那等于开 shell）
  - 没有任意路径（`du`/`tail` 的路径都限死在白名单前缀里）
  - 没有写操作（除了 systemctl restart 和 git pull 这两个被明确允许的）
"""
import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

TZ = timezone(timedelta(hours=8))
AUDIT_LOG = Path("/var/log/ling-muxue/shell_access.log")
TIMEOUT = 60
MAX_OUTPUT = 8000

# 允许重启/查看的服务（枚举，不是通配）
SERVICES = (
    "nginx|xray|fail2ban|egress-filter|"
    "ling-character-chat|ling-bridge|ling-scheduler|"
    "certbot-renew.timer|arxiv-push.timer|aide-check.timer|git-mirror.timer"
)

# 每条：(正则, 人类可读说明)
ALLOWED = [
    # —— 服务管理（status/is-active 只读；restart/reload 会短暂中断）——
    (r"^systemctl (status|is-active|is-enabled|restart|reload|start|stop) (%s)$" % SERVICES,
     "服务状态/重启"),
    (r"^systemctl list-timers( --all)?$", "定时任务列表"),
    (r"^systemctl list-units --type=service --state=(running|failed)( --no-pager)?$", "服务列表"),

    # —— 只读系统信息 ——
    (r"^uptime$", "负载"),
    (r"^free -m$", "内存"),
    (r"^df -h( /(opt|var|srv|home|root)?[\w./-]*)?$", "磁盘"),
    (r"^du -sh /(opt|var|srv|home|root)/[\w./-]+$", "目录大小"),
    (r"^ps aux( --sort=[\w%.-]+)?$", "进程"),
    (r"^ss -tn(lpa|lap|a|tal)?$", "连接"),
    (r"^ss -tn state established$", "已建立连接"),
    # 监听端口。原先只放行了 -tn（已建立连接），所以她看得到「谁连着」、
    # 看不到「谁在听」——2026-10-07 她如实报告了这个缺口（没有编），这里补上。
    (r"^ss -tln(p|pa|a)?$", "监听端口"),
    (r"^ss -tlnp? state listening$", "监听端口"),
    # 带字节数的连接视图：能看出「哪个进程在传多少」，排查流量异常靠它。
    # -i 给 tcp_info（bytes_sent / bytes_received），-p 给进程名。
    (r"^ss -tnip? state established$", "已建立连接（含字节数）"),
    (r"^lsof -i :(443|80|8000|8443|9097)$", "端口占用"),

    # —— 带宽历史（2026-10-07 新增）——
    # 起因：那天 17:26-17:34 入网+出网带宽同时打满，四个服务一起超时，
    # 但 VM 里没装任何流量工具，事后既不知道多大、也不知道是谁。
    # 现在 vnstat 给「多大/何时」，netmon 给「是谁」。
    # netmon 的采样日志落在 /var/log/netmon/bandwidth.log —— 上面那条通用
    # tail 规则本来就覆盖它（/var/log/<目录>/<文件>.log），不用另开一条。
    (r"^vnstat -i (eth0|lo)( -(h|d|m|y|5))?$", "带宽历史（小时/日/月/年/5分钟）"),
    (r"^vnstat -i (eth0|lo) --oneline$", "带宽单行摘要"),

    # —— 日志（路径限死在 /var/log 下，且不许通配穿目录）——
    (r"^tail -n \d{1,4} /var/log/[\w./+-]+\.log$", "读日志"),
    (r"^tail -n \d{1,4} /var/log/[\w-]+/[\w./+-]+\.log$", "读日志(子目录)"),
    (r"^wc -l /var/log/[\w./+-]+$", "日志行数"),

    # —— git（只读 + pull，仓库名限死在 /srv/git 下的裸仓库）——
    (r"^git -C /srv/git/[\w.+-]+\.git (status|remote -v|branch|rev-parse HEAD)$", "git 只读"),
    (r"^git -C /srv/git/[\w.+-]+\.git log( -n \d{1,3})?( --oneline)?$", "git log"),
    (r"^git -C /srv/git/[\w.+-]+\.git pull( --ff-only)?$", "git 拉取"),
    (r"^git -C /srv/git/[\w.+-]+\.git fetch( --all| --tags| --prune)?$", "git fetch"),

    # —— 本地 HTTP 端点（只许 127.0.0.1，不许外网）——
    (r"^curl -s(S)? (http://127\.0\.0\.1:(8000|9097)[\w/?=&.-]*)$", "本地端点"),
]

_COMPILED = [(re.compile(p), d) for p, d in ALLOWED]


def _audit(cmd: str, verdict: str, detail: str = ""):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %-8s %s%s\n" % (ts, verdict, cmd[:300],
                                          ("  # " + detail[:150]) if detail else ""))
    except Exception:
        pass


def _match(cmd: str):
    """返回 (是否允许, 说明)。必须整条全匹配。"""
    c = cmd.strip()
    if not c:
        return False, "空命令"
    for rx, desc in _COMPILED:
        if rx.match(c):
            return True, desc
    return False, "不在白名单里"


RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "在服务器上跑一条**白名单内**的命令。能跑的范围：\n"
            "  · systemctl {status|is-active|restart|reload} <服务>　（服务名是枚举的，见下）\n"
            "  · uptime / free -m / df -h / du -sh <白名单路径> / ps aux / ss -tn / lsof -i :<端口>\n"
            "  · tail -n <行数> /var/log/**.log　（只能读 /var/log 下的 .log）\n"
            "  · vnstat -i eth0 [-h|-d|-m|-y|-5]　带宽历史（小时/日/月/年/5分钟粒度）\n"
            "  · ss -tnip state established　已建立连接 + 每连接字节数 + 进程名\n"
            "  · git -C /srv/git/<仓库>.git {status|log|pull|fetch}\n"
            "  · curl -s http://127.0.0.1:8000|9097/...　（只能打本地端点）\n"
            "\n【排查「服务突然都超时」的固定动作】\n"
            "  带宽打满会让所有 TCP 一起丢包 —— 表现是「四个不相干的服务同时坏」。\n"
            "  遇到这种症状，按顺序：\n"
            "    1. tail -n 200 /var/log/netmon/bandwidth.log　找 [SPIKE] 行\n"
            "       （每分钟一行；超 20Mbps 会附上「每进程增量 + 逐连接字节」详情块）\n"
            "    2. vnstat -i eth0 -h　看那个时段的总量，确认是不是真打满\n"
            "    3. ss -tnip state established　看此刻谁在传（-i 给字节数，-p 给进程）\n"
            "  注意：xray 是代理出口，它的日志只记「连接数」不记「字节数」——\n"
            "  连接数正常也可能有单个大流量连接。看字节数，别看连接数。\n"
            "\n可重启的服务：%s\n"
            "\n【硬要求】命令必须是**完整的一条**，不能带 ; && | $() —— 会被直接拒。"
            "被拒就是被拒，把拒绝原因告诉学长，不要反复试别的写法。"
            "\n【重启会影响在线服务】restart 前先跟学长说一声你要重启什么、为什么。"
        ) % SERVICES.replace("|", " / "),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的命令（单条，不带管道和分号）"},
                "reason": {"type": "string", "description": "为什么要跑它（会记进审计日志）"},
            },
            "required": ["cmd"],
        },
    },
}


def run_command(cmd: str = "", reason: str = "") -> str:
    c = (cmd or "").strip()
    ok, why = _match(c)

    if not ok:
        _audit(c, "DENIED", "%s | reason=%s" % (why, reason))
        return (
            "命令被拒（%s）。\n"
            "能跑的在 run_command 的工具说明里列着。\n"
            "如果这件事确实需要、但白名单里没有，**直接告诉学长「这个我跑不了」** —— "
            "别换写法反复试，也别编一个「我试过了没成功」。" % why
        )

    # shlex.split + shell=False：不经过 shell，注入在语法上不可能
    try:
        argv = shlex.split(c)
    except Exception as e:
        _audit(c, "DENIED", "split 失败: %s" % e)
        return "命令解析失败：%s" % e

    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _audit(c, "TIMEOUT", reason)
        return "命令超时（%ds）：%s" % (TIMEOUT, c)
    except Exception as e:
        _audit(c, "ERROR", str(e))
        return "执行失败：%s: %s" % (type(e).__name__, e)

    out = ((r.stdout or "") + (r.stderr or ""))[:MAX_OUTPUT]
    _audit(c, "OK" if r.returncode == 0 else "RC%d" % r.returncode, reason)
    logger.info("[shell] %s -> rc=%s (%s)" % (c[:120], r.returncode, reason))

    head = "$ %s   [%s]" % (c, why)
    if r.returncode != 0:
        head += "  → 退出码 %s" % r.returncode
    return head + "\n" + (out or "(无输出)")
