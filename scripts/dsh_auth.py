"""账号管理：加人、改密、列人、删人。

用法（在机器上跑，不是远程）：
    python scripts/dsh_auth.py add <用户名>          # 交互式输密码
    python scripts/dsh_auth.py add <用户名> --password <密码>   # 非交互（会进 shell 历史，慎用）
    python scripts/dsh_auth.py list
    python scripts/dsh_auth.py passwd <用户名>
    python scripts/dsh_auth.py remove <用户名>
    python scripts/dsh_auth.py kick                 # 换签名密钥 = 所有人立刻被登出

为什么单独做成脚本而不是网页注册页：
    这是"连上机器"的唯一凭据。开放自助注册等于把门拆了。加人必须在机器上做。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from character_chat.wechat import auth  # noqa: E402


def _read_password(prompt: str = "密码: ") -> str:
    first = getpass.getpass(prompt)
    second = getpass.getpass("再输一次: ")
    if first != second:
        print("两次输入不一致")
        sys.exit(1)
    if len(first) < 6:
        print("密码至少 6 位")
        sys.exit(1)
    return first


def main() -> int:
    ap = argparse.ArgumentParser(description="character_chat 连接认证的账号管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="加用户或改密码")
    p_add.add_argument("username")
    p_add.add_argument("--password", help="非交互模式（会留在 shell 历史里）")

    sub.add_parser("list", help="列出用户")

    p_pw = sub.add_parser("passwd", help="改密码")
    p_pw.add_argument("username")
    p_pw.add_argument("--password")

    p_rm = sub.add_parser("remove", help="删用户")
    p_rm.add_argument("username")

    sub.add_parser("kick", help="换签名密钥，把所有人登出")

    args = ap.parse_args()

    if args.cmd == "add":
        pw = args.password or _read_password()
        auth.create_user(args.username, pw)
        print(f"已写入用户 {args.username!r} -> {auth.USERS_FILE}")
    elif args.cmd == "passwd":
        if args.username not in auth.list_users():
            print(f"没有这个用户: {args.username}")
            return 1
        pw = args.password or _read_password("新密码: ")
        auth.create_user(args.username, pw)
        print(f"已改密码: {args.username}")
    elif args.cmd == "list":
        users = auth.list_users()
        if not users:
            print("(没有用户。远程访问会被拒——先 add 一个)")
        for u in users:
            rec = auth._load_users().get(u) or {}
            print(f"  {u}")
        print(f"\n库文件: {auth.USERS_FILE}")
    elif args.cmd == "remove":
        if auth.delete_user(args.username):
            print(f"已删除: {args.username}（他手里的 cookie 下次请求就失效）")
        else:
            print(f"没有这个用户: {args.username}")
            return 1
    elif args.cmd == "kick":
        try:
            auth.SECRET_FILE.unlink()
            print("已删除签名密钥——所有会话立刻失效（用户还在，重新登录即可）")
        except FileNotFoundError:
            print("签名密钥本来就不存在，没有会话可踢")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
