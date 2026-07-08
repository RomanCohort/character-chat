"""Hippocampus consolidation layer.

Consolidation = moving important short-term memories into long-term storage.
Triggered by:
  - `/sleep` command (user-initiated)
  - Idle timeout (auto, Phase 4)
  - Buffer overflow (when short-term exceeds capacity)

Consolidation strategy (inspired by CLF hippocampus, simplified):
  1. Select consolidation candidates from short-term buffer
  2. Boost importance for emotionally charged entries
  3. Merge similar entries (dedup by content similarity)
  4. Store into long-term memory
  5. Remove consolidated entries from short-term buffer
"""
from typing import List, Optional
from loguru import logger

from .schema import MemoryEntry, MemoryType, MemoryImportance
from .short_term import ShortTermMemory
from .long_term import LongTermMemory


class HippocampusConsolidator:
    """Consolidates short-term memories into long-term storage."""

    def __init__(
        self,
        short_term: ShortTermMemory,
        long_term: LongTermMemory,
        similarity_threshold: float = 0.85,
    ):
        self._short_term = short_term
        self._long_term = long_term
        self._similarity_threshold = similarity_threshold

    def consolidate(self, force_all: bool = False) -> int:
        """Run consolidation. Returns number of memories consolidated.

        Args:
            force_all: If True, consolidate all short-term memories
                      (used for /sleep command).
        """
        if force_all:
            candidates = self._short_term.get_all()
        else:
            candidates = self._short_term.get_consolidation_candidates()

        if not candidates:
            logger.debug("[Hippocampus] No consolidation candidates")
            return 0

        # Boost importance for emotional memories
        for entry in candidates:
            if entry.emotion_label and entry.emotion_label not in ("neutral", None):
                if entry.importance == MemoryImportance.LOW:
                    entry.importance = MemoryImportance.NORMAL
                elif entry.importance == MemoryImportance.NORMAL:
                    entry.importance = MemoryImportance.HIGH

        # Dedup: check if similar memories already exist in long-term
        new_entries = []
        for entry in candidates:
            # Check similarity against existing long-term memories
            existing = self._long_term.search(entry.content, top_k=3, min_score=0.3)
            is_duplicate = any(score > self._similarity_threshold for _, score in existing)
            if not is_duplicate:
                new_entries.append(entry)
            else:
                logger.debug(f"[Hippocampus] Skipping duplicate: {entry.content[:40]}...")

        # Store new entries
        if new_entries:
            ids = self._long_term.store_batch(new_entries)
            logger.info(f"[Hippocampus] Consolidated {len(new_entries)} memories (IDs: {ids})")

        # Remove consolidated entries from short-term buffer
        self._short_term.remove(candidates)

        return len(new_entries)

    def get_consolidation_stats(self) -> dict:
        """Get stats about consolidation state."""
        return {
            "short_term_size": self._short_term.size,
            "long_term_size": self._long_term.count(),
            "consolidation_candidates": len(self._short_term.get_consolidation_candidates()),
        }
