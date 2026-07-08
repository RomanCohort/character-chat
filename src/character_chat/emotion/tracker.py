"""Emotion tracker (Layer 1) - rule-based keyword detection + sentiment.

Uses keyword scanning + simple sentiment estimation (no external analyzer dependency).
EMA blending (blend_factor=0.4) + time decay (0.02/sec).
"""
import time
from typing import Dict, Optional

from .state import EmotionLabel, EmotionState


class EmotionTracker:
    """Rule-based emotion tracker (Layer 1).

    Processes user messages for keyword-based emotion detection and
    simple sentiment scoring for rule-based updates.
    """

    # EMA blending factor (0 = no change, 1 = instant)
    _BLEND_FACTOR = 0.4
    # Decay per second toward NEUTRAL
    _DECAY_RATE = 0.02
    # Minimum intensity to count as "active"
    _THRESHOLD = 0.05

    def __init__(self, decay_interval: float = 1.0):
        self._decay_interval = decay_interval
        self.state = EmotionState()
        self._last_decay_time = time.time()

    def update_from_keyword(self, text: str, force: bool = True) -> Optional[EmotionLabel]:
        """Detect emotion from keywords in text. Returns detected emotion or None.

        Args:
            text: User message text.
            force: If True (default), strong keyword matches directly set the
                   emotion above neutral so a single clear input dominates.
                   Set False to keep slow EMA accumulation (IGEM-sama style).
        """
        text_lower = text.lower()
        for emotion, keywords in _KEYWORD_EMOTIONS.items():
            for kw in keywords:
                if kw in text_lower:
                    if force:
                        self._force_set(emotion, 0.7)
                    else:
                        self._blend(emotion, 0.7)
                    self.state.last_update = time.time()
                    return emotion
        return None

    def _force_set(self, target: EmotionLabel, intensity: float):
        """Force target emotion above neutral (single-turn dominance).

        When a keyword clearly matches, we want the detected emotion to
        immediately dominate over neutral. Uses a strong blend factor.
        """
        old = self.state.intensities.get(target.value, 0.0)
        # Strong blend: target gets most of the weight
        a = 0.6
        boosted = old * (1 - a) + intensity * a
        # Ensure target exceeds a dominance threshold
        boosted = max(boosted, 0.55)
        self.state.intensities[target.value] = min(boosted, 1.0)
        # Reduce neutral sharply
        neutral_key = EmotionLabel.NEUTRAL.value
        total_non_neutral = sum(v for k, v in self.state.intensities.items() if k != neutral_key)
        self.state.intensities[neutral_key] = max(0.0, 1.0 - min(total_non_neutral, 1.0))

    def update_from_sentiment(
        self, sentiment_score: Optional[float] = None
    ) -> Optional[EmotionLabel]:
        """Update emotion from a sentiment score (-1 to 1).
        Returns dominant emotion. If sentiment_score is None, uses rule-based estimation."""

        if sentiment_score is None:
            # Use simple rule-based estimation
            sentiment_score = self._estimate_sentiment(self._get_narrative_context())
            if sentiment_score is None:
                return None

        # Map sentiment to EmotionLabel
        target = EmotionLabel.NEUTRAL
        for (lo, hi), label in _SENTIMENT_EMOTION_MAP.items():
            if lo <= sentiment_score < hi:
                target = label
                break

        # Clamp extreme values
        if sentiment_score >= 0.8:
            target = EmotionLabel.EXCITED
        elif sentiment_score <= -0.8:
            target = EmotionLabel.ANGRY

        self._blend(target, abs(sentiment_score))
        self.state.last_update = time.time()
        return self.state.dominant

    def _get_narrative_context(self) -> list[str]:
        """Get last 3 user messages as narrative context."""
        return []  # TODO: integrate with session history

    def _estimate_sentiment(self, texts: list[str]) -> Optional[float]:
        """Simple rule-based sentiment average (-1..1)."""
        pos_count = 0
        neg_count = 0
        for txt in texts:
            txt_lower = txt.lower()
            # Positive indicators
            if any(kw in txt_lower for kw in ["开心", "好", "棒", "厉害", "喜欢", "太好了"]):
                pos_count += 1
            # Negative indicators
            if any(kw in txt_lower for kw in ["难过", "烦", "讨厌", "生气", "失望"]):
                neg_count += 1
        total = pos_count + neg_count
        if total == 0:
            return None
        # Map to [-1, 1] non-linearly (strengthen positive perception)
        return (pos_count / total * 1.5 - 0.5)  # pos 0→ -0.5, pos 1→ 1.0

    def apply_decay(self):
        """Apply time-based decay toward NEUTRAL."""
        now = time.time()
        dt = now - self._last_decay_time
        if dt < self._decay_interval:
            return
        self._last_decay_time = now

        neutral_key = EmotionLabel.NEUTRAL.value
        for key in self.state.intensities:
            if key == neutral_key:
                continue
            self.state.intensities[key] -= self._DECAY_RATE * dt
            if self.state.intensities[key] < self._THRESHOLD:
                self.state.intensities[key] = 0.0

        # Neutral rises as others decay
        total = sum(v for k, v in self.state.intensities.items() if k != neutral_key)
        self.state.intensities[neutral_key] = max(0.0, 1.0 - total)

        self.state.last_update = now

    def get_prompt_hint(self) -> str:
        """Return behavior hint string for prompt injection."""
        dominant = self.state.dominant
        return _EMOTION_HINTS.get(dominant, "")

    def _blend(self, target: EmotionLabel, intensity: float):
        """Smoothly blend target emotion using EMA."""
        a = self._BLEND_FACTOR
        old = self.state.intensities.get(target.value, 0.0)
        self.state.intensities[target.value] = old * (1 - a) + intensity * a
        # Reduce others proportionally
        neutral_key = EmotionLabel.NEUTRAL.value
        total_non_neutral = sum(v for k, v in self.state.intensities.items() if k != neutral_key)
        self.state.intensities[neutral_key] = max(0.0, 1.0 - min(total_non_neutral, 1.0))


