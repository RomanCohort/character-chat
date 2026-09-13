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


class MemoryCategory(str, Enum):
    """Memory category for shared memory (life vs engineering).

    LIFE:         personal, emotional, preferences, daily chat
    ENGINEERING:  code, bugs, architecture, project work, technical tasks
    BOTH:         ambiguous — will be classified at record time
    MILESTONE:    relationship milestones (相识/告白/在一起/纪念日/共同目标) —
                  Phase3 人格化：里程碑优先召回 + 周年触发主动话题
    SENSORY:      感官记忆（他手凉/他洗衣液的味道/他工位下午逆光/他靠近时的心跳）—
                  Phase7-5: 感官锚点最持久，召回时加权 + fade 衰减更慢
                  （恋爱里感官记忆比事件记忆更难忘）
    """
    LIFE = "life"
    ENGINEERING = "engineering"
    BOTH = "both"
    MILESTONE = "milestone"
    SENSORY = "sensory"


@dataclass
class MemoryEntry:
    """Single memory entry."""
    content: str              # raw text (user input + assistant reply pair)
    id: Optional[int] = None  # auto-generated for long_term
    memory_type: MemoryType = MemoryType.SHORT_TERM
    importance: MemoryImportance = MemoryImportance.NORMAL
    emotion_label: Optional[str] = None  # dominant emotion at that time
    emotional_valence: Optional[float] = None  # VAD valence [-1,+1] at record time (借 CLF)
    scene: Optional[str] = None          # scene name
    time_phase: Optional[str] = None     # narrative phase (晨/午/黄昏/夜/深夜)
    category: MemoryCategory = MemoryCategory.BOTH  # life vs engineering
    timestamp: datetime = field(default_factory=datetime.now)
    embedding: Optional[List[float]] = None  # for long_term (TF-IDF vector)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "importance": self.importance.value,
            "emotion_label": self.emotion_label,
            "emotional_valence": self.emotional_valence,
            "scene": self.scene,
            "time_phase": self.time_phase,
            "category": self.category.value,
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
            emotional_valence=data.get("emotional_valence"),
            scene=data.get("scene"),
            time_phase=data.get("time_phase"),
            category=MemoryCategory(data.get("category", "both")),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(),
            embedding=data.get("embedding"),
        )