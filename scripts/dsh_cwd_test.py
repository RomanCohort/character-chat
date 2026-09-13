"""换工作目录：切换、各自一条会话、换回去接得回。

离线跑（直接驱动 DshBridge，不发微信）。用 D:\\wechat-character-bridge
——那是 wcab-bot 自己的工作目录，里面有 hi.txt / ok.txt 可对照。

    python scripts/dsh_cwd_test.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat.acp_client import DshBridge  # noqa: E402

BRIDGE_DIR = r"D:\wechat-character-bridge"
CHAT = "cwd-test"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


async def ask(bridge: DshBridge, text: str) -> str:
    res = await bridge.run_turn(CHAT, text)
    return f"<ERROR {res.error}>" if res.error else res.text


async def main() -> int:
    ok = True
    state = os.path.join(tempfile.gettempdir(), "dsh_cwd_test_state.json")
    if os.path.exists(state):
        os.remove(state)

    b = DshBridge(state_file=state)
    try:
        start_cwd = b.cwd_of(CHAT)
        print(f"  起始目录 {start_cwd}")

        # 1. 先在起点目录建一条会话（这样"换回去"才有东西可接）
        a0 = await ask(b, "只回两个字：在的")
        sid_home = b.dsh_session_of(CHAT)
        ok &= check("起点目录建会话", bool(sid_home), str(sid_home)[:8])

        # 2. 换到 bridge 目录干活
        cwd, existing = await b.switch_cwd(CHAT, BRIDGE_DIR)
        ok &= check("切换工作目录", cwd.upper() == BRIDGE_DIR.upper(), cwd)
        ok &= check("该目录此前无会话", existing == "")

        a1 = await ask(b, "只列出当前工作目录下的 .txt 文件名，一行一个，不要别的字")
        print(f"  -> {a1!r}")
        sid_bridge = b.dsh_session_of(CHAT)
        ok &= check("会话落在 bridge 目录", "hi.txt" in a1.lower(), a1.replace("\n", " "))
        ok &= check("两个目录各一条会话", sid_bridge != sid_home and bool(sid_bridge))

        # 3. 换回起点目录：应当接回原来那条，而不是新建
        cwd2, _ = await b.switch_cwd(CHAT, start_cwd)
        sid_back = b.dsh_session_of(CHAT)
        ok &= check("换回原目录", cwd2.upper() == str(start_cwd).upper(), cwd2)
        ok &= check("换回后接的是原来那条", sid_back == sid_home,
                    f"home={str(sid_home)[:8]} back={str(sid_back)[:8]}")

        await b.shutdown()  # 模拟重启，逼它走 resume

        # 4. 重启后仍在起点目录、仍是那条会话
        a2 = await ask(b, "只回两个字：在的")
        print(f"  -> {a2!r}")
        sid_again = b.dsh_session_of(CHAT)
        ok &= check("重启后仍在起点目录会话", sid_again == sid_home,
                    f"home={str(sid_home)[:8]} again={str(sid_again)[:8]}")
        ok &= check("bridge 目录那条还留着",
                    (b._chat_rec(CHAT).get("sessions") or {}).get(BRIDGE_DIR) == sid_bridge)
        # 5. 非法目录要被挡住
        for bad in ("相对路径", r"D:\不存在的目录_zzz"):
            try:
                await b.switch_cwd(CHAT, bad)
                ok &= check(f"拒绝 {bad}", False, "竟然接受了")
            except Exception as e:
                ok &= check(f"拒绝 {bad}", True, str(e)[:60])

        ok &= check("被拒后目录没被改坏", b.cwd_of(CHAT).upper() == str(start_cwd).upper())
    finally:
        await b.shutdown()

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
