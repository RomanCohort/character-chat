"""端到端多轮：微信通道连发两条，确认是同一个 DSH 会话接得上。

    python scripts/dsh_wechat_multiturn.py --url http://127.0.0.1:8001

不看内部状态——两条消息都从 /chat 进，结果都发到你微信。
你微信上应当收到：第一轮的「好」，第二轮的「四十七」。
"""
import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")

from character_chat.wechat import sender  # noqa: E402

TURNS = [
    "记住这个数字：四十七。只回一个字：好",
    "刚才让你记的数字是多少？只回那两个汉字",
]


def post_chat(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--gap", type=float, default=90.0, help="两条之间的间隔（秒）")
    args = ap.parse_args()

    chat_id = sender.resolve_target()["to_user_id"]
    print(f"对话方 {chat_id} → {args.url}")

    for i, text in enumerate(TURNS, start=1):
        t = time.time()
        res = post_chat(args.url, f"!dsh {text}", chat_id)
        print(f"[{i}] {time.time()-t:.1f}s 回执 {res.get('reply')!r}  character={res.get('character')}")
        if i < len(TURNS):
            print(f"    等 {args.gap:.0f}s 让第一轮跑完")
            time.sleep(args.gap)

    print("两条都派发了。结果在微信里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
