"""服务器运维工具 — 只读 + 防御动作。

【权限边界（用户 2026-10-07 确认）】
  能做的：看系统状态 / 查日志 / 看安全态势 / 封解 IP / 读代码 / 搜代码
  碰不到的：改配置 / 重启服务 / 任意 shell

【四条护栏】
  1. 不给任意 shell —— 所有操作都是预定义动作，参数走白名单/格式校验
  2. 路径校验 —— realpath 后必须落在白名单根目录内（防 ../ 穿越）
  3. 全部带超时 —— subprocess timeout，防卡死
  4. 输出截断 —— 限行数/字节，防 token 爆炸

封 IP 的策略（用户选 A）：允许直接封，但**必须在回复里先说明理由**。
"""
import ipaddress
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger

# ── 白名单 ──────────────────────────────────────────────────────────────
READABLE_ROOTS = [
    "/opt/character_chat/src",
    "/opt/ling-muxue-daemon",
    "/opt/arxiv-push",
    "/opt/sitegen",
    "/opt/wechat-bridge",
    "/var/www/site",
    "/etc/nginx",
    "/etc/systemd/system",
    "/var/log",
]

LOG_SOURCES = {
    "nginx":      ["/var/log/nginx/access.log", "/var/log/nginx/error.log"],
    "nginx-access": ["/var/log/nginx/access.log"],
    "nginx-error":  ["/var/log/nginx/error.log"],
    "ling":       ["journalctl:-u", "ling-character-chat", "-u", "ling-bridge", "-u", "ling-scheduler"],
    "scheduler":  ["journalctl:-u", "ling-scheduler"],
    "arxiv":      ["journalctl:-u", "arxiv-push"],
    "xray":       ["journalctl:-u", "xray"],
    "fail2ban":   ["/var/log/fail2ban.log"],
    "auth":       ["journalctl:-u", "sshd"],
    "syslog":     ["journalctl:-n"],
}

MAX_LINES = 200
MAX_BYTES = 20000
TIMEOUT = 15

# ── 工具 schema ─────────────────────────────────────────────────────────
SYSTEM_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "system_status",
        "description": (
            "查看服务器当前状态：负载、内存、磁盘、各服务是否在跑，以及安全态势"
            "（fail2ban 封禁列表、当前连接数、最近失败登录）。"
            "学长问「服务器怎么样」「有什么异常吗」时调用。只读，安全。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

READ_LOGS_TOOL = {
    "type": "function",
    "function": {
        "name": "read_logs",
        "description": (
            "读服务器日志。可用于排查报错、看谁在访问、找异常请求。"
            "日志源白名单：nginx / nginx-access / nginx-error / ling（凌暮雪自己的三个服务）"
            "/ scheduler / arxiv / xray / fail2ban / auth（SSH）/ syslog。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "日志源，见上"},
                "pattern": {"type": "string", "description": "可选过滤关键词（grep），不填则取最近若干行"},
                "lines": {"type": "integer", "description": "返回行数，默认 50，最多 200"},
            },
            "required": ["source"],
        },
    },
}

IP_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "ip_action",
        "description": (
            "封禁或解封一个 IP（走 fail2ban）。用法：action=ban 或 unban，给 ip 和 reason。"
            "\n【硬要求】封之前**必须在回复里说明理由**（为什么封它、有什么证据）。"
            "绝对不能静默封禁 —— 学长要能看见你做了什么。"
            "\n⚠️ 不要封这些：127.0.0.1、10./172.16./192.168. 内网段、以及学长当前的出口 IP。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["ban", "unban"]},
                "ip": {"type": "string", "description": "IPv4 地址"},
                "jail": {"type": "string", "enum": ["sshd", "nginx-scan"],
                         "description": "哪个 jail：sshd（SSH 爆破）或 nginx-scan（扫 .env/.git 等）"},
                "reason": {"type": "string", "description": "封禁理由（会写进日志，也会在回复里说明）"},
            },
            "required": ["action", "ip"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读服务器上一个文件的代码/内容。可读目录白名单："
            "/opt/character_chat/src（凌暮雪自己的代码）、/opt/ling-muxue-daemon、/opt/arxiv-push、"
            "/opt/sitegen（网站生成器）、/var/www/site（网站）、/etc/nginx、/etc/systemd/system、/var/log。"
            "\n学长让你看代码、问你某段实现时用。看不到的文件要如实说看不到，不要编。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件的绝对路径"},
                "start": {"type": "integer", "description": "起始行（1 起），默认 1"},
                "lines": {"type": "integer", "description": "读多少行，默认 200，最多 500"},
            },
            "required": ["path"],
        },
    },
}

SEARCH_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": (
            "在白名单目录里搜代码/文本（grep）。用于找某个函数在哪、某个报错文本出现在哪。"
            "scope 可选：opt / sitegen / website / daemon / cc / repos（代码镜像）。"
            "不填则搜全部白名单目录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索关键词或正则"},
                "scope": {"type": "string", "enum": ["all", "opt", "sitegen", "website", "daemon", "cc", "repos"]},
                "limit": {"type": "integer", "description": "最多返回多少条命中，默认 30，最多 100"},
            },
            "required": ["pattern"],
        },
    },
}

