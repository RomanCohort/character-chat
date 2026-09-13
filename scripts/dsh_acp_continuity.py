"""离线性验证：多轮连续性 + 重启恢复。

不走微信、不碰网络发送——把 sender.send_text 换成记录器，直接在进程内验协议层。
（真发微信见 scripts/dsh_wechat_e2e.py。）

覆盖三件事：
1. 同一 chat 连问两轮：第二轮记得第一轮的数字（同一个 ACP 会话）
2. 全程只开一条 DSH 会话，不重复 new
3. 换一个 DshBridge 实例（模拟服务重启）：走 resume 接回原会话，仍然记得
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import acp_client as acp  # noqa: E402
from character_chat.wechat.acp_client import DshBridge  # noqa: E402

CWD = acp.DEFAULT_CWD
CHAT = "selftest-chat"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


async def ask(bridge: DshBridge, text: str) -> str:
    res = await bridge.run_turn(CHAT, text)
    if res.error:
        return f"<ERROR {res.error}>"
    return res.text


async def main() -> int:
    ok = True
    state = os.path.join(tempfile.gettempdir(), "dsh_bridge_selftest_state.json")
    if os.path.exists(state):
        os.remove(state)

    b1 = DshBridge(cwd=CWD, state_file=state)
    try:
        a1 = await ask(b1, "记住这个数字：四十七。只回一个字：好")
        print(f"  turn1 -> {a1!r}")
        sid1 = b1.dsh_session_of(CHAT)
        a2 = await ask(b1, "刚才让你记的数字是多少？只回那两个汉字")
        print(f"  turn2 -> {a2!r}")
        sid2 = b1.dsh_session_of(CHAT)

        ok &= check("第二轮记得第一轮", "四十七" in a2, a2)
        ok &= check("全程同一个会话", sid1 == sid2 and bool(sid1), f"{str(sid1)[:8]}")

        # 模拟服务重启：新 bridge 实例、新 ACP 进程
        await b1.shutdown()
        b2 = DshBridge(cwd=CWD, state_file=state)
        try:
            a3 = await ask(b2, "还是刚才那个数字，只回那两个汉字")
            print(f"  turn3(重启后) -> {a3!r}")
            ok &= check("重启后 resume 接得回", "四十七" in a3, a3)
            ok &= check("重启用的是原会话", b2.dsh_session_of(CHAT) == sid1)
        finally:
            await b2.shutdown()
    finally:
        await b1.shutdown()

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
