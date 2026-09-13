"""验：手机从 LAN 地址能不能完成配对（不是猜，真跑一遍接受流程）。

流程（照 remote-web-ui 的实现）：
  1. 从回环铸一枚一次性令牌  POST /api/pair/issue          —— 只有回环能做
  2. 模拟手机拿着令牌去接受  POST /api/pair/accept         —— 从 LAN 地址发，带该地址的 Host
  3. 看返回是不是给了设备 cookie，以及 /pair-app 能不能拿到应用壳

    python scripts/pair_probe.py --ip 10.66.221.119
"""
import argparse
import json
import urllib.error
import urllib.request


def req(url: str, method="GET", body=None, host=None, cookie=None):
    headers = {}
    if host:
        headers["Host"] = host
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, dict(e.headers or {}), raw
    except Exception as e:
        return 0, {}, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="10.66.221.119", help="手机所在的 LAN 地址")
    ap.add_argument("--port", type=int, default=3080)
    args = ap.parse_args()
    ok = True

    # 1. 铸令牌（只能从回环）
    st, _, raw = req("http://127.0.0.1:%d/api/pair/issue" % args.port, method="POST")
    print(f"[1] issue 回环 -> {st}")
    if st != 200:
        print("    铸令牌失败，后面没法继续:", raw[:200])
        return 1
    data = json.loads(raw)
    token = data["token"]
    print(f"    url    = {data.get('url')}")
    print(f"    token  = {token[:12]}…")
    print(f"    lan    = {data.get('lanAddresses')}")

    # 2. 从 LAN 地址接受
    lan = f"http://{args.ip}:{args.port}"
    st2, hdrs2, raw2 = req(
        lan + "/api/pair/accept",
        method="POST",
        body={"token": token},
        host=f"{args.ip}:{args.port}",
    )
    print(f"[2] accept 从 LAN({args.ip}) -> {st2}")
    print(f"    响应: {raw2[:220]}")
    set_cookie = hdrs2.get("Set-Cookie") or hdrs2.get("set-cookie") or ""
    print(f"    下发 cookie: {set_cookie[:80] if set_cookie else '（无）'}")
    if st2 != 200:
        ok = False

    # 3. 拿应用壳
    cookie = ""
    if set_cookie:
        cookie = set_cookie.split(";")[0]
    st3, _, raw3 = req(
        lan + "/pair-app", host=f"{args.ip}:{args.port}", cookie=cookie or None
    )
    print(f"[3] GET /pair-app -> {st3}  长度 {len(raw3)}")
    looks_like_app = ("<div id=" in raw3) or ("<!DOCTYPE html" in raw3[:200] and len(raw3) > 2000)
    print(f"    像应用壳吗: {looks_like_app}")
    if st3 != 200:
        ok = False

    print("\n" + ("配对通道可用" if ok else "有问题，见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
