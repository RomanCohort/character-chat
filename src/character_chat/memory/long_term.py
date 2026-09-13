"""Long-term memory store (sqlite + embedding vector retrieval).

升级自 TF-IDF → BGE-small-zh embedding（512维，中文语义检索）。
- store 时编码 content 存 BLOB
- search 时编码 query → cosine（归一化向量点积）
- 保留 importance/recency/category/mood 加权重排
- embedding 模型不可用时降级到 TF-IDF fallback，不让检索挂掉
"""
import json
import sqlite3
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from loguru import logger

from .schema import MemoryEntry, MemoryType, MemoryImportance, MemoryCategory
from .embedding import get_encoder, encode_query, EmbeddingEncoder


# ---------------------------------------------------------------------------
# 记忆权重机制 (#17) — 最终检索分 = 加权组合，不再纯靠 TF-IDF
# ---------------------------------------------------------------------------
# importance → 数值映射：high 事件记忆权重最高，low 闲聊最低
IMPORTANCE_WEIGHT = {
    MemoryImportance.HIGH.value:   1.0,
    MemoryImportance.NORMAL.value: 0.6,
    MemoryImportance.LOW.value:     0.3,
}
# 各维度权重（保守默认，可调）：
W_SEMANTIC  = 0.6   # 语义相似度（TF-IDF 余弦）
W_IMPORTANCE = 0.25  # 重要度
W_RECENCY   = 0.15  # 时间新鲜度
# emotion-aware recall 调节幅度（乘性，借 CLF mood 层）
# 心境偏消极时积极记忆微加（mood repair），偏积极时同质记忆共鸣
W_MOOD = 0.15
# access_count 钝化：被反复想起的记忆轻微降权，避免总召回同一条
ACCESS_DECAY_BASE = 0.95
ACCESS_DECAY_CAP = 10   # 最多衰减 10 次
# 时间新鲜度：30 天内满分，90 天衰减到 0
RECENCY_FULL_DAYS = 30
RECENCY_ZERO_DAYS = 90


