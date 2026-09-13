"""转述链路：DSH 的结果由凌暮雪说出来。

离线跑（把 sender.send_text 换成记录器，不发微信），把关的是两条：
1. 回执/进度/结果确实是她的话（不是「收到，跑着呢」那种技术回执）
2. **失败没有被美化**——报错/没找到，必须照实说

    python scripts/dsh_narrate_test.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import bridge, sender  # noqa: E402
from character_chat.wechat.acp_client import DshBridge  # noqa: E402

CHAT = "narrate-test"
sent: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


async def main() -> int:
    ok = True

    # 换掉出站发送，记录即可
    async def fake_send(text: str, to_user_id=None):
        sent.append(text)
        print(f"    [发出] {text}")
        return True, "recorded"

    sender.send_text = fake_send

    state = os.path.join(tempfile.gettempdir(), "dsh_narrate_state.json")
    if os.path.exists(state):
        os.remove(state)
    bridge._bridge = DshBridge(state_file=state)

    # 注册凌暮雪作为转述器
    project_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    os.chdir(project_root)
    from character_chat.cli import Cli
    from functools import partial

    cli = await asyncio.get_running_loop().run_in_executor(
        None, lambda: Cli(character_name="凌暮雪", neural_enabled=False)
    )
    bridge.set_narrator(partial(cli.send, skip_record=True))
    ok &= check("转述器已注册", bridge._narrator is not None)

    # 1. 回执
    receipt = await bridge.dispatch(CHAT, "!dsh 只回两个字：在的")
    print(f"  回执 -> {receipt!r}")
    ok &= check("回执是她的话（不是技术模板）",
                receipt != "收到，跑着呢……好了我发你。" and "!" not in receipt and "！" not in receipt,
                receipt)

    await asyncio.sleep(75)  # 等这一轮跑完
    ok &= check("后台确实发出了结果", len(sent) >= 1, f"{len(sent)} 条")
    if sent:
        final = sent[-1]
        ok &= check("结果没带 DSH 机械头", not final.startswith("DSH ·"), final[:40])

    # 2. 失败必须照实说（不存在的文件）
    sent.clear()
    await bridge.dispatch(CHAT, "!dsh 读一下 D:\\character_chat\\__肯定没有这个文件__.txt，把内容原样给我")
    await asyncio.sleep(75)
    final = sent[-1] if sent else ""
    print(f"  失败转述 -> {final!r}")
    hints = ("没有", "不存在", "找不到", "失败", "报错", "错误", "打不开", "no such", "not found")
    ok &= check("失败被照实说了", any(h in final.lower() for h in hints), final[:60])
    ok &= check("失败被说成成功", not any(w in final for w in ("成功", "搞定", "好了", "完成了")), final[:60])

    await bridge.shutdown()
    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
