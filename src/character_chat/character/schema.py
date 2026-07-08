"""Character card schema — extended from IGEM-sama's CharacterConfig.

Adds background/relationships/scenario/expression_tags for role-play use.
Self-contained (no zerolan.data dependency).
"""
from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class FilterConfig(BaseModel):
    bad_words: List[str] = Field(
        default=["作为一名人工智能助手", "人工智能助手", "作为一个AI"],
        description="Words to filter out of responses."
    )


class ShortTermMemoryConfig(BaseModel):
    enable: bool = Field(default=True)
    max_recent_messages: int = Field(default=10)
    summary_threshold: int = Field(default=16)
    max_summaries: int = Field(default=3)
    max_summary_chars: int = Field(default=200)


class ChatConfig(BaseModel):
    filter: FilterConfig = Field(default_factory=FilterConfig)
    system_prompt: str = Field(default="")
    injected_history: List[str] = Field(
        default_factory=list,
        description="Seed Q&A pairs (even count: q, a, q, a, ...)."
    )
    max_history: int = Field(default=20)
    short_term_memory: Optional[ShortTermMemoryConfig] = Field(default=None)


class PersonalityConfig(BaseModel):
    """Trait values in [0, 1]. Keys: tsundere/warm/lively/knowledgeable/playful/scientific."""
    traits: Dict[str, float] = Field(default_factory=dict)
    speaking_style: str = Field(default="")


class RelationshipConfig(BaseModel):
    target: str = Field(default="user")
    type: str = Field(default="陌生人")
    affection: float = Field(default=0.0, description="Affection in [0, 1].")


class ScenarioConfig(BaseModel):
    setting: str = Field(default="")
    opening: str = Field(default="", description="Character's opening line.")


class CharacterCard(BaseModel):
    """Full character card — loadable from YAML in /config/characters/."""
    bot_name: str
    background: str = Field(default="")
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    relationships: List[RelationshipConfig] = Field(default_factory=list)
    scenario: ScenarioConfig = Field(default_factory=ScenarioConfig)
    expression_tags: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Emotion label -> list of expression tags like '*低头*'."
    )
    system_prompt: str = Field(default="")
    chat: ChatConfig = Field(default_factory=ChatConfig)

    def get_relationship(self, target: str = "user") -> RelationshipConfig:
        for r in self.relationships:
            if r.target == target:
                return r
        return RelationshipConfig(target=target)