SCOPE_ROOTS = {
    "all": READABLE_ROOTS,
    "opt": ["/opt/character_chat/src", "/opt/ling-muxue-daemon", "/opt/arxiv-push", "/opt/sitegen", "/opt/wechat-bridge"],
    "sitegen": ["/opt/sitegen"],
    "website": ["/var/www/site"],
    "daemon": ["/opt/ling-muxue-daemon"],
    "cc": ["/opt/character_chat/src"],
    # repos: blob/*.html is HTML-wrapped source; read_file strips tags
    "repos": ["/var/www/site/repos"],
}


# ── 护栏 ────────────────────────────────────────────────────────────────
def _check_ip(s: str):
    """校验 IP，拒绝内网/回环/广播。返回 ipaddress 对象或抛异常。"""
    ip = ipaddress.ip_address(s.strip())
    if ip.version != 4:
        raise ValueError("只支持 IPv4")
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise ValueError(f"{s} 是内网/回环/保留地址，拒绝操作")
    return ip


def _check_path(p: str) -> Path:
    """realpath 后必须在白名单根内。"""
    rp = Path(p).resolve()
    for root in READABLE_ROOTS:
        try:
            rp.relative_to(Path(root).resolve())
            return rp
        except ValueError:
            continue
    raise ValueError(f"路径不在可读白名单内：{p}")


