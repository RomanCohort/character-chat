"""FastAPI server wrapper for character_chat CLI.
Exposes POST /chat endpoint for wechat-ai-code-bridge integration.

Supports task execution: when the character's reply contains a <task>...</task>
tag, the server spawns Claude Code CLI to execute the task, feeds the result
back to the character for narration, and returns the narrated reply.
"""
import sys
import os
import re
import json
import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional
from contextlib import asynccontextmanager
from functools import partial

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from loguru import logger

# Ensure src/ is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from character_chat.cli import Cli
from character_chat.config import load_config
from character_chat.live2d import live2d_websocket, get_status
from character_chat.wechat import auth
from character_chat.wechat import bridge as dsh_bridge


# --- Config ---

cli_instances: Dict[str, Cli] = {}
character_name: str = "凌暮雪"

# Task execution settings
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
TASK_TIMEOUT_S = int(os.environ.get("TASK_TIMEOUT_S", "300"))  # Claude Code 子进程启动+干活要久些
TASK_WORKDIR = os.environ.get("TASK_WORKDIR", "D:/wechat-character-bridge")

# proactive scheduler 的 state 文件——/chat 收到消息时记一次互动
PROACTIVE_STATE_FILE = os.environ.get(
    "PROACTIVE_STATE_FILE", r"D:\ling-muxue-logs\proactive_state.json"
)

# --- CORS ---
# 原来是 `allow_origins=["*"]`，那是个真漏洞：任何网页都能向 127.0.0.1:8000
# 发跨域 POST /chat，而 /chat 带 *work 就是"在这台机器上执行命令"。
# 也就是说浏览器开着的时候点进一个恶意页面，等于把机器交出去。
#
# 实测过：本仓库/本机所有调用方（wechat-ai-code-bridge、memory_mcp_server.py、
# scripts/ 下的诊断脚本）全是 node/python 服务端调用，**没有一个浏览器**。
# 所以默认不发任何跨域许可；确需浏览器直连时用 DSH_CORS_ORIGINS 显式列白名单。
_CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("DSH_CORS_ORIGINS", "").split(",")
    if o.strip()
]

# 任务执行走"请求批准"流程（路线1）：
# 凌暮雪识别任务 → 输出 <task_request>描述</task_request> + 微信里问你"可以吗"
# → 你回"可以"/"行" → server 取出 pending task 调子进程执行 → 转述结果
# → 你回"不"/"算了" → 清空 pending，她说"那算了"
# 一次只挂一个 pending（同一 session 内新任务覆盖旧的）。
# Regex 提取 <task_request>...</task_request> 块
TASK_REQUEST_RE = re.compile(r"<task_request>(.*?)</task_request>", re.DOTALL | re.IGNORECASE)
# 兼容旧的 <task> 标签（旧人设卡残留，直接当 approved 处理，不阻塞）
TASK_RE = re.compile(r"<task>(.*?)</task>", re.DOTALL | re.IGNORECASE)

# 每个 session 挂起的待批准任务：session_id -> 任务描述字符串
pending_tasks: Dict[str, str] = {}

# 批准/拒绝意图识别（微信口语，宽松匹配）
APPROVE_PATTERNS = re.compile(
    r"^\s*(可以|行|好的|好|嗯|嗯嗯|OK|ok|Okay|okay|同意|批准|准了|可以吧|成|成的|做吧|执行|跑吧|弄吧|办吧|就这样|去吧|你弄吧|你做吧)\s*[!！。.\?？]?\s*$"
)
REJECT_PATTERNS = re.compile(
    r"^\s*(不|不行|不要|别|算了|放弃|停|拒绝|否|不用了|算了算了|别弄了|别做了)\s*[!！。.\?？]?\s*$"
)


