"""Character card schema — extended from IGEM-sama's CharacterConfig.

Adds background/relationships/scenario/expression_tags for role-play use.
Self-contained (no zerolan.data dependency).
"""
from typing import Any, List, Optional, Dict
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


class WorldConfig(BaseModel):
    """角色卡里的「世界」——时段节律与环境事件，全部可选。

    不写就用内置那套（那套是按凌暮雪写的，只给她用）；写了就走卡。
    访客实例一律走卡：没写 → 只留一句中性的时段提示，不会带上凌暮雪的
    时段话题和烦心事。

    示例（YAML）：
        world:
          pronoun: 她
          phases:
            夜: {activity: "在写作业", tone: "安静", topics: "聊聊今天"}
          weekend:
            夜: "周末晚上，没有明天要交的压力"
          meals:
            晚饭: "刚吃完外卖"
          worries:
            - {name: "作业写不完", mood_modifier: -0.2, weight: 0.3}
    """
    pronoun: str = Field(default="", description="'她'/'他'/''（你）")
    phases: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    weekend: Dict[str, str] = Field(default_factory=dict)
    meals: Dict[str, str] = Field(default_factory=dict)
    sleep: Dict[str, str] = Field(default_factory=dict)
    worries: List[Dict[str, Any]] = Field(default_factory=list)


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
    world: WorldConfig = Field(default_factory=WorldConfig)

    def get_relationship(self, target: str = "user") -> RelationshipConfig:
        for r in self.relationships:
            if r.target == target:
                return r
        return RelationshipConfig(target=target)
