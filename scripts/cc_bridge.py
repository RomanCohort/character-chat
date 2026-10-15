# -*- coding: utf-8 -*-
"""JLU: Become Human —— 每人一条微信桥的发放 / 登录 / 启停。

一把 /ling/ 密钥 = 一个 state 目录 + 一个 env 文件 + 一个 systemd 实例
（cc-bridge@<哈希前12位>），互不干扰。微信会话落在那个 state 目录里。

    python3 cc_bridge.py login  <密钥>     取二维码（写进状态文件，页面读它）
    python3 cc_bridge.py status <密钥>     看登录状态
    python3 cc_bridge.py enable <密钥>     登录成功后起常驻桥
    python3 cc_bridge.py disable <密钥>    停掉并禁用
    python3 cc_bridge.py list              列出所有已发放的桥
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time

ROOT = "/opt/character_chat"
KEY_FILE = "/var/lib/ling/cc_keys.json"
BRIDGE_DIR = "/var/lib/ling/cc-bridge"
HELPER = "/opt/wechat-bridge/cc-wechat-login.mjs"
BRIDGE_CWD = "/opt/wechat-bridge"
NODE = "/usr/bin/node"
BACKEND = "http://127.0.0.1:8000"


def h12(key):
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:12]


def paths(h):
    d = os.path.join(BRIDGE_DIR, h)
    return {"dir": d, "env": os.path.join(BRIDGE_DIR, h + ".env"),
            "status": os.path.join(d, "login.json")}


def provision(key):
    """建目录 + 写 env 文件（0600）。幂等。"""
    h = h12(key)
    p = paths(h)
    if not os.path.isdir(p["dir"]):
        os.makedirs(p["dir"])
    body = (
        "# 自动生成，别手改（改完要重起 cc-bridge@%s）\n"
        "WEIXIN_MCP_STATE_DIR=%s\n"
        "CC_LOGIN_STATUS=%s\n"
        "CHARACTER_CHAT_URL=%s/api/cc/bridge/%s\n"
        "WEIXIN_BOT_AGENT=claude\n"
        # 登录成功后由 enable 写进来；systemd 用 ${CC_ACCOUNT} 展开传给 --account。
        # 不传 --account 的话桥会走交互式选账号（问 stdin），在 systemd 下会挂住。
        "CC_ACCOUNT=\n"
    ) % (h, p["dir"], p["status"], BACKEND, key.strip())
    tmp = p["env"] + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p["env"])
    return h, p


def read_status(key):
    p = paths(h12(key))
    try:
        with io.open(p["status"], encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def spawn_login(key):
    """后台起登录工具，把二维码写进状态文件。"""
    h, p = provision(key)
    try:
        os.remove(p["status"])
    except OSError:
        pass
    env = dict(os.environ)
    env["WEIXIN_MCP_STATE_DIR"] = p["dir"]
    env["CC_LOGIN_STATUS"] = p["status"]
    with open(os.path.join(p["dir"], "login.log"), "ab") as log:
        subprocess.Popen([NODE, HELPER], cwd=BRIDGE_CWD, env=env,
                         stdout=log, stderr=log,
                         start_new_session=True)
    # 等它写出二维码（最多 25 秒）
    for _ in range(50):
        time.sleep(0.5)
        st = read_status(key)
        if st and st.get("state") in ("waiting", "failed", "connected"):
            return h, st
    return h, {"state": "starting", "message": "还在取二维码，稍等一下再刷新"}


def systemctl(action, key):
    h = h12(key)
    unit = "cc-bridge@%s.service" % h
    r = subprocess.run(["systemctl", action, unit],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, (r.stdout or b"").decode("utf-8", "replace").strip()


def cmd_login(args):
    h, st = spawn_login(args.key)
    print(json.dumps({"id": h, "status": st}, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    h = h12(args.key)
    st = read_status(args.key) or {"state": "none"}
    unit = "cc-bridge@%s.service" % h
    r = subprocess.run(["systemctl", "is-active", unit],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    st["service"] = (r.stdout or b"").decode("utf-8", "replace").strip()
    st["id"] = h
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_enable(args):
    provision(args.key)
    st = read_status(args.key) or {}
    if st.get("state") != "connected":
        print("还没登录成功（state=%s），先扫码。" % st.get("state"))
        return 1
    acct = st.get("account") or ""
    if not acct:
        print("状态里没有账号名，先重新登录。")
        return 1
    # 把账号名写回 env（systemd 用它展开 --account）
    h = h12(args.key)
    p = paths(h)
    with io.open(p["env"], encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("CC_ACCOUNT="):
            lines[i] = "CC_ACCOUNT=" + acct
    tmp = p["env"] + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p["env"])
    rc, out = systemctl("enable", args.key)
    rc2, out2 = systemctl("restart", args.key)
    print("account=%s  enable rc=%d restart rc=%d  %s %s"
          % (acct, rc, rc2, out, out2))
    return 0 if rc2 == 0 else 1


def cmd_disable(args):
    rc, out = systemctl("disable", args.key)
    rc2, out2 = systemctl("stop", args.key)
    print("disable rc=%d stop rc=%d %s %s" % (rc, rc2, out, out2))
    return 0


def cmd_list(args):
    if not os.path.isdir(BRIDGE_DIR):
        print("(还没发放过)")
        return 0
    names = sorted(n for n in os.listdir(BRIDGE_DIR) if n.endswith(".env"))
    for n in names:
        h = n[:-4]
        st = {"state": "?"}
        try:
            with io.open(os.path.join(BRIDGE_DIR, h, "login.json"),
                         encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            pass
        r = subprocess.run(["systemctl", "is-active", "cc-bridge@%s.service" % h],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print("%s  %-10s account=%s  unit=%s"
              % (h, st.get("state"), st.get("account") or "-",
                 (r.stdout or b"").decode("utf-8", "replace").strip()))
    print("共 %d 条" % len(names))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    for name in ("login", "status", "enable", "disable"):
        p = sub.add_parser(name)
        p.add_argument("key")
    sub.add_parser("list")
    args = ap.parse_args()
    fn = {"login": cmd_login, "status": cmd_status, "enable": cmd_enable,
          "disable": cmd_disable, "list": cmd_list}.get(args.cmd)
    if not fn:
        ap.print_help()
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
