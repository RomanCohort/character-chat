"""对照：`*work.`（带句点）vs `*work `（带空格）分别走哪条路。

- `*work. ...` → 聊天通道（凌暮雪），因为触发词要求后面跟空格或行尾
- `*work ...`  → DSH 通道，真的把活派出去

    python scripts/dsh_period_vs_space.py --url http://127.0.0.1:8000
"""
import argparse
import asyncio
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")

from character_chat.wechat import bridge as dsh_bridge  # noqa: E402
from character_chat.wechat import sender  # noqa: E402


def post(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id, "skip_record": True}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--task", default="只回四个字：派活成功")
    args = ap.parse_args()
    chat_id = sender.resolve_target()["to_user_id"]
    ok = True

    # 0. 纯函数层：哪种写法算命令
    cases = [
        ("*work.   帮我调查一下", False),
        ("*work 帮我调查一下", True),
        ("*work", True),
        ("*working on it", False),
    ]
    bad = [(t, w, dsh_bridge.is_command(t)) for t, w in cases if dsh_bridge.is_command(t) != w]
    ok &= check("触发词判定", not bad, f"不符：{bad}" if bad else f"{len(cases)} 例")

    # 1. 带句点 → 必须走聊天
    t = time.time()
    r1 = post(args.url, "*work.   帮我调查一下", chat_id)
    print(f"  [*work. 带句点] {time.time()-t:.1f}s character={r1.get('character')} reply={r1.get('reply','')[:50]!r}")
    ok &= check("带句点走聊天通道", r1.get("character") == "凌暮雪", str(r1.get("character")))

    # 2. 带空格 → 必须走 DSH，且真的派出去
    time.sleep(2)
    t = time.time()
    r2 = post(args.url, f"*work {args.task}", chat_id)
    print(f"  [*work 带空格] {time.time()-t:.1f}s character={r2.get('character')} reply={r2.get('reply','')[:60]!r}")
    ok &= check("带空格走 DSH", r2.get("character") == "dsh", str(r2.get("character")))
    ok &= check("回执在 30 秒内返回（桥等得起）", time.time() - t < 30, f"{time.time()-t:.1f}s")

    print("\n结果稍后由后台任务发到微信（顺便验转述链路）。")
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
