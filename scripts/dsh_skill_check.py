"""验：DSH 在微信通道里认不认 skill。

不是查文档猜的——直接让 DSH 自己报它有什么。

    python scripts/dsh_skill_check.py          # 只查有没有/有哪些
    python scripts/dsh_skill_check.py --run    # 再真调一个 skill 试试
"""
import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")

from character_chat.wechat import sender  # noqa: E402


def post(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--run", action="store_true", help="再真调一个 skill")
    args = ap.parse_args()
    chat_id = sender.resolve_target()["to_user_id"]

    tasks = [
        "你手上有 skill 这个工具吗？如果有，把你当前能用的 skill 名字全部列出来，"
        "一行一个，不要解释；如果没有，只回两个字：没有",
    ]
    if args.run:
        tasks.append(
            "用 skill 工具加载 research 这个 skill，然后只回一句话：skill 加载成功还是失败"
        )

    for t in tasks:
        print(f"--- 派发：{t[:50]}…")
        r = post(args.url, f"*work {t}", chat_id)
        print(f"    回执：{r.get('reply','')[:60]!r}  character={r.get('character')}")
        print("    结果稍后发到微信（也见 dsh_bridge.log）")
        time.sleep(80)

    print("\n看微信，或看 D:\\ling-muxue-logs\\dsh_bridge.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
