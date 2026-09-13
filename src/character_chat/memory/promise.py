"""未来承诺识别与存储——#4 未来脚本的核心闭环。

当学长在对话里给出带内容的承诺（"等 TorusFold 跑通我就……""以后我们一起……"
"CSP 考完那天我们去……"），自动识别并存成一条 milestone 记忆，带 PROMISE_PREFIX
前缀标记。daemon 端 promise_src 召回时用这个前缀 + 关键词组合判别。

为什么单独成模块：承诺识别是 character_chat 对话路径上的事（record 之后立即跑），
不该让 cli 反向依赖 daemon。召回逻辑在 daemon/promise_src.py，识别+存储在
character_chat/memory/promise.py，两者通过 memory.db 的 PROMISE_PREFIX 前缀契约联动。

铁律：只识别真正的承诺，不把事件叙述误判成承诺（"以后别硬撑"是建议不是承诺）。
判别用"未来触发词 + 动作意图词"组合 + 强承诺短语白名单，跟 daemon 端一致。
"""
import re
from typing import Optional
from loguru import logger

from .schema import MemoryEntry, MemoryImportance, MemoryCategory

# 承诺标记前缀——daemon 端 promise_src 用它识别"这是未来脚本"
PROMISE_PREFIX = "(未来承诺) "

# 强承诺短语——单独出现即认定承诺
STRONG_PROMISE_PHRASES = (
    "以后我们", "以后一起", "等实现", "等跑通", "等考完",
    "等毕业", "等那时候", "等解决", "等做完", "等完成后",
    "等下次", "等下回",
    "考完那天", "考完之后", "考完了",  # "CSP 考完那天……" 无"等"前缀也算
    "跑通了就", "完成了就", "毕业以后",
)

# 未来性触发词 + 动作意图词——组合判别
FUTURE_TRIGGER_WORDS = ("等", "以后", "将来", "到时候", "完事")
FUTURE_INTENT_WORDS = ("就", "要", "会", "一起", "再", "打算", "准备")

# 排除模式——这些虽然是"以后/等"开头，但是建议/关照/即时等待，不是承诺
# （"以后别硬撑""以后注意身体"是关照；"等你回复我再继续"是即时等待非未来脚本）
EXCLUDE_PATTERNS = re.compile(
    r"(以后别|以后不|以后要记得|以后注意|以后要保|以后要注|"
    r"等会儿|等下我再|等你回复|等你回|等会儿再|等下回|"
    r"以后要少|以后要多)"
)


def detect_promise(user_text: str, reply_text: str) -> Optional[str]:
    """从一轮对话识别承诺，返回承诺摘要文本（已带 PROMISE_PREFIX），无则 None。

    识别范围：user_text 优先（学长给出的承诺），reply_text 次之（凌暮雪自己承诺的）。
    返回的摘要用于存成 milestone 记忆——是承诺内容的简述，不是原文照搬。

    策略：
      1. 强承诺短语命中 → 直接认定，提取承诺所在句
      2. 未来触发词 + 动作意图词同时出现 → 认定
      3. 排除模式命中 → 不认定（关照/事件叙述）
    """
    for text in (user_text, reply_text):
        if not text:
            continue
        # 排除模式先过
        if EXCLUDE_PATTERNS.search(text):
            continue
        # 强承诺短语
        if any(p in text for p in STRONG_PROMISE_PHRASES):
            return _extract_promise_sentence(text)
        # 组合判别
        has_trigger = any(w in text for w in FUTURE_TRIGGER_WORDS)
        has_intent = any(w in text for w in FUTURE_INTENT_WORDS)
        if has_trigger and has_intent:
            return _extract_promise_sentence(text)
    return None


def _extract_promise_sentence(text: str) -> str:
    """从文本里提取承诺所在的那一句。

    简单策略：按句号/问号/感叹号分句，挑含触发词+意图词的那句。
    找不到就返回整段（限长 80 字符，避免长独白撑爆 content）。
    """
    sentences = re.split(r"[。！？\n]", text)
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if any(p in s for p in STRONG_PROMISE_PHRASES):
            return s[:80]
        has_trigger = any(w in s for w in FUTURE_TRIGGER_WORDS)
        has_intent = any(w in s for w in FUTURE_INTENT_WORDS)
        if has_trigger and has_intent:
            return s[:80]
    return text.strip()[:80]


def store_promise(long_term, summary: str, scene: Optional[str] = None,
                  time_phase: Optional[str] = None) -> Optional[int]:
    """把承诺摘要存成一条 milestone 记忆，带 PROMISE_PREFIX 前缀。

    Args:
        long_term: LongTermMemory 实例（cli.memory.long_term）
        summary: detect_promise 返回的摘要（不含前缀，本函数加）
        scene/time_phase: 当前场景/时段，用于 daemon 端召回时还原语境

    Returns: row_id 或 None（失败）
    """
    if not summary:
        return None
    content = f"{PROMISE_PREFIX}{summary}"
    try:
        entry = MemoryEntry(
            content=content,
            importance=MemoryImportance.HIGH,  # 承诺是高重要度——未来脚本该被优先召回
            emotion_label="anticipation",
            scene=scene,
            time_phase=time_phase,
            category=MemoryCategory.MILESTONE,
        )
        rid = long_term.store(entry)
        logger.info(f"[Promise] stored future promise: {summary[:50]} (id={rid})")
        return rid
    except Exception as e:
        logger.warning(f"[Promise] store failed: {e}")
        return None


def detect_and_store_promise(long_term, user_text: str, reply_text: str,
                             scene: Optional[str] = None,
                             time_phase: Optional[str] = None) -> Optional[int]:
    """一步到位：识别承诺 + 存成 milestone 记忆。

    cli.py record 之后调这个。无承诺时返回 None（不存）。
    """
    summary = detect_promise(user_text, reply_text)
    if not summary:
        return None
    return store_promise(long_term, summary, scene=scene, time_phase=time_phase)
