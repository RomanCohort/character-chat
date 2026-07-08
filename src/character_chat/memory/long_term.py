"""Long-term memory store (sqlite + TF-IDF vector retrieval).

Uses sqlite for persistent storage and sklearn TF-IDF + cosine similarity
for semantic retrieval. No external embedding model needed.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .schema import MemoryEntry, MemoryType, MemoryImportance


class LongTermMemory:
    """Persistent long-term memory with vector retrieval."""

    def __init__(self, db_path: str = "data/memory.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
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
        """Create tables if not exist."""
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
        self._conn.commit()

    def _rebuild_index(self):
        """Rebuild TF-IDF index from all stored memories."""
        rows = self._conn.execute(
            "SELECT id, content FROM memories ORDER BY id"
        ).fetchall()
        self._corpus = [r["content"] for r in rows]
        if self._corpus:
            self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
        else:
            self._tfidf_matrix = None

    def store(self, entry: MemoryEntry) -> int:
        """Store a memory entry. Returns the row ID."""
        cur = self._conn.execute(
            """INSERT INTO memories (content, importance, emotion_label, scene, time_phase)
               VALUES (?, ?, ?, ?, ?)""",
            (
                entry.content,
                entry.importance.value,
                entry.emotion_label,
                entry.scene,
                entry.time_phase,
            ),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        # Update TF-IDF index incrementally
        self._corpus.append(entry.content)
        if len(self._corpus) > 1:
            self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
        else:
            self._tfidf_matrix = self._vectorizer.fit_transform(self._corpus)
        return row_id

    def store_batch(self, entries: List[MemoryEntry]) -> List[int]:
        """Store multiple entries. Returns list of row IDs."""
        ids = []
        for entry in entries:
            ids.append(self.store(entry))
        return ids

    def search(self, query: str, top_k: int = 5, min_score: float = 0.1) -> List[Tuple[MemoryEntry, float]]:
        """Search memories by semantic similarity.

        Returns list of (MemoryEntry, score) sorted by score desc.
        """
        if self._tfidf_matrix is None or len(self._corpus) == 0:
            return []

        # Transform query
        query_vec = self._vectorizer.transform([query])
        scores = cosine_similarity(query_vec, self._tfidf_matrix).flatten()

        # Get top-k indices
        top_indices = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score < min_score:
                continue
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(idx) + 1,)
            ).fetchone()
            if row:
                entry = MemoryEntry(
                    id=row["id"],
                    content=row["content"],
                    importance=MemoryImportance(row["importance"]),
                    emotion_label=row["emotion_label"],
                    scene=row["scene"],
                    time_phase=row["time_phase"],
                    timestamp=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
                )
                # Update access count
                self._conn.execute(
                    "UPDATE memories SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                self._conn.commit()
                results.append((entry, score))
        return results

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
