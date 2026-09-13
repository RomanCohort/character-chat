"""Memory pipeline - orchestrates short-term, long-term, hippocampus, and retrieval.

Single entry points:
  - MemoryPipeline.record(user_text, reply, emotion, scene, phase) -> record a turn
  - MemoryPipeline.recall(query, top_k) -> retrieve relevant memories
  - MemoryPipeline.consolidate(force_all) -> run hippocampus consolidation
  - MemoryPipeline.seed_if_empty(yaml_path) -> fill long-term with seed memories
"""
from typing import List, Optional, Tuple
from pathlib import Path
import re
from datetime import datetime, timedelta
from loguru import logger

from .schema import MemoryEntry, MemoryImportance, MemoryCategory
from .short_term import ShortTermMemory
from .long_term import LongTermMemory
from .hippocampus import HippocampusConsolidator


# ---------------------------------------------------------------------------
# Category classifier (Plan C: keyword fast-path + LLM tag fallback + scene default)
# ---------------------------------------------------------------------------

# Engineering signals: code, file paths, commands, technical terms
ENG_KEYWORDS = {
    # code markers
    "def ", "class ", "import ", "from ", "return ", "function ",
    # file extensions / paths
    ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md", ".txt", ".sh",
    "/", "\\", "C:", "D:",
    # commands
    "git ", "npm ", "pip ", "python ", "node ", "echo ", "ls ", "cd ",
    "mkdir", "rm ", "cat ", "grep", "find ",
    # technical terms (CN)
    "变量", "函数", "架构", "接口", "模块", "参数", "配置", "部署",
    "数据库", "服务器", "前端", "后端", "框架", "依赖", "环境",
    # task tags
    "<task>", "bug", "error", "exception", "traceback", "debug",
    "commit", "merge", "branch", "pull",
}

# Life signals: personal, emotional, daily, preferences
LIFE_KEYWORDS = {
    # emotions
    "喜欢", "讨厌", "开心", "难过", "生气", "害怕", "紧张", "害羞",
    "累", "困", "饿", "饱", "心情", "情绪", "感动", "委屈",
    # daily life
    "今天", "昨天", "明天", "周末", "放假", "回家", "宿舍", "食堂",
    "吃饭", "睡觉", "熬夜", "起床", "上课", "下课", "考试", "作业",
    # relationships / personal
    "爸妈", "妈妈", "爸爸", "家", "朋友", "同学", "生日",
    "想你", "想你啦", "在干嘛", "在吗",
    # preferences
    "我喜欢", "我不喜欢", "最爱", "偏好",
}

# Phase7-5: Sensory signals — 感官锚点记忆（恋爱里最持久的记忆类型）
# 触发即分类为 SENSORY，召回时加权 + fade 衰减更慢
SENSORY_KEYWORDS = {
    # 触觉
    "手凉", "手暖", "握住", "牵", "靠", "偎", "搂", "碰", "触",
    "体温", "心跳", "呼吸",
    # 嗅觉
    "味道", "香味", "洗衣液", "香水", "气息", "闻到",
    # 视觉细节
    "逆光", "侧脸", "睫毛", "眼神", "低头", "抬头", "看着",
    "光", "影", "黄昏", "晨光",
    # 听觉
    "声音", "笑声", "叹气", "沉默", "安静",
    # 空间/距离感
    "靠近", "走近", "工位", "旁边", "挨着",
}

# LLM tag pattern: <mem:life> or <mem:eng> or <mem:both>
MEM_TAG_RE = re.compile(r"<mem:(life|eng|both)>", re.IGNORECASE)


