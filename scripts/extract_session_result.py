"""从 DSH 会话日志里把最后一轮助手正文捞出来。

用途：结果生成出来了、但因为 iLink 回复窗口关闭而发不出去时，
内容并没丢——它在 DSH 的会话日志里（`$DSH_HOME/sessions/**/session.v3.jsonl.zstd`）。
这个脚本把它解压、取最后一个 assistant/message 的正文块。

    python scripts/extract_session_result.py <session.v3.jsonl.zstd 路径或会话 id>
"""
import json
import sys
import zstandard as zstd
from pathlib import Path

DSH_SESSIONS = Path.home() / ".dsh" / "sessions"


def find_session(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    # 当成会话 id 前缀找
    hits = [f for f in DSH_SESSIONS.rglob("session.v3.jsonl.zstd") if arg in str(f)]
    if not hits:
        raise SystemExit(f"找不到会话: {arg}")
    hits.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return hits[0]


def load_events(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        raw = zstd.ZstdDecompressor().stream_reader(f).read()
    events = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def last_assistant_text(events: list[dict]) -> str:
    """取最后一个非空 assistant/message 的 text 块（reasoning 不算）。"""
    for ev in reversed(events):
        if ev.get("type") != "assistant/message":
            continue
        content = ((ev.get("data") or {}).get("message") or {}).get("content") or []
        chunks = [c.get("text", "") for c in content
                  if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(x for x in chunks if x).strip()
        if text:
            return text
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = find_session(sys.argv[1])
    events = load_events(path)
    text = last_assistant_text(events)
    print(f"会话文件: {path}")
    print(f"事件数: {len(events)}   最后正文长度: {len(text)}")
    if not text:
        print("（没找到正文）")
        return 1
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"已写入: {out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print("--- 正文 ---")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
