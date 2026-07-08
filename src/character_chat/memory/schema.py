"""Memory schema for short-term and long-term memory.

Memory types:
- short_term: Recent conversation turns (in-memory buffer, bounded size)
- long_term: Consolidated memories (sqlite-vec stored, vector searchable)
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from enum import Enum


class MemoryType(str, Enum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


class MemoryImportance(str, Enum):
    LOW = "low"       # casual chat
    NORMAL = "normal" # significant event
    HIGH = "high"     # emotionally charged / important


@dataclass
class MemoryEntry:
    """Single memory entry."""
    content: str              # raw text (user input + assistant reply pair)
    id: Optional[int] = None  # auto-generated for long_term
    memory_type: MemoryType = MemoryType.SHORT_TERM
    importance: MemoryImportance = MemoryImportance.NORMAL
    emotion_label: Optional[str] = None  # dominant emotion at that time
    scene: Optional[str] = None          # scene name
    time_phase: Optional[str] = None     # narrative phase (晨/午/黄昏/夜/深夜)
    timestamp: datetime = field(default_factory=datetime.now)
    embedding: Optional[List[float]] = None  # for long_term (TF-IDF vector)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "importance": self.importance.value,
            "emotion_label": self.emotion_label,
            "scene": self.scene,
            "time_phase": self.time_phase,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(
            id=data.get("id"),
            content=data["content"],
            memory_type=MemoryType(data.get("memory_type", "short_term")),
            importance=MemoryImportance(data.get("importance", "normal")),
            emotion_label=data.get("emotion_label"),
            scene=data.get("scene"),
            time_phase=data.get("time_phase"),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(),
            embedding=data.get("embedding"),
        )