def _run(cmd: list, timeout: int = TIMEOUT) -> tuple:
    """跑白名单命令，返回 (stdout, returncode)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        return f"(命令超时 {timeout}s)", -1
    except Exception as e:
        return f"(执行失败: {e})", -1


def _cut(text: str, max_lines: int = MAX_LINES) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[-max_lines:]) + f"\n…（只显示最后 {max_lines} 行）"
    return text


# ── 实现 ────────────────────────────────────────────────────────────────
def system_status() -> str:
    out = []

    # 负载 / 开机时长
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        out.append(f"开机时长: {up/86400:.1f} 天")
    except Exception:
        pass
    try:
        out.append("负载(1/5/15min): " + Path("/proc/loadavg").read_text().split()[:3][0] + " " +
                   " ".join(Path("/proc/loadavg").read_text().split()[0:3]))
    except Exception:
        pass

    # 内存
    try:
        mem = {}
        for ln in Path("/proc/meminfo").read_text().splitlines():
            k, v = ln.split(":", 1)
            mem[k] = int(v.strip().split()[0])
        total, avail = mem["MemTotal"] // 1024, mem["MemAvailable"] // 1024
        out.append(f"内存: {total - avail}/{total} MB 已用，可用 {avail} MB")
    except Exception:
        pass

    # 磁盘
    try:
        du = shutil.disk_usage("/")
        out.append(f"磁盘 /: {du.used/1e9:.1f}/{du.total/1e9:.1f} GB ({du.used*100/du.total:.0f}%)")
    except Exception:
        pass

    # swap
    try:
        sw = {}
        for ln in Path("/proc/meminfo").read_text().splitlines():
            if ln.startswith("SwapTotal") or ln.startswith("SwapFree"):
                k, v = ln.split(":", 1)
                sw[k] = int(v.strip().split()[0]) // 1024
        out.append(f"Swap: {sw.get('SwapTotal',0)-sw.get('SwapFree',0)}/{sw.get('SwapTotal',0)} MB")
    except Exception:
        pass

    # 服务
    services = ["nginx", "xray", "fail2ban", "egress-filter",
                "ling-character-chat", "ling-bridge", "ling-scheduler"]
    txt, _ = _run(["systemctl", "is-active"] + services)
    states = txt.split("\n")
    out.append("服务: " + "  ".join(f"{s}={st}" for s, st in zip(services, states) if st))

    # 安全态势
    out.append("")
    out.append("── 安全 ──")
    txt, _ = _run(["fail2ban-client", "status"])
    if "Jail list" in txt:
        out.append(txt.split("Jail list:")[1].strip()[:200])
    for jail in ("sshd", "nginx-scan"):
        t, _ = _run(["fail2ban-client", "status", jail])
        m = re.search(r"Currently banned:\s*(\d+)", t)
        ips = re.search(r"Banned IP list:\s*(.*)", t)
        if m:
            out.append(f"  {jail}: 当前封禁 {m.group(1)} 个"
                       + (f"  [{ips.group(1).strip()[:120]}]" if ips and ips.group(1).strip() else ""))

    # 连接数
    t, _ = _run(["bash", "-c", "ss -tn state established 2>/dev/null | grep -v '127.0.0.1' | wc -l"])
    out.append(f"外部已建立连接: {t.strip()} 条")

    # 出站阻断
    t, _ = _run(["bash", "-c", "iptables -L OUTPUT -n 2>/dev/null | grep -c REJECT"])
    out.append(f"出站阻断规则: {t.strip()} 条")

    return "\n".join(out)


def read_logs(source: str = "", pattern: str = "", lines: int = 50) -> str:
    if source not in LOG_SOURCES:
        return f"(日志源 {source!r} 不在白名单。" + " 可选：" + " / ".join(LOG_SOURCES) + ")"
    n = max(1, min(int(lines or 50), MAX_LINES))
    spec = LOG_SOURCES[source]

    if spec[0].startswith("journalctl:"):
        args = spec[0].split(":", 1)[1:2] + spec[1:]
        cmd = ["journalctl", "--no-pager", "-n", str(n * 4 if pattern else n)] + args
    else:
        # 文件日志：用 tail，多个文件就分别 tail
        parts = []
        for f in spec:
            if os.path.exists(f):
                t, _ = _run(["tail", "-n", str(n * 4 if pattern else n), f])
                parts.append(t)
        text = "\n".join(parts)
        if pattern:
            text = "\n".join(l for l in text.splitlines() if pattern.lower() in l.lower())
            text = "\n".join(text.splitlines()[-n:])
        return f"[{source}] {len(text.splitlines())} 行\n" + _cut(text, n)[:MAX_BYTES]

    text, _ = _run(cmd)
    if pattern:
        text = "\n".join(l for l in text.splitlines() if pattern.lower() in l.lower())
        text = "\n".join(text.splitlines()[-n:])
    return f"[{source}]" + (f" 过滤「{pattern}」" if pattern else "") + f" {len(text.splitlines())} 行\n" + _cut(text, n)[:MAX_BYTES]


def ip_action(action: str = "", ip: str = "", reason: str = "", jail: str = "sshd") -> str:
    try:
        obj = _check_ip(ip)
    except Exception as e:
        return f"(IP 校验失败：{e})"

    # jail 白名单（原来是写死 sshd，被凌暮雪 review 出来 —— 那样扫 .env 的 IP 封不了）
    if jail not in ("sshd", "nginx-scan"):
        jail = "sshd"

    ip = str(obj)
    if action == "ban":
        t, rc = _run(["fail2ban-client", "set", jail, "banip", ip])
        ok = rc == 0 or "1" in t
        logger.info(f"[server_ops] ban {ip} jail={jail} reason={reason!r} -> {t.strip()[:80]}")
        return (f"{'已封禁' if ok else '封禁可能失败'} {ip}（jail={jail}）" +
                (f"\n理由：{reason}" if reason else "\n⚠️ 没给理由 —— 回复里必须向学长说明为什么要封") +
                f"\n（fail2ban 返回：{t.strip()[:120]}）")
    if action == "unban":
        t, rc = _run(["fail2ban-client", "set", jail, "unbanip", ip])
        logger.info(f"[server_ops] unban {ip} jail={jail} -> {t.strip()[:80]}")
        return f"已解封 {ip}（jail={jail}）\n（fail2ban 返回：{t.strip()[:120]}）"
    return f"(action 只能是 ban / unban，收到 {action!r})"


def read_file(path: str = "", start: int = 1, lines: int = 200) -> str:
    try:
        rp = _check_path(path)
    except Exception as e:
        return f"({e})"
    if not rp.exists():
        return f"(文件不存在：{path})"
    if rp.is_dir():
        items = sorted(p.name + ("/" if p.is_dir() else "") for p in rp.iterdir())[:80]
        return f"目录 {path} （{len(items)} 项）:\n" + "\n".join("  " + x for x in items)

    n = max(1, min(int(lines or 200), 500))
    st = max(1, int(start or 1))
    try:
        text = rp.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(读取失败：{e})"
    all_lines = text.splitlines()
    if rp.suffix == ".html" and "blob" in str(rp):
        # /repos 里 HTML 化的源码 —— 剥标签
        text = re.sub(r"<[^>]+>", "", text)
        all_lines = text.splitlines()
    chunk = all_lines[st - 1: st - 1 + n]
    body = "\n".join(f"{st + i:5d}| {l}" for i, l in enumerate(chunk))
    header = f"{path}  (共 {len(all_lines)} 行，显示 {st}-{st+len(chunk)-1})"
    return header + "\n" + body[:MAX_BYTES]


def search_code(pattern: str = "", scope: str = "all", limit: int = 30) -> str:
    if not pattern or not pattern.strip():
        return "(搜索词是空的)"
    roots = SCOPE_ROOTS.get(scope or "all", READABLE_ROOTS)
    n = max(1, min(int(limit or 30), 100))
    cmd = ["grep", "-rnI", "--exclude-dir=__pycache__", "--exclude-dir=node_modules",
           "--exclude-dir=venv", "--exclude-dir=.git", "-m", "3",
           "-e", pattern] + roots
    t, rc = _run(cmd, timeout=TIMEOUT)
    hits = [l for l in t.splitlines() if l.strip()]
    if not hits:
        return f"(在 {scope} 范围内没找到「{pattern}」)"
    shown = hits[:n]
    body = "\n".join(l[:200] for l in shown)
    more = f"\n…还有 {len(hits)-len(shown)} 条" if len(hits) > len(shown) else ""
    return f"搜「{pattern}」在 {scope}：{len(hits)} 条命中{more}\n{body}"
