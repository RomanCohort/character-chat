"""验闸门：只有 *work 开头才会碰电脑。

- `*work …`      → character=dsh（走 DSH）
- 不带 *work 的话 → character=凌暮雪（走聊天，绝不碰电脑）
- 旧前缀 `!dsh`   → 也必须是凌暮雪（已被撤掉，不再是命令）

    python scripts/dsh_trigger_check.py --url http://127.0.0.1:8000
"""
import argparse
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")

from character_chat.wechat import sender  # noqa: E402


def post(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id, "skip_record": True}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    chat_id = sender.resolve_target()["to_user_id"]
    ok = True

    # 1. 带 *work：必须走 DSH
    t = time.time()
    r = post(args.url, "*work status", chat_id)
    print(f"  [*work status] {time.time()-t:.1f}s character={r.get('character')} reply={r.get('reply','')[:60]!r}")
    ok &= check("*work 走 DSH", r.get("character") == "dsh", str(r.get("character")))

    # 2. 不带触发词：必须走聊天（凌暮雪），绝不碰电脑
    time.sleep(2)
    t = time.time()
    r2 = post(args.url, "今天有点累，随便聊两句", chat_id)
    print(f"  [无触发词] {time.time()-t:.1f}s character={r2.get('character')} reply={r2.get('reply','')[:60]!r}")
    ok &= check("无触发词走聊天", r2.get("character") == "凌暮雪", str(r2.get("character")))

    # 3. 旧前缀不再是命令
    time.sleep(1)
    r3 = post(args.url, "!dsh status", chat_id)
    print(f"  [!dsh status] character={r3.get('character')}")
    ok &= check("旧前缀 !dsh 已失效", r3.get("character") != "dsh", str(r3.get("character")))

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
