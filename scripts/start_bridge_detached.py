"""把微信轮询桥以**脱离方式**启动，让它不属于任何 agent 会话的 job。

为什么需要这个（2026-09-12 踩过的坑）：
  - 用 agent 的后台任务启动桥 → 它成了那个 job 的子进程。DSH 的 job 管理器
    在宿主重载/取消时会把它一起带走，于是"桥活着但不转发"或直接消失。
  - `start_ling_muxue_silent.bat` 用 `start /b` 也不够——那是 cmd 的内部后台，
    父 cmd 一退它未必活。
  - watchdog.py 里 `check_bridge()` 被作者有意改成永远返回 True（注释写着
    "bridge 挂了手动重启比让 watchdog 乱拉更安全"），而且它调的
    `node_modules\\.bin\\wcab-bot` 是 bash 包装脚本，在 Windows 上起不来。
    所以看护这条路是空的，别指望它。

做法：DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP + DEVNULL stdio。
这样桥是独立的顶层进程，父进程退出不影响它。

用法：
    python scripts/start_bridge_detached.py          # 没在跑就启动
    python scripts/start_bridge_detached.py --force  # 先杀掉再启动
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = os.environ.get("DSH_WECHAT_BRIDGE_DIR", r"D:\wechat-character-bridge")
ACCOUNT = os.environ.get("WEIXIN_BOT_ACCOUNT", "54a0145ab8d5-im-bot")
LOG = os.environ.get(
    "DSH_WECHAT_BRIDGE_LOG", str(_PROJECT_ROOT / "logs" / "bridge_detached.log")
)
STATE_DIR = Path.home() / ".weixin-mcp"


def sync_file() -> Path:
    return STATE_DIR / "accounts" / f"{ACCOUNT}.sync.json"


def cursor_age() -> float | None:
    f = sync_file()
    if not f.exists():
        return None
    return time.time() - f.stat().st_mtime


def bridge_pids() -> list[int]:
    """真正的桥进程：命令行含 wcab-bot.cmd，且不是检测命令自己。"""
    try:
        out = subprocess.check_output(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                "Where-Object { $_.CommandLine -like '*wcab-bot.cmd*' -and $_.CommandLine -notlike '*Get-CimInstance*' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            text=True, timeout=25, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        print(f"枚举进程失败: {e!r}")
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="先杀掉现有的再启动")
    args = ap.parse_args()

    existing = bridge_pids()
    if existing and not args.force:
        age = cursor_age()
        age_text = f"{age:.0f}s" if age is not None else "无光标文件"
        print(f"桥已在跑 (pid={existing})，光标年龄 {age_text}；不动它。")
        return 0

    if args.force:
        for pid in existing:
            print(f"杀掉 pid={pid}")
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(2)

    Path(LOG).parent.mkdir(parents=True, exist_ok=True)
    cmdline = (
        f'cd /d "{BRIDGE_DIR}" && '
        "set CHARACTER_CHAT_URL=http://127.0.0.1:8000&& "
        "set CHARACTER_CHAT_TIMEOUT_MS=600000&& "
        f'node_modules\\.bin\\wcab-bot.cmd --cwd "{BRIDGE_DIR}" --account {ACCOUNT} '
        f'>> "{LOG}" 2>&1'
    )

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd.exe", "/c", cmdline],
        cwd=BRIDGE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    print("已以脱离方式启动，等它把光标推起来…")

    for i in range(18):  # 最多 90 秒
        time.sleep(5)
        age = cursor_age()
        if age is not None and age < 60:
            print(f"OK 光标在动（{age:.0f}s 前），桥已就绪")
            print("pid:", bridge_pids())
            return 0
        print(f"  …第 {i+1} 次探测，光标年龄 {age if age is None else round(age)}s")
    print("FAILED：90 秒内光标没动起来，看 " + LOG)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