def classify_category(user_text: str, reply_text: str, scene: Optional[str] = None) -> MemoryCategory:
    """Plan C classifier: sensory first → keyword fast-path → LLM tag → scene default.

    1. Sensory keyword match (Phase7-5: 感官记忆最持久，优先识别)
    2. Keyword match (fast, zero LLM cost) — handles ~80% of cases
    3. LLM tag in reply (free-rider on existing API call) — handles ambiguous
    4. Scene-based default — last resort
    """
    combined = f"{user_text} {reply_text}".lower()

    # Phase7-5: Sensory first — 感官记忆优先识别
    sensory_hits = sum(1 for kw in SENSORY_KEYWORDS if kw.lower() in combined)
    if sensory_hits >= 2:
        return MemoryCategory.SENSORY

    # Layer 1: keyword fast-path
    eng_hits = sum(1 for kw in ENG_KEYWORDS if kw.lower() in combined)
    life_hits = sum(1 for kw in LIFE_KEYWORDS if kw.lower() in combined)

    if eng_hits > 0 and life_hits == 0:
        return MemoryCategory.ENGINEERING
    if life_hits > 0 and eng_hits == 0:
        return MemoryCategory.LIFE
    if eng_hits > 0 and life_hits > 0:
        # Both present — go to LLM tag layer
        pass

    # Layer 2: LLM tag in reply (free-rider)
    tag_match = MEM_TAG_RE.search(reply_text)
    if tag_match:
        tag = tag_match.group(1).lower()
        return {
            "life": MemoryCategory.LIFE,
            "eng": MemoryCategory.ENGINEERING,
            "both": MemoryCategory.BOTH,
        }[tag]

    # Layer 3: scene-based default
    if scene:
        scene_lower = scene.lower()
        if any(w in scene_lower for w in ["实验室", "工位", "项目"]):
            return MemoryCategory.ENGINEERING
        if any(w in scene_lower for w in ["食堂", "宿舍", "咖啡", "图书馆"]):
            return MemoryCategory.LIFE

    # Final fallback
    return MemoryCategory.BOTH