# Sentiment to emotion mapping (raw scores -1..1)
_SENTIMENT_EMOTION_MAP = {
    (-1.0, -0.5): EmotionLabel.SAD,
    (-0.5, -0.2): EmotionLabel.ANGRY,
    (-0.2, 0.2): EmotionLabel.NEUTRAL,
    (0.2, 0.5): EmotionLabel.CURIOUS,
    (0.5, 0.8): EmotionLabel.HAPPY,
    (0.8, 1.0): EmotionLabel.EXCITED,
}

# Keyword-based emotion detection (Chinese + English)
_KEYWORD_EMOTIONS = {
    EmotionLabel.HAPPY: ["开心", "太好了", "厉害", "棒", "哈哈", "笑", "喜欢", "可爱", "好耶", "666", "good", "awesome", "wow", "great"],
    EmotionLabel.EXCITED: ["激动", "兴奋", "超", "太牛", "震撼"],
    EmotionLabel.CURIOUS: ["为什么", "怎么回事", "好奇", "什么意思", "怎么做到"],
    EmotionLabel.SAD: ["难过", "可惜", "伤心", "遗憾", "哭", "失望"],
    EmotionLabel.ANGRY: ["生气", "烦", "讨厌", "气死"],
    EmotionLabel.SHY: ["害羞", "脸红", "不好意思", "夸我"],
    EmotionLabel.CALM: ["平静", "没事", "还好"],
}

# Behavior hints per emotion (Chinese, for prompt injection)
_EMOTION_HINTS = {
    EmotionLabel.HAPPY: "你现在很开心，语气轻快。",
    EmotionLabel.EXCITED: "你现在非常激动，说话很兴奋！",
    EmotionLabel.CALM: "你现在很平静。",
    EmotionLabel.CURIOUS: "你现在很好奇，想了解更多。",
    EmotionLabel.SAD: "你现在有点难过，语气低沉。",
    EmotionLabel.ANGRY: "你现在有点生气。",
    EmotionLabel.SHY: "你现在有点害羞。",
    EmotionLabel.PROUD: "你现在很自豪，想分享团队成果！",
    EmotionLabel.NEUTRAL: "",
}
