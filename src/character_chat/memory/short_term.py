"""Short-term memory buffer (in-memory, bounded size).

Holds recent conversation turns. When capacity is exceeded, oldest entries
are candidates for consolidation into long-term memory (via hippocampus).
"""
from typing import List, Optional
from .schema import MemoryEntry, MemoryType, MemoryImportance


class ShortTermMemory:
    """Fixed-size in-memory buffer of recent conversation turns."""

    def __init__(self, max_entries: int = 20):
        self._buffer: List[MemoryEntry] = []
        self._max_entries = max_entries

    def add(
        self,
        content: str,
        emotion_label: Optional[str] = None,
        scene: Optional[str] = None,
        time_phase: Optional[str] = None,
        importance: MemoryImportance = MemoryImportance.NORMAL,
        emotional_valence: Optional[float] = None,
    ) -> MemoryEntry:
        """Add a new memory entry. Returns the created entry."""
        entry = MemoryEntry(
            content=content,
            memory_type=MemoryType.SHORT_TERM,
            importance=importance,
            emotion_label=emotion_label,
            emotional_valence=emotional_valence,
            scene=scene,
            time_phase=time_phase,
        )
        self._buffer.append(entry)
        # Evict oldest if over capacity
        while len(self._buffer) > self._max_entries:
            self._buffer.pop(0)
        return entry

    def get_recent(self, n: int = 5) -> List[MemoryEntry]:
        """Get the n most recent entries."""
        return self._buffer[-n:]

    def get_all(self) -> List[MemoryEntry]:
        """Get all entries."""
        return list(self._buffer)

    def get_consolidation_candidates(self) -> List[MemoryEntry]:
        """Get entries that should be consolidated to long-term memory.

        Strategy: return entries that are important or emotionally charged.
        Normal-importance entries are only consolidated if they exceed
        a recency threshold (i.e. they're no longer in the "active" window).
        """
        candidates = []
        active_window = min(5, len(self._buffer))
        for i, entry in enumerate(self._buffer):
            # Always consolidate important/emotional memories
            if entry.importance in (MemoryImportance.HIGH,):
                candidates.append(entry)
            # Older entries (outside active window) are consolidation candidates
            elif i < len(self._buffer) - active_window:
                candidates.append(entry)
        return candidates

    def remove(self, entries: List[MemoryEntry]):
        """Remove specific entries after consolidation."""
        ids_to_remove = {id(e) for e in entries}
        self._buffer = [e for e in self._buffer if id(e) not in ids_to_remove]

    def clear(self):
        """Clear all entries."""
        self._buffer.clear()

    @property
    def size(self) -> int:
        return len(self._buffer)

    def to_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self._buffer]