class MemoryPipeline:
    """End-to-end memory pipeline for character chat."""

    def __init__(
        self,
        db_path: str = "data/memory.db",
        max_short_term: int = 20,
        mood_state_path: str = "data/mood_state.json",
    ):
        self.short_term = ShortTermMemory(max_entries=max_short_term)
        self.long_term = LongTermMemory(db_path=db_path)
        self.hippocampus = HippocampusConsolidator(
            short_term=self.short_term,
            long_term=self.long_term,
        )
        # 轻量情绪层（借 CLF 架构，纯 python）：VAD + OU 回归 + 跨会话持久
        from .mood import MoodSystem
        self.mood = MoodSystem(state_path=mood_state_path)

    def record(
        self,
        user_text: str,
        reply_text: str,
        emotion_label: Optional[str] = None,
        scene: Optional[str] = None,
        time_phase: Optional[str] = None,
        importance: MemoryImportance = MemoryImportance.NORMAL,
        category: Optional[MemoryCategory] = None,
    ) -> MemoryEntry:
        """Record a conversation turn into short-term memory.

        Args:
            user_text: User's message.
            reply_text: Character's reply.
            emotion_label: Dominant emotion at this turn.
            scene: Current scene name.
            time_phase: Narrative time phase.
            importance: Memory importance level.
            category: life/engineering/both. If None, auto-classify (Plan C).

        Returns:
            The created MemoryEntry.
        """
        # Combine user + reply for context (the memory should capture the interaction)
        content = f"用户: {user_text}\n角色: {reply_text}"

        # === 情绪层：更新心境（VAD） ===
        # 每轮对话推动心境，存 mood_state.json，跨会话持久
        # 传 user_text：emotion 模型对"想你了"这种直球常识别成 neutral，
        # 用关键词兜底强制推 melting，否则心境不动 → prompt 注入基线 → 反应冷淡
        try:
            self.mood.update(
                emotion_label=emotion_label,
                user_text=user_text,
                reply_text=reply_text,
            )
        except Exception as e:
            logger.warning(f"[Mood] update failed: {e}")

        # Auto-detect importance from emotional intensity
        if emotion_label and emotion_label not in ("neutral", None):
            importance = MemoryImportance.HIGH
        # 情绪强化记忆：高唤醒（被撩/慌乱/忘我）的记忆更值得长期保存
        try:
            if self.mood.importance_from_arousal(importance.value) == "high":
                importance = MemoryImportance.HIGH
        except Exception:
            pass

        # 取当前心境效价写入记忆（借 CLF：记忆带情绪色彩，召回时可做 mood-aware recall）
        try:
            current_valence = self.mood.state.valence
        except Exception:
            current_valence = None

        # Auto-classify category if not provided (Plan C)
        if category is None:
            category = classify_category(user_text, reply_text, scene)

        entry = self.short_term.add(
            content=content,
            emotion_label=emotion_label,
            scene=scene,
            time_phase=time_phase,
            importance=importance,
            emotional_valence=current_valence,
        )
        # Attach category (short_term.add doesn't take it, set on entry)
        entry.category = category
        logger.debug(
            f"[Memory] Recorded: {content[:50]}... "
            f"(importance={importance.value}, category={category.value})"
        )

        # Phase 4: auto-consolidate short_term → long_term
        # record() 只写短期库，recall() 查长期库——不自动 consolidate 的话新记忆
        # 永远 recall 不到，动态记忆不闭环。借 MemGPT 思路：积到阈值就自动固化。
        # 阈值 5 条：太少频繁重建 TF-IDF，太多短期记忆 recall 不到。high importance
        # 的更急，单独走一条更短的阈值（3 条）让它尽快进长期库。
        st_count = self.short_term.size
        high_count = sum(
            1 for e in self.short_term.get_all()
            if e.importance == MemoryImportance.HIGH
        )
        should_consolidate = st_count >= 5 or high_count >= 3
        if should_consolidate:
            try:
                n = self.consolidate()
                if n > 0:
                    logger.info(f"[Memory] auto-consolidated {n} entries → long_term")
            except Exception as e:
                logger.warning(f"[Memory] auto-consolidate failed: {e}")

        return entry

    def recall(self, query: str, top_k: int = 3,
               category: Optional[str] = None,
               min_score: float = 0.3) -> List[str]:
        """Retrieve relevant memories for prompt injection.

        Args:
            query: search query (usually last user message)
            top_k: max results
            category: 'life'/'engineering'/None=all. 'both' memories always match.
            min_score: minimum similarity (0.3 default to control token noise)

        Returns formatted memory strings suitable for prompt injection.
        """
        # 取当前心境效价传给 long_term.search 做 emotion-aware recall
        # （心境消极→召回积极记忆作抚慰；心境积极→同质记忆共鸣）
        try:
            current_mood_valence = self.mood.state.valence
        except Exception:
            current_mood_valence = None

        # Search long-term first
        results = self.long_term.search(
            query, top_k=top_k, min_score=min_score,
            category=category, current_mood_valence=current_mood_valence,
        )

        # Format for prompt
        memories = []
        for entry, score in results:
            cat_tag = f"[{entry.category.value}]" if entry.category.value != "both" else ""
            memories.append(
                f"[{entry.time_phase or '?'}|{entry.scene or '?'}|{entry.emotion_label or 'neutral'}]{cat_tag} {entry.content}"
            )

        return memories

    def consolidate(self, force_all: bool = False) -> int:
        """Run hippocampus consolidation.

        Args:
            force_all: If True, consolidate ALL short-term memories (for /sleep).

        Returns:
            Number of memories consolidated.
        """
        return self.hippocampus.consolidate(force_all=force_all)

    def seed_if_empty(self, yaml_path: str, force: bool = False) -> int:
        """Fill long-term memory with seed memories if DB is empty (#16).

        Reads seed memories from a YAML file and stores them directly into
        long_term with a plausible past timestamp each (random 1-30 days ago),
        so the recency weighting in search() treats them as old-but-relevant
        rather than all-fresh.

        Only runs if long_term.count() == 0 — safe to call on every startup;
        won't duplicate on subsequent runs. Pass force=True to override
        (e.g. backfilling seeds into a DB that already has real memories).

        Returns: number of seeds stored.
        """
        if not force and self.long_term.count() > 0:
            logger.debug("[Memory] seed_if_empty skipped — DB already has memories")
            return 0

        yaml_p = Path(yaml_path)
        if not yaml_p.exists():
            logger.warning(f"[Memory] seed file not found: {yaml_path}")
            return 0

        try:
            import yaml
        except ImportError:
            logger.warning("[Memory] PyYAML not installed — cannot seed")
            return 0

        data = yaml.safe_load(yaml_p.read_text(encoding="utf-8"))
        seeds = data.get("seeds", []) if data else []
        if not seeds:
            return 0

        now = datetime.now()
        n = 0
        for s in seeds:
            try:
                entry = MemoryEntry(
                    content=s["content"],
                    importance=MemoryImportance(s.get("importance", "normal")),
                    emotion_label=s.get("emotion_label"),
                    scene=s.get("scene"),
                    time_phase=s.get("time_phase"),
                    category=MemoryCategory(s.get("category", "both")),
                )
                # 随机过去 1~30 天的时间戳，让种子有合理的时间分布
                # 用 hash(content) 决定性生成 offset，避免每次重启时间乱跳（虽然只在空库跑一次）
                offset_days = 1 + (hash(s["content"]) % 30)
                created_at = now - timedelta(days=offset_days)
                self.long_term.store_seed(entry, created_at=created_at)
                n += 1
            except Exception as e:
                logger.warning(f"[Memory] seed entry skipped: {e}")
        logger.info(f"[Memory] seeded {n} memories from {yaml_path}")
        return n

    def get_stats(self) -> dict:
        """Get memory system stats."""
        stats = self.hippocampus.get_consolidation_stats()
        return stats

    def close(self):
        """Close database connection."""
        self.long_term.close()
