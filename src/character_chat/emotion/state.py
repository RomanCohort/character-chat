"""Emotion state — lifted from IGEM-sama emotion/tracker.py.

Dropped threading lock (single-threaded CLI) and atomic file persistence
(persistence handled by tracker caller). Kept the 9-label emotion model,
EMA blending, and time decay.
"""
import time
from enum import Enum
from typing import Dict, Optional


class EmotionLabel(str, Enum):
    HAPPY = "happy"
    EXCITED = "excited"
    CALM = "calm"
    CURIOUS = "curious"
    SAD = "sad"
    ANGRY = "angry"
    SHY = "shy"
    PROUD = "proud"
    NEUTRAL = "neutral"


# Default intensities: all zero except NEUTRAL=1.0
_DEFAULT_INTENSITIES: Dict[str, float] = {e.value: 0.0 for e in EmotionLabel}
_DEFAULT_INTENSITIES[EmotionLabel.NEUTRAL.value] = 1.0

# Map our 5 neural labels (joy/sadness/anger/fear/neutral) → 9-label space
NEURAL_TO_RULE_MAP: Dict[str, EmotionLabel] = {
    "joy": EmotionLabel.HAPPY,
    "sadness": EmotionLabel.SAD,
    "anger": EmotionLabel.ANGRY,
    "fear": EmotionLabel.SHY,   # fear → shy (character-appropriate)
    "neutral": EmotionLabel.NEUTRAL,
}


class EmotionState:
    """Snapshot of all emotion intensities [0,1]."""

    def __init__(self, intensities: Optional[Dict[str, float]] = None):
        self.intensities: Dict[str, float] = dict(
            intensities if intensities else _DEFAULT_INTENSITIES
        )
        self.last_update: float = time.time()

    @property
    def dominant(self) -> EmotionLabel:
        """Return the emotion with the highest intensity."""
        best = EmotionLabel.NEUTRAL
        best_val = -1.0
        for label, val in self.intensities.items():
            if val > best_val:
                best_val = val
                best = EmotionLabel(label)
        return best

    @property
    def dominant_intensity(self) -> float:
        return self.intensities.get(self.dominant.value, 0.0)

    def top_n(self, n: int = 3) -> list[tuple[str, float]]:
        """Return top-n (label, intensity) pairs sorted desc."""
        items = sorted(
            self.intensities.items(), key=lambda x: x[1], reverse=True
        )
        return items[:n]

    def to_dict(self) -> dict:
        return {"intensities": self.intensities, "last_update": self.last_update}

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        state = cls(intensities=data.get("intensities"))
        state.last_update = data.get("last_update", time.time())
        return state