class LongTermMemory:
    """Persistent long-term memory with vector retrieval."""

    def __init__(self, db_path: str = "data/memory.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI 用线程池跑 cli.send()，不同请求
        # 可能落不同线程但共用同一个 connection。Python 层默认禁止跨线程用会
        # 抛 "SQLite objects created in a thread can only be used in that same
        # thread"。关掉守卫后，并发读 sqlite 内部会串行化；并发写需要自己加锁。
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 保护所有写操作（store/store_seed/store_batch/access_count 更新）
        # sqlite 内核本身会串行化写，但 Python 层要先序列化避免 race on _corpus
        # 和 _tfidf_matrix 重建。
        self._write_lock = threading.Lock()
        self._init_db()

        # TF-IDF vectorizer (fit on all stored content)
        # Use char-level + word-level analyzer for Chinese support
        self._vectorizer = TfidfVectorizer(
            max_features=5000,
            analyzer="char_wb",  # character n-grams within word boundaries
            ngram_range=(2, 4),  # bigrams to 4-grams capture Chinese char combinations
        )
        self._corpus: List[str] = []  # parallel to DB rows
        self._tfidf_matrix = None
        self._rebuild_index()

    def _init_db(self):
        """Create tables if not exist, with migrations for category/emotional_valence/embedding."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                importance TEXT DEFAULT 'normal',
                emotion_label TEXT,
                scene TEXT,
                time_phase TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT
            );
        """)
        # Migration: add category column if missing (backwards compatible)
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "category" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'both'"
            )
            self._conn.commit()
        # Migration: add emotional_valence (float) for emotion-aware recall
        if "emotional_valence" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN emotional_valence REAL"
            )
            self._conn.commit()
        # Migration: add embedding BLOB for RAG (BGE-small-zh, 512-dim float32)
        # 存 L2 归一化后的向量，检索时 cosine = 点积（np.dot），比 TF-IDF 快且语义强
        if "embedding" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN embedding BLOB"
            )
            self._conn.commit()
        # Phase3: add is_milestone flag for relationship milestones (相识/告白/在一起/纪念日)
        # 里程碑在 search 时加权召回，daemon 用它算周年主动话题
        if "is_milestone" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN is_milestone INTEGER DEFAULT 0"
            )
            self._conn.commit()
        # Phase5b: add fade column for memory decay (0.0 = vivid, 1.0 = forgotten)
        # fade increases over time based on importance: high 365d, medium 90d, low 30d
        if "fade" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN fade REAL DEFAULT 0.0"
            )
            self._conn.commit()
            logger.info("Added fade column to memories")
        # Phase5b: last_recalled_at 记录上次重回忆时间（重回忆时归零再推进到 new fade）
        # 用于统计持久化：里程碑周年仍依据 created_at
        if "last_recalled_at" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN last_recalled_at TEXT"
            )
            self._conn.commit()
            logger.info("Added last_recalled_at column to memories")

    def _calculate_fade(self, importance: str, days_elapsed: int,
                        category: Optional[str] = None) -> float:
        """计算记忆的当前 fade 值（0.0 = vivid, 1.0 = forgotten）。

        fade 根据 important度和时间间隔计算（指数衰减，越久越接近1）：
        - high: 半衰期 365 天（一年才模糊一半）
        - medium: 半衰期 90 天
        - low: 半衰期 30 天

        公式：fade = 1 - exp(-days / half_life)
        （重回忆后 last_recalled_at 重置，相当于记忆焕新）

        Phase7-5: 感官记忆衰减更慢（半衰期 ×2）——恋爱里感官锚点最持久。
        """
        half_life = {
            MemoryImportance.HIGH.value:   365,
            MemoryImportance.NORMAL.value: 90,
            MemoryImportance.LOW.value:     30,
        }.get(importance, 365)

        # Phase7-5: 感官记忆衰减更慢（half_life ×2）
        if category == "sensory":
            half_life *= 2

        fade = 1.0 - math.exp(-days_elapsed / half_life)
        return max(0.0, min(1.0, fade))

    def fade_memories(self):
        """根据 created_at / last_recalled_at 重算所有记忆的 fade 值。

        指数衰减：
        - high: ~365 天半衰期（每天约 +0.0027）
        - medium: ~90 天半衰期（每天约 +0.0110）
        - low: ~30 天半衰期（每天约 +0.0328）

        幂等——基于"距离上次焕新时间"重算，可重复调用不累积。
        被 cli.py 的 TIME_ADVANCE 订阅在跨天时调用。
        """
        now = datetime.now()
        rows = self._conn.execute(
            "SELECT id, importance, created_at, last_recalled_at FROM memories"
        ).fetchall()

        updated = 0
        for row in rows:
            mem_id = row["id"]
            importance = row["importance"]
            # 焕新基准时间：最近一次重回忆（last_recalled_at）优先，否则用 created_at
            ref_str = row["last_recalled_at"] or row["created_at"]
            if not ref_str:
                continue
            try:
                ref_time = datetime.fromisoformat(ref_str)
            except Exception:
                continue
            days_elapsed = (now - ref_time).days
            if days_elapsed <= 0:
                continue

            # Phase7-5: 传入 category 让 _calculate_fade 进一步调整半衰期
            category = row["category"]
            new_fade = self._calculate_fade(importance, days_elapsed, category)
            self._conn.execute(
                "UPDATE memories SET fade = ? WHERE id = ?",
                (new_fade, mem_id),
            )
            updated += 1

        if updated > 0:
            self._conn.commit()
            logger.info(f"[LongTermMemory] faded {updated} memories")

    def _prepare_search_result(self, row, score):
        """准备搜索匹配结果，加 fade 加权"""
        # 根据 fade 值调整得分：fade 越高得分越低
        # fade >= 0.8 的记忆基本不召回（几乎忘记）——已在 search 主循环过滤
        fade = row["fade"] if row["fade"] is not None else 0.0
        importance_weight = IMPORTANCE_WEIGHT.get(
            row["importance"], IMPORTANCE_WEIGHT[MemoryImportance.NORMAL.value]
        )

        # fade penalty：fade 每增加 0.1，分数减少 importance_weight * 0.1
        fade_penalty = fade * 0.1 * importance_weight
        adjusted_score = score - fade_penalty

        entry = MemoryEntry(
            id=row["id"],
            content=row["content"],
            importance=MemoryImportance(row["importance"]),
            emotion_label=row["emotion_label"],
            emotional_valence=row["emotional_valence"],
            scene=row["scene"],
            time_phase=row["time_phase"],
            category=MemoryCategory(row["category"]) if row["category"] else MemoryCategory.BOTH,
            timestamp=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
        )
        return entry, adjusted_score, row["id"]

    def _rebuild_index(self):
        """重建向量索引：从 DB 拉所有有 embedding 的记忆，缓存 (id, content, vec) 三元组。

        无 embedding 的旧记忆会在 search 时懒回填（_backfill_embedding）。
        TF-IDF 的 _corpus/_tfidf_matrix 仍保留作 fallback——embedding 模型没加载
        或编码失败时降级到 TF-IDF，不让检索整个挂掉。
        """
        rows = self._conn.execute(
            "SELECT id, content, embedding FROM memories ORDER BY id"
        ).fetchall()
        self._corpus = [r["content"] for r in rows]  # 保留给 TF-IDF fallback
        self._ids = [r["id"] for r in rows]
        # 向量缓存：平行于 _ids/_corpus 的 List[np.ndarray]
        self._vecs: List[Optional[np.ndarray]] = []
        for r in rows:
            if r["embedding"] is not None:
                self._vecs.append(EmbeddingEncoder.bytes_to_vec(r["embedding"]))
            else:
                self._vecs.append(None)  # 待回填
        # TF-IDF fallback（embedding 不可用时用）
        if self._corpus:
            try:
                self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
            except Exception:
                self._tfidf_matrix = None
        else:
            self._tfidf_matrix = None
        # 是否有任何向量可用
        self._has_vecs = any(v is not None for v in self._vecs)

    def _backfill_embedding(self, idx: int):
        """给某条无 embedding 的记忆懒编码回填。

        search 时碰到 vec=None 的记忆，若它在候选集里，就当场编码一次写回 DB + 缓存。
        这样存量记忆会被逐步补全（不用一次性全量回填，避免启动卡顿）。
        """
        content = self._corpus[idx]
        try:
            vec = get_encoder().encode(content, normalize=True)
            blob = vec.tobytes()
            with self._write_lock:
                self._conn.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (blob, self._ids[idx]),
                )
                self._conn.commit()
            self._vecs[idx] = vec
            self._has_vecs = True
            logger.debug(f"[LongTerm] backfilled embedding for id={self._ids[idx]}")
        except Exception as e:
            logger.warning(f"[LongTerm] backfill failed for id={self._ids[idx]}: {e}")

    def backfill_all(self, batch_log_every: int = 10) -> int:
        """全量回填：给所有无 embedding 的存量记忆编码向量。

        用于升级到 RAG 后一次性补全旧记忆。批量编码（一次 encode 列表）比逐条快很多。
        返回成功编码的条数。
        """
        # 收集待回填的下标
        todo = [i for i, v in enumerate(self._vecs) if v is None]
        if not todo:
            logger.info("[LongTerm] backfill_all: 无需回填，所有记忆已有 embedding")
            return 0
        logger.info(f"[LongTerm] backfill_all: 回填 {len(todo)} 条存量记忆...")

        try:
            enc = get_encoder()
            # 分批编码（避免一次性 encode 太多爆内存）
            BATCH = 64
            done = 0
            for start in range(0, len(todo), BATCH):
                batch_idx = todo[start:start + BATCH]
                contents = [self._corpus[i] for i in batch_idx]
                vecs = enc.encode(contents, normalize=True)  # (n, dim)
                blobs = [v.tobytes() for v in vecs]
                with self._write_lock:
                    for k_local, gi in enumerate(batch_idx):
                        self._conn.execute(
                            "UPDATE memories SET embedding = ? WHERE id = ?",
                            (blobs[k_local], self._ids[gi]),
                        )
                        self._vecs[gi] = vecs[k_local]
                    self._conn.commit()
                done += len(batch_idx)
                if done % batch_log_every < len(batch_idx):
                    logger.info(f"[LongTerm] backfill_all: {done}/{len(todo)}")
            self._has_vecs = True
            logger.info(f"[LongTerm] backfill_all 完成: {done} 条")
            return done
        except Exception as e:
            logger.error(f"[LongTerm] backfill_all 失败: {e}")
            return done if 'done' in dir() else 0

    def _encode_and_cache(self, entry: MemoryEntry, row_id: int) -> Optional[np.ndarray]:
        """编码 entry.content → 存 DB embedding 列 + 更新内存向量缓存。

        编码失败（embedding 模型没加载）时返回 None，记忆仍存得进库（content 列在），
        只是检索时该条走不了向量，降级到 TF-IDF。不让 embedding 故障阻塞写入。
        """
        try:
            vec = get_encoder().encode(entry.content, normalize=True)
            blob = vec.tobytes()
            with self._write_lock:
                self._conn.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (blob, row_id),
                )
                self._conn.commit()
            # 更新内存缓存（_corpus 已 append 过，对应最后一个）
            if self._vecs and len(self._vecs) >= len(self._corpus):
                self._vecs[-1] = vec
            self._has_vecs = True
            return vec
        except Exception as e:
            logger.warning(f"[LongTerm] encode embedding failed for id={row_id}: {e}")
            return None

    def store(self, entry: MemoryEntry) -> int:
        """Store a memory entry. Returns the row ID."""
        is_mile = 1 if entry.category == MemoryCategory.MILESTONE else 0
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT INTO memories (content, importance, emotion_label, scene, time_phase, category, emotional_valence, is_milestone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.content,
                    entry.importance.value,
                    entry.emotion_label,
                    entry.scene,
                    entry.time_phase,
                    entry.category.value,
                    entry.emotional_valence,
                    is_mile,
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
            # 更新 TF-IDF fallback 索引（embedding 不可用时降级用）
            self._corpus.append(entry.content)
            self._ids.append(row_id)
            self._vecs.append(None)
            try:
                if len(self._corpus) > 1:
                    self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
                else:
                    self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
            except Exception:
                pass
        # 锁外编码 embedding（编码慢，不占写锁）
        self._encode_and_cache(entry, row_id)
        return row_id

    def store_seed(self, entry: MemoryEntry, created_at: datetime) -> int:
        """Store a seed memory with a custom past timestamp (for #16 seed filling).

        Unlike store(), which uses SQLite default datetime('now'), this lets seeds
        carry a plausible "this happened in the past" timestamp so recency weighting
        in search() works correctly for seeded memories.
        """
        is_mile = 1 if entry.category == MemoryCategory.MILESTONE else 0
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT INTO memories
                   (content, importance, emotion_label, scene, time_phase, category, emotional_valence, created_at, is_milestone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.content,
                    entry.importance.value,
                    entry.emotion_label,
                    entry.scene,
                    entry.time_phase,
                    entry.category.value,
                    entry.emotional_valence,
                    created_at.isoformat(),
                    is_mile,
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
            self._corpus.append(entry.content)
            self._ids.append(row_id)
            self._vecs.append(None)
            try:
                if len(self._corpus) > 1:
                    self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
                else:
                    self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
            except Exception:
                pass
        self._encode_and_cache(entry, row_id)
        return row_id

    def store_batch(self, entries: List[MemoryEntry]) -> List[int]:
        """Store multiple entries. Returns list of row IDs."""
        ids = []
        for entry in entries:
            ids.append(self.store(entry))
        return ids

    def search(self, query: str, top_k: int = 5, min_score: float = 0.1,
               category: Optional[str] = None,
               current_mood_valence: Optional[float] = None) -> List[Tuple[MemoryEntry, float]]:
        """Search memories by semantic similarity.

        Args:
            query: search query text
            top_k: max results
            min_score: minimum cosine similarity threshold
            category: filter by category ('life'/'engineering'/None=all).
                      If 'both' is stored, it matches any query category.
            current_mood_valence: 当前心境效价 [-1,+1]（借 CLF mood 层传入）。
                用于 emotion-aware recall：
                - 心境积极：同质积极记忆微加（共鸣）
                - 心境消极：积极记忆微加（mood repair / 抚慰），消极记忆微减（避免反刍）
                - None/中性：不调节

        Returns list of (MemoryEntry, score) sorted by score desc.
        """
        if len(self._corpus) == 0:
            return []

        # === 计算语义相似度分数 ===
        # 优先 embedding（cosine = 归一化向量点积，语义强）；失败/加载中降级 TF-IDF
        # 模型加载要 ~100s，加载期间 /chat 不能等——available=False 时直接走 TF-IDF
        from .embedding import get_encoder
        enc = get_encoder()
        use_embedding = self._has_vecs and enc.available
        scores = None
        if use_embedding:
            try:
                q_vec = encode_query(query)  # (dim,) 归一化
                # 堆叠有效向量（无 embedding 的旧记忆跳过，下面懒回填）
                valid_idx = [i for i, v in enumerate(self._vecs) if v is not None]
                if not valid_idx:
                    use_embedding = False
                else:
                    mat = np.stack([self._vecs[i] for i in valid_idx])  # (n_valid, dim)
                    sims = mat @ q_vec  # (n_valid,) 点积 = cosine（都已归一化）
                    # 映射回全量下标空间，无效位置给 -inf（不进候选）
                    scores = np.full(len(self._corpus), -np.inf, dtype=np.float32)
                    for k_local, k_global in enumerate(valid_idx):
                        scores[k_global] = float(sims[k_local])
                    # 对无 embedding 的旧记忆懒回填一次（只回填 top_k*4 最近邻的 TF-IDF 候选，
                    # 避免一次回填全库）—— 这里先标记，下面循环碰到时按需编码
            except Exception as e:
                logger.warning(f"[LongTerm] embedding search failed, fallback TF-IDF: {e}")
                use_embedding = False

        if not use_embedding:
            # TF-IDF fallback
            if self._tfidf_matrix is None:
                return []
            query_vec = self._vectorizer.transform([query])
            scores = cosine_similarity(query_vec, self._tfidf_matrix).flatten()

        # 取候选集（多取一些，因为加权重排后排名会变）
        fetch_k = top_k * 4
        # 排除 -inf（无 embedding 且未回填的旧记忆，在 fallback 路径下 scores 全有效）
        candidate_indices = np.argsort(scores)[::-1][:fetch_k]

        now = datetime.now()
        scored = []
        for idx in candidate_indices:
            sem_score = float(scores[idx])
            if sem_score < min_score:
                continue
            content = self._corpus[int(idx)]
            row = self._conn.execute(
                "SELECT * FROM memories WHERE content = ? ORDER BY id LIMIT 1", (content,)
            ).fetchone()
            if not row:
                continue
            # Category filter: 'both' memories match any category
            if category and row["category"] not in (category, "both"):
                continue

            # --- 加权重排 (#17) + Phase5b fade 加权 ---
            imp = IMPORTANCE_WEIGHT.get(row["importance"], 0.6)
            # 时间新鲜度
            try:
                created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else now
                age_days = (now - created).days
            except Exception:
                age_days = 0
            if age_days <= RECENCY_FULL_DAYS:
                recency = 1.0
            elif age_days >= RECENCY_ZERO_DAYS:
                recency = 0.0
            else:
                recency = 1.0 - (age_days - RECENCY_FULL_DAYS) / (RECENCY_ZERO_DAYS - RECENCY_FULL_DAYS)
            # access 钝化
            access = int(row["access_count"] or 0)
            access_factor = ACCESS_DECAY_BASE ** min(access, ACCESS_DECAY_CAP)

            # --- emotion-aware recall (借 CLF mood 层) ---
            # 记忆自身效价（记录时的 VAD valence）
            mem_valence = row["emotional_valence"] if row["emotional_valence"] is not None else 0.0
            mood_factor = 1.0
            if current_mood_valence is not None:
                if current_mood_valence < -0.2:
                    # 心境消极：积极记忆微加（mood repair / 抚慰），消极记忆微减（避免反刍）
                    if mem_valence > 0.15:
                        mood_factor = 1.0 + W_MOOD * 0.5
                    elif mem_valence < -0.15:
                        mood_factor = 1.0 - W_MOOD * 0.5
                elif current_mood_valence > 0.2:
                    # 心境积极：同质积极记忆微加（共鸣），消极记忆略压
                    if mem_valence > 0.15:
                        mood_factor = 1.0 + W_MOOD * 0.3
                    elif mem_valence < -0.15:
                        mood_factor = 1.0 - W_MOOD * 0.2

            # Phase5b fade 加权（限制 fade >= 0.8 不召回）
            # sqlite3.Row 没有 .get()，用 in keys() 兜底（旧库没这列也不崩）
            try:
                fade = row["fade"]
                if fade is None:
                    fade = 0.0
            except (KeyError, IndexError):
                fade = 0.0
            if fade >= 0.8:
                continue

            # 总加权分（语义+重要度+时间新颖度）
            base_score = (
                W_SEMANTIC * sem_score
                + W_IMPORTANCE * imp
                + W_RECENCY * recency
            ) * access_factor * mood_factor

            # Phase3: 里程碑加权——关系里程碑（is_milestone=1）召回时 boost，
            # 让"在一起那天""相识那天"等共同记忆更容易被 recall 到
            try:
                if row["is_milestone"]:
                    base_score *= 1.4
            except (KeyError, IndexError):
                pass

            # Phase7-5: 感官记忆召回加权——恋爱里感官锚点最难忘，×1.3
            try:
                if row["category"] == MemoryCategory.SENSORY.value:
                    base_score *= 1.3
            except (KeyError, IndexError):
                pass

            # 克制版黏糊：情感关键词召回加权——让她每次回复都更容易想到这些
            # 关键词命中越多加权越高（最多 ×1.3），不覆盖非情感记忆只是微提
            emotional_keywords = ("想你", "想你了", "想你吗", "喜欢", "想见",
                                  "在一起", "偷偷", "心跳", "偷偷喜欢")
            emo_hits = sum(1 for kw in emotional_keywords if kw in content)
            if emo_hits > 0:
                # 1个关键词 ×1.1，2+ 个 ×1.3（封顶，避免情感记忆过度霸榜）
                base_score *= (1.1 if emo_hits == 1 else 1.3)

            # 最终用 _prepare_search_result 统一调整分数（含 fade penalty）
            entry, adjusted_score, row_id = self._prepare_search_result(
                row, base_score
            )
            scored.append((entry, adjusted_score, row_id))

        # 按加权分排序，取 top_k
        scored.sort(key=lambda x: x[1], reverse=True)
        results = []
        for entry, final_score, row_id in scored[:top_k]:
            # Update access count
            self._conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
                (row_id,),
            )
            # Phase5b: 重新回忆时 fade 归零 + 记录 last_recalled_at
            # （记忆被重新激活后重新焕新，从现在起按 importance 重新衰减）
            self._conn.execute(
                "UPDATE memories SET fade = 0.0, last_recalled_at = datetime('now') WHERE id = ?",
                (row_id,),
            )
            results.append((entry, final_score))
        if results:
            self._conn.commit()
        return results

    def get_upcoming_milestone(self, within_days: int = 7) -> List[dict]:
        """查未来 N 天内即将到的里程碑（周年纪念日）。

        Phase3 人格化：daemon topic_sources.get_milestone_topics() 调这个方法，
        找到即将到来的纪念日（"在一起 100 天""相识 1 周年"），作为主动话题素材。
        """
        now = datetime.now()
        rows = self._conn.execute(
            "SELECT content, created_at FROM memories WHERE is_milestone = 1"
        ).fetchall()

        upcoming = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
                if not created:
                    continue
                # 算今年/明年的周年日（避开已过的今年）
                try:
                    anniversary_this_year = created.replace(year=now.year)
                except ValueError:  # 2/29 之类
                    anniversary_this_year = created.replace(year=now.year, day=28)
                if anniversary_this_year.date() < now.date():
                    try:
                        anniversary = created.replace(year=now.year + 1)
                    except ValueError:
                        anniversary = created.replace(year=now.year + 1, day=28)
                else:
                    anniversary = anniversary_this_year
                delta = (anniversary - now).days
                if 0 <= delta <= within_days:
                    years = now.year - created.year + (1 if anniversary > anniversary_this_year else 0)
                    upcoming.append({
                        "content": row["content"],
                        "anniversary_date": anniversary.isoformat(),
                        "days_until": delta,
                        "years": years,  # 第 N 年
                        "created_at": row["created_at"],
                    })
            except Exception:
                continue
        upcoming.sort(key=lambda x: x["days_until"])
        return upcoming

    def get_all(self, limit: int = 100) -> List[MemoryEntry]:
        """Get all memories (most recent first)."""
        rows = self._conn.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        entries = []
        for row in rows:
            entries.append(MemoryEntry(
                id=row["id"],
                content=row["content"],
                importance=MemoryImportance(row["importance"]),
                emotion_label=row["emotion_label"],
                scene=row["scene"],
                time_phase=row["time_phase"],
            ))
        return entries

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        return row["cnt"]

    def close(self):
        self._conn.close()

    def get_cognitive_slices(self, limit: int = 4) -> List[str]:
        """Phase7-1: 抽取「她对学长的观察切片」——从真实回忆里动态生长的认知锚点。

        不是抽象数字（亲密=0.7），而是带内容的具体场景认知单元：
        「他写代码戴耳机但没放音乐」「他攻克bug会小声说搞定嘴角动一下」。
        这些来自真实存档记忆，随聊天积累越来越丰富，避免硬编码。

        判定"观察切片"的核心特征：以「他」为主语描述他的习惯/行为/特质，
        不是事件叙述（"他跟我告白了"→事件，不取）也不是她自述（"我愣了"→不取）。
        优先 sensory（感官锚点最立体）+ high importance。

        Phase7-1 修复：早期不用 created_at DESC LIMIT 预筛——种子观察切片
        （"他写代码戴耳机""他攻克bug说搞定"）created_at 集中在早期，会被
        新对话挤到候选集之外。含"他"的记忆总量小（~70 条），全拉后在
        Python 层过滤+评分，无性能问题。
        """
        # Phase7-1: 观察切片抽取的核心策略
        # 提取以"他"开头、简洁描述他的习惯/行为的短文本（≤80字符）。
        # 排除事件叙述（他跟我/他给我/他跟我说了/他告白），保留观察性描述。
        EVENT_PREFIXES = (
            "他跟", "他给", "他告诉", "他跟我说", "他向外",
            "他对我老婆", "他舅舅", "他跟我", "他忙起来不", "他一直以为",
            "他喜欢玩", "他曾", "他这次", "他那天",
        )

        def is_observe_slice(content: str) -> bool:
            s = content.strip()
            if not s.startswith("他"):
                return False
            # 排除事件叙述 / 长段自述
            if s.startswith(EVENT_PREFIXES):
                return False
            # 长度：观察切片是凝练的一句话，事件叙述/心理独白通常更长
            if len(s) > 80:
                return False
            if len(s) < 10:
                return False
            # 必须含「他的具体行为/特质」线索——避免"他…我…"其实是自述
            # （如"他问我xxx，我顿了一下"——主语虽是他，落点在她自己）
            # 启发式：含"我"且"我"出现在"他"行为描述之后 → 偏自述，降级但保留
            return True

        try:
            # 含"他"的记忆总量小，全拉后在 Python 层过滤+评分
            rows = self._conn.execute(
                "SELECT content, category, importance, created_at, access_count "
                "FROM memories WHERE content LIKE ?",
                (f"%他%",),
            ).fetchall()
        except Exception as e:
            logger.warning(f"[LongTerm] get_cognitive_slices query failed: {e}")
            return []

        slices = []
        seen = set()  # 去重（同一条观察可能被多次存档）
        for row in rows:
            content = (row["content"] or "").strip()
            if not is_observe_slice(content):
                continue
            if content in seen:
                continue
            seen.add(content)

            # 优先级：sensory > life > other，high importance 加权
            cat = row["category"] or "life"
            cat_w = 2.0 if cat == MemoryCategory.SENSORY.value else (
                1.0 if cat == MemoryCategory.LIFE.value else 0.7
            )
            imp = row["importance"] or "normal"
            imp_w = 1.2 if imp == MemoryImportance.HIGH.value else (
                1.0 if imp == MemoryImportance.NORMAL.value else 0.8
            )
            try:
                ts = datetime.fromisoformat(row["created_at"]).timestamp()
            except Exception:
                ts = 0.0

            slices.append({
                "content": content,
                "score": cat_w * imp_w,
                "ts": ts,
                "access": row["access_count"] or 0,
            })

        # 排序：score 优先 → 时间近优先 → 访问多优先
        slices.sort(key=lambda x: (-x["score"], -x["ts"], -x["access"]))
        return [s["content"] for s in slices[:limit]]
