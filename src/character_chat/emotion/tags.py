"""Expression tag selector (Layer 4) - maps emotion to action/tone tags.

Mapping is defined in CharacterCard.expression_tags.
Tags are rendered as ANSI-dimmed: *抬手* （轻声）
"""
from typing import Dict, List, Optional
from .state import EmotionLabel


class ExpressionTagSelector:
    """Select expression tags based on current emotion."""

    # Must call load_character_tags() first
    def __init__(self):
        self._character_tags: Dict[EmotionLabel, List[str]] = {}

    def load_character_tags(self, expression_tags: Dict[str, List[str]]):
        """Load expression tags from character card YAML.

        Args:
            expression_tags: Dict from emotion names to list of tags.
                           "shy": ["*低头*", "（小声）"] etc.
        """
        self._character_tags = {}
        for emotion_name, tags in expression_tags.items():
            try:
                label = EmotionLabel(emotion_name.lower())
                self._character_tags[label] = tags
            except ValueError:
                continue  # ignore invalid emotion names

    def select_tags(self, dominant: EmotionLabel, top_n: int = 1) -> List[str]:
        """Select top N tags for dominant emotion.

        If emotion not defined, returns empty list.
        """
        tags = self._character_tags.get(dominant, [])
        # Return top N (or all if < N)
        return tags[:top_n]

    def format_for_cli(self, tags: List[str]) -> str:
        """Format tags for CLI output (ANSI-dimmed)."""
        # Simple strategy: dim all tags
        parts = []
        for tag in tags:
            # Mark tag as dimmed: *tag* -> \033[2m*tag*\033[0m
            parts.append(f"\033[2m{tag}\033[0m")
        return " ".join(parts)
