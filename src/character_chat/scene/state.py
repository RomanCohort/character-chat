"""SceneState — scene as a time-phased state object.

场景是状态机节点，包含当前相位 + 在场物品 + 在场人数。转移由 LLM 输出
scene 标签触发，解析后更新状态。
"""
from dataclasses import dataclass, field
from typing import Optional
from ..event.bus import EventBus
from ..event.types import SCENE_TRANSITION


PHASES = ("深夜", "晨", "午", "黄昏", "夜")


@dataclass
class SceneObject:
    """物体/环境因素"""
    name: str
    state: dict[str, str] = field(default_factory=dict)  # `{属性名: 值}`
    talkable: bool = True  # 是否可交互（影响 LLM 的限制）


@dataclass
class SceneState:
    """场景状态机节点"""
    name: str                          # 场景名称（如"放学后的天台"、"小夜家"）
    phase: str                         # 当前相位（深夜/晨/午/黄昏/夜）
    objects: list[SceneObject] = field(default_factory=list)  # 在场物品列表
    extra_people: int = 0              # 除用户外的在场人数

    def get_object(self, name: str) -> Optional[SceneObject]:
        for obj in self.objects:
            if obj.name == name:
                return obj
        return None

    def update_or_add_object(self, name: str, talkable: bool = True):
        """Add or update an object. If exists, update state; else create."""
        obj = self.get_object(name)
        if obj is None:
            self.objects.append(SceneObject(name=name, talkable=talkable))
        else:
            obj.talkable = talkable

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "phase": self.phase,
            "objects": [
                {"name": o.name, "state": o.state, "talkable": o.talkable}
                for o in self.objects
            ],
            "extra_people": self.extra_people,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SceneState":
        """Load from dict."""
        objects = []
        for o in data.get("objects", []):
            objects.append(SceneObject(
                name=o["name"],
                state=o.get("state", {}),
                talkable=o.get("talkable", True),
            ))
        return cls(
            name=data["name"],
            phase=data["phase"],
            objects=objects,
            extra_people=data.get("extra_people", 0),
        )


class SceneManager:
    """Scene manager with transition rules and event publishing."""

    def __init__(self, initial_scene: SceneState, bus: EventBus | None = None):
        self.current_scene = initial_scene
        self.bus = bus

        # Transition rules: if current_scene in phase, can go to target
        self.transitions = {
            "黄昏": ["深夜", "夜"],
            "夜": ["深夜", "晨"],
            "深夜": ["晨", "夜"],
            "晨": ["午", "黄昏"],
            "午": ["黄昏"],
        }

    def transition(self, new_name: str, new_phase: Optional[str] = None) -> bool:
        """Transition to a new scene.

        Returns True if transition succeeded, False if phase doesn't allow.
        """
        current_phase = self.current_scene.phase
        allowed = self.transitions.get(current_phase, [])
        if new_phase and new_phase not in allowed:
            return False

        if self.bus:
            self.bus.publish(
                SCENE_TRANSITION,
                {
                    "from": self.current_scene.to_dict(),
                    "to": {"name": new_name, "phase": new_phase or current_phase},
                },
                source="scene_manager",
            )
        self.current_scene = SceneState(
            name=new_name,
            phase=new_phase or current_phase if new_phase else current_phase,
        )
        return True

    def current_to_dict(self) -> dict:
        return self.current_scene.to_dict()

    def current_human_readable(self) -> str:
        """Human-readable summary (scene + phase + objects + people_count)."""
        objects_str = "、".join(o.name for o in self.current_scene.objects)
        people = f" + {self.current_scene.extra_people} others" if self.current_scene.extra_people else ""
        return f"{self.current_scene.name}（{self.current_scene.phase}）{'/' + objects_str if objects_str else ''}{people}"