def _touch_proactive():
    """告诉 proactive_scheduler：学长刚跟我互动过，重置 idle 计时。

    轻量写文件，不阻塞；失败静默——不影响主聊天流程。
    """
    try:
        from datetime import datetime
        p = Path(PROACTIVE_STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if p.exists():
            try:
                state = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        state["last_interaction"] = datetime.now().isoformat()
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[touch] failed (non-critical): {e}")


# --- 学长消息情感分类（冷淡/忙碌/热情）---
# 真实大一女生的细腻判定：不是看长度，是看"有没有情感"+"有没有交代"

# 结束性/敷衍语气词（单独发或几乎只发这些 = 冷淡）
_COLD_TOKENS = (
    "哦", "嗯", "嗯嗯", "知道了", "行", "好的", "好", "嗯哼", "哦哦",
    "没事", "无所谓", "随便", "得了", "算了", "就这样", "ok", "OK",
)
# 忙碌信号词（说明他在忙）
_BUSY_TOKENS = ("忙", "在忙", "没空", "没时间", "顾不上", "顾不到", "抽不开", "走不开", "忙完")
# 承诺/交代词（忙但有交代 → 不冷淡）
_COMMIT_TOKENS = (
    "晚上", "明天", "后天", "等", "之后", "回头", "找你", "回来", "忙完",
    "找你聊", "晚点", "稍后", "待会", "一会", "一会儿", "结束", "下课", "下班",
)
# 情感/亲密词（热情信号）
_AFFECTION_TOKENS = (
    "想", "想你", "想你了", "喜欢你", "爱你", "喜欢", "爱", "在意", "担心你",
    "心疼", "想你啦", "想你嘛", "想你呢", "亲", "抱", "抱抱", "晚安", "早安",
    "梦到", "梦", "好想你", "想你啊", "想你哟",
)

# mood_state.json 路径（凌暮雪的心境状态）
MOOD_STATE_FILE = os.environ.get(
    "MOOD_STATE_FILE", r"D:\character_chat\data\mood_state.json"
)


def classify_user_message(text: str) -> str:
    """把学长的消息分到四类之一：enthusiastic / busy_warm / cold / neutral。

    真实大一女生的细腻判定——不是看长度，是看"有没有情感"+"有没有交代"。

    规则（按优先级）：
      1. 含情感/亲密词 → enthusiastic（不管长短，"想你"两个字也是热情）
      2. 含忙碌信号词 → busy_warm（"我有点忙"、"在忙"，不管有没有交代都算）
      3. 短消息 + 没有实质内容 → cold（"哦"、"嗯"、"知道了"、"好"、"行"）
      4. 否则 → neutral（正常对话）
    """
    if not text:
        return "neutral"
    t = text.strip()

    # 1. 热情：含情感/亲密词（优先级最高）
    for tok in _AFFECTION_TOKENS:
        if tok in t:
            return "enthusiastic"

    # 2. 忙碌：含忙碌信号词（不管有没有交代都算 busy_warm，她理解为他在忙）
    if any(tok in t for tok in _BUSY_TOKENS):
        return "busy_warm"

    # 3. 冷淡：短消息，且没有实质内容（只发了语气词/简单回应）
    # 判定方式：去掉所有语气词和标点后，剩余字符 ≤ 3 → cold
    if len(t) <= 15:  # 只在短消息里做判定，长消息不算冷淡
        noise = set("，。！？,.!?哈嘛呀呢吧啊哦额嗯好了的")
        rest = "".join(c for c in t if c not in noise and not c.isspace())
        if len(rest) <= 3:
            return "cold"

    # 4. 默认中性
    return "neutral"


def update_mood_for_classification(category: str) -> None:
    """根据消息分类，更新 mood_state.json 里的 valence/dominance（轻量写入）。

    - enthusiastic: valence +0.08, dominance +0.03（被他在意，开心一点）
    - busy_warm:    不变（理解他在忙，不退缩也不开心）
    - cold:         valence -0.12, dominance -0.04（被冷了一点，更不确定）
    - neutral:      不变

    单次写入幅度小，靠 scheduler 那边的时间衰减 + 这边的累积。
    """
    deltas = {
        "enthusiastic": (0.08, 0.03),
        "busy_warm":    (0.0, 0.0),
        "cold":         (-0.12, -0.04),
        "neutral":      (0.0, 0.0),
    }
    d_val, d_dom = deltas.get(category, (0.0, 0.0))
    if d_val == 0.0 and d_dom == 0.0:
        return  # neutral/busy_warm 不动 mood

    try:
        import time
        p = Path(MOOD_STATE_FILE)
        if not p.exists():
            return
        mood = json.loads(p.read_text(encoding="utf-8"))
        v = mood.get("valence", 0.0)
        d = mood.get("dominance", 0.0)
        mood["valence"] = max(-1.0, min(1.0, v + d_val))
        mood["dominance"] = max(-1.0, min(1.0, d + d_dom))
        mood["last_update_ts"] = time.time()
        # history 追加一条简短记录
        hist = mood.setdefault("history", [])
        hist.append({
            "ts": time.time(),
            "emotion": f"user:{category}",
            "valence": mood["valence"],
            "arousal": mood.get("arousal", 0.0),
            "dominance": mood["dominance"],
        })
        # history 最多留 60 条
        if len(hist) > 60:
            del hist[: len(hist) - 60]
        p.write_text(json.dumps(mood, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[mood] update failed (non-critical): {e}")


def record_interaction_by_category(category: str) -> None:
    """根据消息分类，决定怎么写 proactive_state.json。

    - enthusiastic / neutral: 正常重置 idle，清掉等待状态（他回来了）
    - busy_warm: 不重置 idle，进入 waiting_mode="busy"（他在忙，有交代）
    - cold:      不重置 idle，进入 waiting_mode="cold"（他冷淡了）
    """
    from datetime import datetime
    try:
        p = Path(PROACTIVE_STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if p.exists():
            try:
                state = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                state = {}

        if category in ("enthusiastic", "neutral"):
            # 他正常互动 → 重置 idle，退出等待
            state["last_interaction"] = datetime.now().isoformat()
            state["waiting_mode"] = None
            state["waiting_since"] = None
        else:
            # busy_warm / cold → 不重置 last_interaction（idle 继续走），
            # 但记一个"他来过但冷淡/忙"的时间戳，scheduler 用来选问句类型
            state["last_touch_cold"] = datetime.now().isoformat()
            state["waiting_mode"] = "busy" if category == "busy_warm" else "cold"
            state["waiting_since"] = datetime.now().isoformat()

        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[record] failed (non-critical): {e}")


def trigger_petty_if_cold_streak() -> None:
    """检测连续 cold 次数，达到阈值触发 petty_state（生闷气）。

    连续 2 次 cold → 触发 sulking（闷 3 小时）
    连续 4 次 cold → 触发 angry（气 2 小时，等级更高）
    重置条件：enthusiastic / neutral 分类后清零计数
    """
    try:
        p = Path(PROACTIVE_STATE_FILE)
        if not p.exists():
            return
        state = json.loads(p.read_text(encoding="utf-8"))

        # 累计 cold 次数（proactive_state.json 里没有这个字段就初始化为 0）
        cold_count = state.get("cold_streak_count", 0)

        # 检查当前 waiting_mode
        waiting_mode = state.get("waiting_mode")
        if waiting_mode == "cold":
            cold_count += 1
            state["cold_streak_count"] = cold_count

            # 达到阈值 → 触发 petty_state
            if cold_count >= 4:
                # 连续 4 次 cold → 生气（等级高）
                from character_chat.memory.mood import MoodSystem
                mood_mgr = MoodSystem(str(Path(MOOD_STATE_FILE)))
                mood_mgr.trigger_petty("cold_streak", duration_hours=2.0)
                logger.info(f"[petty] triggered angry (cold_streak={cold_count})")
            elif cold_count >= 2:
                # 连续 2 次 cold → 闷气
                from character_chat.memory.mood import MoodSystem
                mood_mgr = MoodSystem(str(Path(MOOD_STATE_FILE)))
                mood_mgr.trigger_petty("cold_streak", duration_hours=3.0)
                logger.info(f"[petty] triggered sulking (cold_streak={cold_count})")
        else:
            # 非 cold → 重置计数
            state["cold_streak_count"] = 0

        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[petty] cold streak check failed: {e}")


OVERBOARD_JOKE_KEYWORDS = ["笨蛋", "笨", "傻", "蠢", "笑死你了", "哈哈哈你", "真差劲", "没用", "丢人"]  # 过头玩笑关键词

# 吃醋关键词（命中累积 jealous_streak，达到阈值触发 petty angry）
JEALOUS_KEYWORDS = ["她是谁", "那个女生", "学姐", "隔壁组", "她今天", "她帮我", "她代码"]


def trigger_petty_if_overboard_joke(user_text: str) -> None:
    """检测过头玩笑关键词，触发 petty_state。

    关键词命中 → 触发 sulking（闷 2 小时）
    """
    try:
        for kw in OVERBOARD_JOKE_KEYWORDS:
            if kw in user_text:
                from character_chat.memory.mood import MoodSystem
                mood_mgr = MoodSystem(str(Path(MOOD_STATE_FILE)))
                mood_mgr.trigger_petty("overboard_joke", duration_hours=2.0)
                logger.info(f"[petty] triggered sulking (overboard_joke: {kw})")
                return
    except Exception as e:
        logger.debug(f"[petty] overboard joke check failed: {e}")


def trigger_petty_if_jealous_spike(user_text: str) -> None:
    """检测吃醋关键词累积，达到阈值触发 petty angry。

    单次命中 → jealous 情绪（mood.update 已处理，不触发 petty）
    连续 2 次命中（jealous_streak >= 2）→ 触发 angry（气 2 小时）
    非 jealous 消息 → 重置计数
    """
    try:
        p = Path(PROACTIVE_STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if p.exists():
            try:
                state = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                state = {}

        has_jealous_kw = any(kw in user_text for kw in JEALOUS_KEYWORDS)
        streak = state.get("jealous_streak", 0)

        if has_jealous_kw:
            streak += 1
            state["jealous_streak"] = streak
            if streak >= 2:
                # 连续 2 次吃醋 → 气愤（等级高于 sulking）
                from character_chat.memory.mood import MoodSystem
                mood_mgr = MoodSystem(str(Path(MOOD_STATE_FILE)))
                mood_mgr.trigger_petty("jealous_spike", duration_hours=2.0)
                logger.info(f"[petty] triggered angry (jealous_spike streak={streak})")
        else:
            state["jealous_streak"] = 0

        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[petty] jealous spike check failed: {e}")




@asynccontextmanager
async def lifespan(app):
    """Initialize Cli instance on startup."""
    logger.info(f"Initializing character_chat with character: {character_name}")

    original_cwd = os.getcwd()
    project_root = Path(__file__).resolve().parents[2]  # D:/character_chat
    os.chdir(project_root)

    try:
        # 不在启动时**等待**凌暮雪构造（那是同步的、要一分钟），但放个后台任务去建，
        # 这样第一条消息不用现等。`*work` 那条路完全不依赖她。
        asyncio.create_task(_warmup_cli())
        logger.info("character_chat ready (*work 通道已可用；凌暮雪后台预热中)")
        yield
    finally:
        await dsh_bridge.shutdown()
        os.chdir(original_cwd)
        cli_instances.clear()
        logger.info("Cli instances cleared")


app = FastAPI(
    title="Character Chat API",
    description="API wrapper for character_chat CLI — connects to wechat-ai-code-bridge",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


# --- Request/Response schemas ---


class ChatRequest(BaseModel):
    text: str
    session_id: str = "default"
    skip_record: bool = False  # scheduler 等内部 prompt 调用时设 True，不污染记忆库


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    character: str
    task_executed: bool = False


class StatusResponse(BaseModel):
    status: str
    character: str
    active_sessions: int


# --- 凌暮雪实例：懒加载 + 只建一次 ---

_cli_lock = asyncio.Lock()
# cli.send 会改 history / emotion / mood，不是线程安全的。普通聊天和 DSH 转述
# 都跑在线程池里，所以用一把进程内的锁把它们串起来（锁在 executor 线程里拿）。
_cli_call_lock = threading.Lock()


def _send_locked(cli: Cli, text: str, skip_record: bool = False) -> str:
    with _cli_call_lock:
        return cli.send(text, skip_record=skip_record)


async def _get_cli(session_id: str = "default") -> Cli:
    """取「default」实例，没有就建一个（并且把转述器注册给微信桥）。

    建实例要几秒（要载 embedding 之类），所以扔线程池，别卡住 event loop——
    /chat 是 async 的，直接在这儿同步构造会把别的请求一起堵住。
    """
    cli = cli_instances.get("default")
    if cli is not None:
        return cli
    async with _cli_lock:
        cli = cli_instances.get("default")
        if cli is not None:
            return cli
        logger.info(f"Initializing default Cli instance (session_id={session_id})")
        project_root = Path(__file__).resolve().parents[2]
        original_cwd = os.getcwd()

        def _build() -> Cli:
            os.chdir(project_root)
            try:
                return Cli(character_name=character_name, neural_enabled=False)
            finally:
                os.chdir(original_cwd)

        cli = await asyncio.get_running_loop().run_in_executor(None, _build)
        cli_instances["default"] = cli
        # DSH 的结果要以她的名义发出去，所以把她的 send 注给微信桥。
        # skip_record=True：这轮只是让她把话转述一遍，那段 [系统提示：...] 不是学长说的话，
        # 不该当真实对话写进记忆库（之前 scheduler 的 prompt 就是这么污染了 37 条）。
        dsh_bridge.set_narrator(partial(_send_locked, cli, skip_record=True))
        logger.info("凌暮雪实例就绪，转述器已注册")
        return cli


async def _warmup_cli() -> None:
    """启动后就偷偷把她的实例建起来。

    不建的话，第一条消息（不管是你发的还是转述）都要等一分钟以上——载 embedding
    时连 huggingface 超时重试 5 轮，而微信桥那侧等不了。放后台，启动不受影响。
    """
    try:
        await _get_cli("warmup")
    except Exception as e:
        logger.warning(f"凌暮雪预热失败（不影响 *work 通道）：{e!r}")


# --- Task execution helpers ---


def _extract_task(reply: str) -> Optional[str]:
    """Extract the first <task>...</task> block from a reply.

    Returns the inner text (stripped) if found, else None.
    """
    m = TASK_RE.search(reply)
    if not m:
        return None
    return m.group(1).strip()


def _extract_task_request(reply: str) -> Optional[str]:
    """Extract the first <task_request>...</task_request> block.

    凌暮雪识别到任务后，用人设卡约定输出 <task_request>自然语言描述</task_request>
    而不是直接 <task>。这个标签意味着"我想做这个，等学长批准"——不立即执行。
    """
    m = TASK_REQUEST_RE.search(reply)
    if not m:
        return None
    return m.group(1).strip()


def _strip_all_task_tags(reply: str) -> str:
    """Remove <task> and <task_request> blocks from a reply (for display)."""
    reply = TASK_REQUEST_RE.sub("", reply)
    reply = TASK_RE.sub("", reply)
    return reply.strip()


def _strip_task_tag(reply: str) -> str:
    """Remove <task>...</task> blocks from a reply (for display)."""
    return TASK_RE.sub("", reply).strip()


def _execute_task(task_text: str) -> str:
    """Execute a task via Claude Code CLI subprocess (headless --print mode).

    不再让凌暮雪自己生成 shell 命令（她写 Git Bash 语法但 server 跑 cmd/bash
    会语义冲突，且容易生成危险/空命令如 `find D:/ -iname ""`）。
    改为：凌暮雪输出自然语言任务描述 → 起一个 Claude Code 子进程，
    让它用 Read/Write/Edit/Bash 工具真正执行 → 把结果回传给凌暮雪转述。

    这样执行能力走 Claude Code（强、安全、有权限模式），凌暮雪只需描述意图。

    Args:
        task_text: 自然语言任务描述，e.g. "在 D:/ling-muxue-logs/ 建一个
                   test.txt，内容写 hello"。不再是 shell 命令。

    Returns:
        Claude Code 的最终输出文本（truncated 2000 chars）。
    """
    logger.info(f"[task] executing via Claude Code: {task_text[:120]}")

    # 找 claude CLI：shutil.which 在后台进程 PATH 不全时可能找不到，
    # 回退到 npm 全局安装的已知路径。Windows 上优先用 .cmd 包装器。
    import shutil
    claude_bin = shutil.which(CLAUDE_BIN)
    if claude_bin is None:
        # 回退候选：npm 全局 bin 目录
        candidates = [
            os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.expandvars(r"%APPDATA%\npm\claude"),
            # 别写死用户目录（原来这里是 /c/Users/<名字>/...）：既是本机用户名泄漏，
            # 换台机器也永远命中不了。%APPDATA% 展开就是同一个位置。
            os.path.expandvars(r"%USERPROFILE%\AppData\Roaming\npm\claude.cmd"),
        ]
        for c in candidates:
            if os.path.exists(c):
                claude_bin = c
                break
    if claude_bin is None:
        logger.error(f"[task] claude CLI not found in PATH or npm global")
        return f"(唔，我这边执行环境有点问题……你手动看看？)"
    logger.debug(f"[task] using claude at: {claude_bin}")

    try:
        # 用 stdin 传 prompt，避免参数解析歧义（--add-dir 等选项会吞掉后面的位置参数）
        # --dangerously-skip-permissions: 批准门已在对话层（<task_request>+学长"可以"），
        # 子进程本身不再卡权限——否则 --print 非交互模式下遇到写操作会直接跳过。
        # 安全边界：只有学长在微信里明确说"可以/行/好"后才会走到这里，等于人工批准。
        result = subprocess.run(
            [claude_bin, "-p", "--output-format", "text",
             "--dangerously-skip-permissions",
             "--add-dir", TASK_WORKDIR],
            input=task_text,
            cwd=TASK_WORKDIR,
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            hint = f"(Claude Code 退出码 {result.returncode})"
            output = (output + "\n" + err + "\n" + hint).strip() if output or err else hint
            logger.warning(f"[task] FAILED code={result.returncode}: {output[:200]}")
            return output[:2000]
        if not output:
            output = "(执行完成，无文本输出)"
        logger.info(f"[task] done, output {len(output)} chars, code=0")
        return output[:2000]
    except subprocess.TimeoutExpired:
        logger.error(f"[task] timed out after {TASK_TIMEOUT_S}s")
        return f"(凌暮雪现在有点忙不过来，等下再回你好不好？)"
    except FileNotFoundError:
        logger.error(f"[task] claude CLI not found: {CLAUDE_BIN}")
        return f"(唔，我这边执行环境有点问题……你手动看看？)"
    except Exception as e:
        logger.error(f"[task] error: {e}")
        return f"(唔，这个没做成……可能哪里出问题了)"


# --- Endpoints ---


@app.get("/", response_model=StatusResponse)
async def root():
    """Health check. 不要求登录——otherwise 没法探活。

    注意它只回状态，不泄漏会话内容 / 记忆 / 任何可操作信息。
    """
    return StatusResponse(
        status="running",
        character=character_name,
        active_sessions=len(cli_instances),
    )


# --- 认证 ---


class LoginRequest(BaseModel):
    username: str
    password: str


async def require_auth(request: Request) -> str:
    """端点守卫：本机免登录，其它来源必须带有效会话 cookie。

    返回身份字符串（`local` 或用户名），只用于日志。
    """
    client_host = request.client.host if request.client else None
    if auth.is_local(request.headers.get("host"), client_host):
        return "local"
    user = auth.verify_session(request.cookies.get(auth.COOKIE_NAME, ""))
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="需要登录：POST /auth/login 拿会话 cookie",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


@app.post("/auth/login")
async def auth_login(req: LoginRequest, response: Response, request: Request):
    """账号密码换会话 cookie。"""
    if not auth.list_users():
        raise HTTPException(
            status_code=503,
            detail="还没有任何账号。在机器上跑：python scripts/dsh_auth.py add <用户名>",
        )
    if not auth.check_credentials(req.username, req.password):
        logger.warning(f"[auth] 登录失败 user={req.username!r} from={request.client.host if request.client else '?'}")
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    value = auth.issue_session(req.username.strip())
    response.set_cookie(
        auth.COOKIE_NAME,
        value,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        # 局域网是纯 HTTP，设 Secure 会导致 cookie 根本发不出去。
        secure=False,
    )
    logger.info(f"[auth] 登录成功 user={req.username!r}")
    return {"ok": True, "username": req.username.strip(), "expires_days": auth.SESSION_DAYS}


@app.post("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/auth/me")
async def auth_me(request: Request, _: str = Depends(require_auth)):
    """当前身份。前端用它判断要不要跳登录页。"""
    client_host = request.client.host if request.client else None
    local = auth.is_local(request.headers.get("host"), client_host)
    return {
        "local": local,
        "username": auth.verify_session(request.cookies.get(auth.COOKIE_NAME, "")),
        "users_exist": bool(auth.list_users()),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _: str = Depends(require_auth)):
    """Main chat endpoint — called by wechat-ai-code-bridge.

    需要认证：本机来源直接放行，其它来源要会话 cookie。见 wechat/auth.py。

    Flow:
      1. Call cli.send(text) → get character reply
      2. If reply contains <task>...</task>:
         a. Extract task instruction
         b. Execute via Claude Code CLI
         c. Feed result back to character for narration
         d. Return narrated reply
      3. Else: return reply as-is
    """
    session_id = req.session_id or "default"

    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text field cannot be empty")

    user_text = req.text.strip()

    # === DSH 命令通道 ===
    # 以 !dsh 开头的消息交给 DSH 常驻会话跑，不经过凌暮雪的人设/记忆/心境管线。
    # 派发是异步的：这里只回一句回执，真正的结果由 bridge 模块直接发回微信。
    if dsh_bridge.is_command(user_text):
        logger.info(f"[dsh] command from session={session_id}: {user_text[:80]}")
        _touch_proactive()  # 人在，别让 proactive 插话
        receipt = await dsh_bridge.dispatch(session_id, user_text, to_user_id=session_id)
        return ChatResponse(
            reply=receipt,
            session_id=session_id,
            character="dsh",
            task_executed=False,
        )

    # 单人场景：凌暮雪只服务学长一个人，所有 session_id 统一映射到 "default" 实例。
    # 否则 bridge 用 wxid 之类调 /chat 会建独立 cli，记忆/心境/历史各自一份，
    # 跟 /memory/* 端点（硬编码查 "default"）对不上——记忆写进 A 实例，stats 查 B 实例。
    # pending_tasks 仍按 session_id 隔离（多个微信会话的待批准任务不串台）。
    cli = await _get_cli(session_id)

    # 学长发消息来了 → 分类他的消息，决定是否重置 idle + 怎么影响 mood
    # (冷淡/忙碌/热情/中性，对应不同行为)
    category = classify_user_message(user_text)
    logger.info(f"[classify] user message: {user_text[:50]} → {category}")
    record_interaction_by_category(category)  # 根据分类写 proactive_state
    update_mood_for_classification(category)  # 根据分类更新 mood_state

    # petty_state 触发检测：连续 cold / 过头玩笑 / 吃醋累积
    trigger_petty_if_cold_streak()
    trigger_petty_if_overboard_joke(user_text)
    trigger_petty_if_jealous_spike(user_text)

    task_executed = False

    try:
        loop = asyncio.get_event_loop()

        # === 请求批准流程（路线1）：先看有没有 pending task，这次消息是不是批准/拒绝 ===
        pending = pending_tasks.get(session_id)
        is_approve = bool(APPROVE_PATTERNS.match(user_text))
        is_reject = bool(REJECT_PATTERNS.match(user_text))

        if pending and is_approve:
            # 学长批准了上一个待办任务 → 真正执行
            logger.info(f"[task] approved, executing: {pending[:80]}")
            task_executed = True
            task_output = await loop.run_in_executor(None, _execute_task, pending)
            # 清空 pending（执行了就不管成功失败都清）
            pending_tasks.pop(session_id, None)
            # 让凌暮雪转述结果——告诉她任务已执行
            narration_prompt = (
                f"[系统提示：学长刚才批准了你提出的任务「{pending}」，"
                f"你已经执行完了。以下是执行结果：]\n{task_output}\n"
                f"[请用你的语气把结果转述给学长，简短自然。如果结果显示失败/出错，"
                f"就如实说没做成，不要谎报成功。]"
            )
            reply = await loop.run_in_executor(None, _send_locked, cli, narration_prompt)
            return ChatResponse(
                reply=reply, session_id=session_id,
                character=cli.card.bot_name, task_executed=task_executed,
            )

        if pending and is_reject:
            # 学长拒绝了 → 清空 pending，让凌暮雪自然回应
            logger.info(f"[task] rejected by user: {pending[:80]}")
            pending_tasks.pop(session_id, None)
            # 不走 cli.send 正常流程，直接给个凌暮雪式的回应
            reply = "……好，那算了。"
            return ChatResponse(
                reply=reply, session_id=session_id,
                character=cli.card.bot_name, task_executed=False,
            )

        # === 正常聊天流程 ===
        # skip_record=True 时跳过记忆记录（scheduler 等内部 prompt 调用用，避免污染）
        reply = await loop.run_in_executor(
            None, partial(_send_locked, cli, skip_record=req.skip_record), user_text
        )

        # 检查 reply 里有没有 <task_request>（凌暮雪想做事，等批准）
        task_req = _extract_task_request(reply)
        if task_req:
            # 暂存为 pending，不执行；下次学长回复批准时才执行
            pending_tasks[session_id] = task_req
            logger.info(f"[task] pending approval: {task_req[:80]}")
            # 把 <task_request> 标签从回复里剥掉（不显示给学长）
            reply = _strip_all_task_tags(reply)
            return ChatResponse(
                reply=reply, session_id=session_id,
                character=cli.card.bot_name, task_executed=False,
            )

        # 兼容旧 <task> 标签（直接执行，不阻塞）——旧人设卡残留路径
        task_text = _extract_task(reply)
        if task_text:
            task_executed = True
            logger.info(f"[task] legacy <task> tag, executing...")
            task_output = await loop.run_in_executor(None, _execute_task, task_text)
            clean_reply = _strip_task_tag(reply)
            narration_prompt = (
                f"[系统提示：你刚才执行了任务「{task_text}」，以下是执行结果：]\n{task_output}\n"
                f"[请用你的语气把这个结果转述给学长，简短自然，不要照搬原始输出。"
                f"如果失败就如实说没做成。]"
            )
            narrated = await loop.run_in_executor(None, _send_locked, cli, narration_prompt)
            final_reply = clean_reply
            if clean_reply and narrated:
                final_reply = f"{clean_reply}\n\n{narrated}"
            elif narrated:
                final_reply = narrated
            reply = final_reply

        return ChatResponse(
            reply=reply,
            session_id=session_id,
            character=cli.card.bot_name,
            task_executed=task_executed,
        )

    except Exception as e:
        logger.error(f"Error processing chat: {e}")
        # Gentle fallback: never expose raw error to WeChat user
        raise HTTPException(
            status_code=500,
            detail="自动回复：我刚断线啦一下……现在恢复了，再发一次试试？",
        )


@app.get("/touch")
async def touch():
    """记录一次互动（重置 proactive scheduler 的 idle 计时）。

    被 bridge 收到学长消息时调用（或 /chat 内部自动调）。
    """
    _touch_proactive()
    return {"status": "touched", "character": character_name}


# --- Live2D WebSocket (#22 Phase 1) ---
# 前端 Electron / 网页连这里，实时接收情绪参数 + 回复文本。
# 订阅的是 cli.bus 进程内事件，零跨进程开销。

@app.websocket("/ws/live2d")
async def ws_live2d(websocket: WebSocket):
    """Live2D 实时事件流。

    协议见 character_chat/live2d/bridge.py。
    前端连上后发 {"type":"hello"} 握手，之后收 emotion_updated / message_sent。
    """
    await live2d_websocket(websocket)


@app.get("/live2d/status")
async def live2d_status():
    """查 Live2D 桥状态：连接数、订阅状态、当前表情参数。"""
    return get_status()


# --- 共享记忆端点 (#12) ---
# 给 MCP server / 终端 Claude Code 远程调用，让两端共享同一个 memory.db。
# 不另开 sqlite 连接——通过 HTTP 走 character_chat 进程，TF-IDF 索引只有一份。

from character_chat.memory.schema import MemoryImportance, MemoryCategory


class MemStoreReq(BaseModel):
    content: str
    importance: str = "normal"      # low/normal/high
    category: str = "both"         # life/engineering/both
    emotion_label: str | None = None
    scene: str | None = None
    time_phase: str | None = None


class MemSearchReq(BaseModel):
    query: str
    top_k: int = 5
    min_score: float = 0.1
    category: str | None = None


@app.post("/memory/store")
async def memory_store(req: MemStoreReq, _: str = Depends(require_auth)):
    """存一条记忆到共享库（终端 Claude Code 用）。"""
    cli = cli_instances.get("default")
    if cli is None:
        raise HTTPException(503, "memory engine not ready")
    from character_chat.memory.schema import MemoryEntry
    try:
        entry = MemoryEntry(
            content=req.content,
            importance=MemoryImportance(req.importance),
            category=MemoryCategory(req.category),
            emotion_label=req.emotion_label,
            scene=req.scene,
            time_phase=req.time_phase,
        )
        rid = cli.memory.long_term.store(entry)
        return {"id": rid, "status": "stored"}
    except Exception as e:
        raise HTTPException(500, f"store failed: {e}")


@app.post("/memory/search")
async def memory_search(req: MemSearchReq, _: str = Depends(require_auth)):
    """检索共享记忆（带 #17 加权机制）。返回格式化后的记忆串 + 原始分。"""
    cli = cli_instances.get("default")
    if cli is None:
        raise HTTPException(503, "memory engine not ready")
    try:
        results = cli.memory.long_term.search(
            req.query, top_k=req.top_k, min_score=req.min_score,
            category=req.category,
        )
        return {
            "count": len(results),
            "results": [
                {
                    "id": e.id,
                    "content": e.content,
                    "importance": e.importance.value,
                    "category": e.category.value,
                    "emotion_label": e.emotion_label,
                    "scene": e.scene,
                    "time_phase": e.time_phase,
                    "score": round(float(score), 4),
                }
                for e, score in results
            ],
        }
    except Exception as e:
        raise HTTPException(500, f"search failed: {e}")


@app.get("/memory/stats")
async def memory_stats(_: str = Depends(require_auth)):
    """共享记忆库统计。"""
    cli = cli_instances.get("default")
    if cli is None:
        raise HTTPException(503, "memory engine not ready")
    stats = cli.memory.get_stats()
    stats["long_term_count"] = cli.memory.long_term.count()
    return stats


@app.get("/memory/milestone")
async def memory_milestone(within_days: int = 7, _: str = Depends(require_auth)):
    """查未来 N 天内即将到来的纪念日（Phase3 里程碑主动话题）。

    daemon topic_sources.get_milestone_topics() 调这个，找"在一起 N 天""相识周年"等。
    """
    cli = cli_instances.get("default")
    upcoming = cli.memory.long_term.get_upcoming_milestone(within_days=within_days)
    return {"results": upcoming, "count": len(upcoming)}


@app.post("/memory/seed")
async def memory_seed_endpoint(force: bool = False, _: str = Depends(require_auth)):
    """手动触发种子记忆灌入。

    默认仅在空库时生效；传 force=true 则强制补灌（用于往已有真实记忆的库回填种子）。
    供 MCP server / 终端 Claude Code 调用。
    """
    cli = cli_instances.get("default")
    if cli is None:
        raise HTTPException(503, "memory engine not ready")
    project_root = Path(__file__).resolve().parents[2]
    seed_path = str(project_root / "config" / "ling_muxue_seed_memories.yaml")
    n = cli.memory.seed_if_empty(seed_path, force=force)
    if n == 0:
        return {"message": "记忆库已有记忆，未灌种子（已跳过）", "seeded": 0}
    return {"message": f"已灌入 {n} 条种子记忆", "seeded": n}


@app.post("/memory/backfill")
async def memory_backfill(_: str = Depends(require_auth)):
    """全量回填 embedding：给所有无向量的存量记忆编码 BGE 向量。

    升级到 RAG 后一次性补全旧记忆（41 条存量原本只有 TF-IDF char n-gram）。
    耗时取决于记忆条数 + 是否首次加载 embedding 模型（首次 ~6s 模型加载）。
    供升级后手动触发；之后新写入的记忆 store 时已自动编码。
    """
    cli = cli_instances.get("default")
    if cli is None:
        raise HTTPException(503, "memory engine not ready")
    import asyncio
    loop = asyncio.get_event_loop()
    n = await loop.run_in_executor(None, cli.memory.long_term.backfill_all)
    return {"backfilled": n, "total": cli.memory.long_term.count()}


@app.get("/sessions")
async def list_sessions(_: str = Depends(require_auth)):
    """List active sessions."""
    return {
        "sessions": list(cli_instances.keys()),
        "count": len(cli_instances),
    }


@app.delete("/session/{session_id}")
async def clear_session(session_id: str, _: str = Depends(require_auth)):
    """Clear a specific session (reset memory)."""
    if session_id in cli_instances:
        del cli_instances[session_id]
        return {"status": "cleared", "session_id": session_id}
    raise HTTPException(status_code=404, detail="Session not found")


# --- Entry point ---


if __name__ == "__main__":
    import uvicorn

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
