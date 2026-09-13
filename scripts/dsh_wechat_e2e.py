"""端到端：走一遍真实的微信命令通道，并把结果发到你的微信。

流程与线上完全一致：
    POST /chat {text:"!dsh ...", session_id:<你的wxid>}
      → server 识别为 DSH 命令 → 派发 → 立刻返回回执
      → 后台跑 DSH → 结果通过 ilink bot API 发到微信

用法：
    python scripts/dsh_wechat_e2e.py "在 D:\\character_chat 下建 e2e_probe.txt，内容写 ok"
    python scripts/dsh_wechat_e2e.py --help

注意：这真的会给你微信发消息；也确实会让 DSH 执行那条任务。
"""
import argparse
import asyncio
import os
import sys
import time

import urllib.request
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import sender  # noqa: E402


def post_chat(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="只回四个字：通道已通")
    ap.add_argument("--url", default=os.environ.get("CHARACTER_CHAT_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--wait", type=float, default=180.0, help="等结果多久（秒）")
    ap.add_argument("--no-send", action="store_true", help="只打回执，不真发微信")
    args = ap.parse_args()

    info = sender.resolve_target()
    chat_id = info["to_user_id"]
    print(f"对话方：{chat_id}")
    print(f"发往   ：{args.url}/chat   任务：{args.task!r}")

    if not args.no_send:
        sender.send_text_sync(f"（通道自检）准备跑：{args.task}")
        print("已先发一条提示到微信")

    t0 = time.time()
    res = post_chat(f"{args.url}/chat", f"!dsh {args.task}", chat_id)
    print(f"[{time.time()-t0:.1f}s] /chat 回执：{res.get('reply')!r} character={res.get('character')}")

    if res.get("character") != "dsh":
        print("!! 没有走 DSH 分支——检查 server 是否已重启加载新代码")
        return 1

    print(f"结果由后台任务发出，等 {args.wait:.0f}s 观察（这里不重复发）")
    time.sleep(min(args.wait, 30))
    print("完。真正的结果请看你微信。详细日志见 D:\\ling-muxue-logs\\dsh_bridge.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
