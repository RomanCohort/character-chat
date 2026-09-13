"""诊断：消息到底有没有真的发出去。

之前的 sender 只判断"HTTP 没报错"就记「发送成功」——这次要看响应体本身。
ilink 的 sendmessage 是 HTTP 200 + JSON 里带 ret 的形态，ret != 0 就是没发成。

    python scripts/dsh_send_diag.py            # 只看响应，不发消息
    python scripts/dsh_send_diag.py --send     # 真发一条测试消息
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import sender  # noqa: E402


def raw_send(text: str) -> None:
    info = sender.resolve_target()
    body = sender._build_body(info["to_user_id"], text, info["context_token"])
    ok, raw = sender._post(info["base_url"], info["token"], body)
    print(f"判定 = {'成功' if ok else '失败'}")
    print(f"响应原文 = {raw[:600]}")
    if not ok:
        print("!! 消息没发出去。sender 现在会照实报失败（修之前它只看 HTTP 状态，把这种当成功）")
    else:
        print("接口收下了")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真发一条")
    args = ap.parse_args()

    info = sender.resolve_target()
    print(f"account   = {info['account_id']}")
    print(f"to        = {info['to_user_id']}")
    print(f"token     = {'有' if info['token'] else '无'}")
    print(f"ctx_token = {info['context_token'][:24]}…（{len(info['context_token'])} 字符）")
    print(f"state dir = {sender.WEIXIN_STATE_DIR}")

    # 光标：bridge 轮询到哪儿了
    sync = sender.WEIXIN_STATE_DIR.parent / "accounts" / f"{info['account_id']}.sync.json"
    print(f"sync buf  = {sync}  存在={sync.exists()}")

    if not args.send:
        print("\n（加 --send 才真发）")
        return 0

    print("\n--- 发送 ---")
    raw_send("诊断：这条是测试消息，收到请忽略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
