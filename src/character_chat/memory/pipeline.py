"""Memory pipeline - orchestrates short-term, long-term, hippocampus, and retrieval.

Single entry points:
  - MemoryPipeline.record(user_text, reply, emotion, scene, phase) -> record a turn
  - MemoryPipeline.recall(query, top_k) -> retrieve relevant memories
  - MemoryPipeline.consolidate(force_all) -> run hippocampus consolidation
"""
from typing import List, Optional, Tuple
from loguru import logger

from .schema import MemoryEntry, MemoryImportance
from .short_term import ShortTermMemory
from .long_term import LongTermMemory
from .hippocampus import HippocampusConsolidator


class MemoryPipeline:
    """End-to-end memory pipeline for character chat."""

    def __init__(
        self,
        db_path: str = "data/memory.db",
        max_short_term: int = 20,
    ):
        self.short_term = ShortTermMemory(max_entries=max_short_term)
        self.long_term = LongTermMemory(db_path=db_path)
        self.hippocampus = HippocampusConsolidator(
            short_term=self.short_term,
            long_term=self.long_term,
        )

    def record(
        self,
        user_text: str,
        reply_text: str,
        emotion_label: Optional[str] = None,
        scene: Optional[str] = None,
        time_phase: Optional[str] = None,
        importance: MemoryImportance = MemoryImportance.NORMAL,
    ) -> MemoryEntry:
        """Record a conversation turn into short-term memory.

        Args:
            user_text: User's message.
            reply_text: Character's reply.
            emotion_label: Dominant emotion at this turn.
            scene: Current scene name.
            time_phase: Narrative time phase.
            importance: Memory importance level.

        Returns:
            The created MemoryEntry.
        """
        # Combine user + reply for context (the memory should capture the interaction)
        content = f"用户: {user_text}\n角色: {reply_text}"

        # Auto-detect importance from emotional intensity
        if emotion_label and emotion_label not in ("neutral", None):
            importance = MemoryImportance.HIGH

        entry = self.short_term.add(
            content=content,
            emotion_label=emotion_label,
            scene=scene,
            time_phase=time_phase,
            importance=importance,
        )
        logger.debug(f"[Memory] Recorded: {content[:50]}... (importance={importance.value})")
        return entry

    def recall(self, query: str, top_k: int = 3) -> List[str]:
        """Retrieve relevant memories for prompt injection.

        Returns formatted memory strings suitable for prompt injection.
        """
        # Search long-term first
        results = self.long_term.search(query, top_k=top_k, min_score=0.15)

        # Format for prompt
        memories = []
        for entry, score in results:
            memories.append(
                f"[{entry.time_phase or '?'}|{entry.scene or '?'}|{entry.emotion_label or 'neutral'}] {entry.content}"
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

    def get_stats(self) -> dict:
        """Get memory system stats."""
        stats = self.hippocampus.get_consolidation_stats()
        return stats

    def close(self):
        """Close database connection."""
        self.long_term.close()
