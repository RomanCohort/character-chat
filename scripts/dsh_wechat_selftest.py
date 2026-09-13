"""微信 DSH 通道的离线自检。

验三件事，全部对着真实状态读，不 mock：
1. server.py 能导入（接线没断）
2. 前缀识别 / 元命令路由
3. sender 能从 bridge 的 state dir 解析出发送目标（token / 对话方 / context_token 齐全）
4. acp_client 能找到 DSH CLI

真跑一轮对话见 scripts/dsh_acp_smoke.py；真发微信见 scripts/dsh_wechat_e2e.py。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from character_chat.wechat import bridge as dsh_bridge  # noqa: E402
from character_chat.wechat import sender  # noqa: E402
from character_chat.wechat.acp_client import DSH_CLI, DshBridge  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


async def main() -> int:
    ok = True

    # 1. server 能导入
    import character_chat.server as server  # noqa: F401
    ok &= check("server.py 导入", True)

    # 2. 触发词识别
    cases = [
        ("*work 跑一下测试", True),
        ("*work", True),
        ("*work status", True),
        ("*work /status", True),      # 斜杠形式仍认（肌肉记忆）
        ("*workcwd 不是命令", False),
        ("* work 不是命令", False),
        ("!dsh 旧前缀不再触发", False),
        ("你好呀", False),
        ("", False),
    ]
    bad = [(t, want, dsh_bridge.is_command(t)) for t, want in cases if dsh_bridge.is_command(t) != want]
    ok &= check("触发词识别", not bad, f"不符：{bad}" if bad else f"{len(cases)} 例")

    # 3. 去前缀
    ok &= check("去前缀", dsh_bridge.strip_prefix("*work status") == "status",
                repr(dsh_bridge.strip_prefix("*work status")))
    ok &= check("子命令拆分（带/不带斜杠都对）",
                dsh_bridge._split_meta("cwd D:\\x") == ("cwd", "D:\\x")
                and dsh_bridge._split_meta("/cwd D:\\x") == ("cwd", "D:\\x")
                and dsh_bridge._split_meta("看一下测试") == ("", "看一下测试"))

    # 4. sender 目标解析
    try:
        t = sender.resolve_target()
        ok &= check("sender 目标解析", bool(t["token"]) and bool(t["to_user_id"]),
                    f"account={t['account_id']} to={t['to_user_id'][:12]}… token=有")
    except Exception as e:
        ok &= check("sender 目标解析", False, repr(e))

    # 5. 分块
    chunks = sender.chunk_text("a" * 4000)
    ok &= check("长消息分块", len(chunks) == 3 and all(len(c) <= sender.CHUNK_CHARS for c in chunks),
                f"{len(chunks)} 块 / 上限 {sender.CHUNK_CHARS}")

    # 6. DSH CLI 在位
    ok &= check("DSH CLI 在", os.path.exists(DSH_CLI), DSH_CLI)

    # 7. 元命令（不碰网络，只走 help 与 status 的本地部分）
    try:
        reply = await dsh_bridge.dispatch("selftest", "*work help")
        ok &= check("元命令 help", "*work <任务>" in reply, reply.splitlines()[0] if reply else "")
    except Exception as e:
        ok &= check("元命令 help", False, repr(e))

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
