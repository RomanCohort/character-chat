"""检查：凌暮雪起来之后，!dsh 的回执是不是她的话。

先发一条普通聊天把她的实例建起来（这一步 bridge 会回你微信），
再发一条 !dsh，看回执是「收到，跑着呢」还是她自己的口气。

    python scripts/dsh_receipt_check.py --url http://127.0.0.1:8000
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import sender  # noqa: E402

FALLBACK = "收到，跑着呢……好了我发你。"


def post(url: str, text: str, session_id: str, skip_record: bool = False) -> dict:
    body = json.dumps({"text": text, "session_id": session_id, "skip_record": skip_record}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--skip-record", action="store_true", default=True)
    args = ap.parse_args()

    chat_id = sender.resolve_target()["to_user_id"]

    t = time.time()
    r1 = post(args.url, "在忙，随便说句话就行", chat_id, skip_record=args.skip_record)
    print(f"[{time.time()-t:.1f}s] 普通聊天 character={r1.get('character')} reply={r1.get('reply','')[:60]!r}")
    print("           → 她这条也会发到你微信")

    time.sleep(5)
    t = time.time()
    r2 = post(args.url, "!dsh 只回两个字：在的", chat_id)
    reply = r2.get("reply", "")
    print(f"[{time.time()-t:.1f}s] !dsh 回执 character={r2.get('character')} reply={reply!r}")

    if reply == FALLBACK:
        print("FAIL  还是技术模板——说明转述器没注册上（看 active_sessions 是否为 0）")
        return 1
    if "！" in reply or "!" in reply:
        print("FAIL  回执里带感叹号，不像她")
        return 1
    print("PASS  回执是她自己的口气")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
