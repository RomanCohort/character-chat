#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发/查/吊销「她不一样」网页版的密钥。

    python3 slm_keys.py new --label 朋友A --days 30 --calls 3
    python3 slm_keys.py list
    python3 slm_keys.py revoke <哈希前缀>

明文密钥只在 new 的那一次打印出来 —— 之后库里只有 sha256。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, "/opt/character_chat/src")
os.chdir("/opt/character_chat")
from character_chat import sheloveme as SLM      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--label", required=True)
    n.add_argument("--days", type=int, default=30)
    n.add_argument("--calls", type=int, default=SLM.CALLS_PER_DAY)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("prefix")
    a = ap.parse_args()

    if a.cmd == "new":
        out = SLM.mint(a.label, a.days, a.calls)
        print("密钥（只显示这一次，请立刻发出去）：")
        print("   ", out["key"])
        print("    标签 %s · 每天 %d 次 · 有效期 %d 天（到 %s）"
              % (out["record"]["label"], a.calls, a.days,
                 time.strftime("%Y-%m-%d", time.localtime(out["record"]["expires"]))))
    elif a.cmd == "list":
        rows = SLM.list_keys()
        if not rows:
            print("还没有密钥。")
            return 0
        print("%-14s %-12s %-6s %-12s %-6s %-6s" % ("哈希前缀", "标签", "在用", "有效期至", "今日", "累计"))
        for x in rows:
            print("%-14s %-12s %-6s %-12s %-6s %-6s"
                  % (x["hash"], (x["label"] or "")[:12], "是" if x["enabled"] else "否",
                     time.strftime("%Y-%m-%d", time.localtime(x["expires"] or 0)),
                     x["used_today"], x["total"]))
    else:
        n = SLM.revoke(a.prefix)
        print("吊销了 %d 把。" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
