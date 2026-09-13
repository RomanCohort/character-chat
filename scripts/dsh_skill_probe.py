"""让 DSH 自己把"skill 清单 / skill 可用性"写成文件，我好直接读。

    python scripts/dsh_skill_probe.py
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("scripts", 1)[0] + "src")

from character_chat.wechat import sender  # noqa: E402

# 默认写到项目根，可用环境变量覆盖（原来写死 D:\character_chat）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("DSH_SKILL_PROBE_OUT", os.path.join(_PROJECT_ROOT, "skill_probe.txt"))

TASKS = [
    (
        "把你当前可用的 skill 名字全部写进文件 " + OUT + "，一行一个，不要写别的解释。"
        "如果你没有 skill 这个工具，就在文件里只写一行：NO_SKILL_TOOL"
    ),
    (
        "现在用 skill 工具加载 research 这个 skill。加载成功或失败，"
        "都把结果追加写进 " + OUT + "：成功写 OK_RESEARCH，失败写 FAIL_RESEARCH 加原因"
    ),
]


def post(url: str, text: str, session_id: str) -> dict:
    body = json.dumps({"text": text, "session_id": session_id}).encode("utf-8")
    req = urllib.request.Request(
        url + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    chat_id = sender.resolve_target()["to_user_id"]
    for t in TASKS:
        r = post(url, f"*work {t}", chat_id)
        print(f"派发：{r.get('reply','')[:50]!r}")
        time.sleep(70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
