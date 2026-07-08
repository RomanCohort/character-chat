"""Tests for Phase 1.5: WorldClock + SceneState + EventBus + parser."""
import sys
from pathlib import Path

src = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src))

import pytest
from character_chat.event.bus import EventBus
from character_chat.event.types import (
    TIME_ADVANCE, SCENE_TRANSITION, MESSAGE_RECEIVED
)
from character_chat.world.clock import (
    WorldClock, minute_to_phase, minute_to_hhmm
)
from character_chat.scene.state import SceneState, SceneManager, SceneObject
from character_chat.scene.parser import parse_scene_tag


class TestWorldClock:
    """Test narrative time clock."""

    def test_initial_state(self):
        """Clock starts at given time."""
        clock = WorldClock(day=1, minute=1020)  # 17:00
        assert clock.day == 1
        assert clock.minute == 1020
        assert clock.phase == "黄昏"
        assert clock.hhmm == "17:00"

    def test_phase_boundaries(self):
        """minute_to_phase correctly maps all day phases."""
        assert minute_to_phase(0) == "深夜"      # 00:00
        assert minute_to_phase(300) == "深夜"    # 05:00
        assert minute_to_phase(360) == "晨"      # 06:00
        assert minute_to_phase(720) == "午"      # 12:00
        assert minute_to_phase(1020) == "黄昏"   # 17:00
        assert minute_to_phase(1200) == "夜"     # 20:00
        assert minute_to_phase(1439) == "夜"     # 23:59

    def test_hhmm_formatting(self):
        """HH:MM format works."""
        assert minute_to_hhmm(0) == "00:00"
        assert minute_to_hhmm(1020) == "17:00"
        assert minute_to_hhmm(1439) == "23:59"

    def test_advance_progresses_time(self):
        """Each advance adds some minutes."""
        clock = WorldClock(day=1, minute=1020)
        start_minute = clock.minute
        clock.advance()
        assert clock.minute > start_minute

    def test_advance_publishes_event(self):
        """Advance publishes TIME_ADVANCE event."""
        bus = EventBus()
        received = []
        bus.subscribe(TIME_ADVANCE, lambda e: received.append(e.data))

        clock = WorldClock(day=1, minute=1020)
        clock.advance(bus)

        assert len(received) == 1
        assert "phase" in received[0]
        assert "delta" in received[0]

    def test_day_rollover(self):
        """Minute overflow rolls to next day."""
        clock = WorldClock(day=1, minute=1430)  # 23:50
        # advance_schedule[0] = 15 → 1445 → day 2, 00:05
        clock.advance()
        assert clock.day == 2
        assert clock.minute == 5  # 1445 - 1440
        assert clock.phase == "深夜"

    def test_describe(self):
        """describe() returns human-readable string."""
        clock = WorldClock(day=3, minute=1020)
        desc = clock.describe()
        assert "3" in desc
        assert "17:00" in desc
        assert "黄昏" in desc


class TestSceneState:
    """Test scene state and transitions."""

    def test_scene_creation(self):
        """SceneState can be created with name + phase."""
        scene = SceneState(name="天台", phase="黄昏")
        assert scene.name == "天台"
        assert scene.phase == "黄昏"
        assert scene.objects == []

    def test_scene_with_objects(self):
        """Scene can hold objects."""
        scene = SceneState(
            name="教室",
            phase="午",
            objects=[SceneObject(name="黑板"), SceneObject(name="课桌")],
            extra_people=2,
        )
        assert scene.get_object("黑板") is not None
        assert scene.get_object("不存在") is None
        assert scene.extra_people == 2

    def test_scene_transition_allowed(self):
        """黄昏 → 夜 is allowed."""
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene)
        success = mgr.transition("客厅", new_phase="夜")
        assert success is True
        assert mgr.current_scene.name == "客厅"
        assert mgr.current_scene.phase == "夜"

    def test_scene_transition_blocked(self):
        """黄昏 → 晨 is blocked (not in allowed transitions)."""
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene)
        success = mgr.transition("教室", new_phase="晨")
        assert success is False
        assert mgr.current_scene.name == "天台"  # unchanged

    def test_scene_transition_no_phase_keeps_current(self):
        """Transition without phase keeps current phase."""
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene)
        success = mgr.transition("客厅")  # no new_phase
        assert success is True
        assert mgr.current_scene.phase == "黄昏"  # kept

    def test_scene_transition_publishes_event(self):
        """Transition publishes SCENE_TRANSITION event."""
        bus = EventBus()
        received = []
        bus.subscribe(SCENE_TRANSITION, lambda e: received.append(e.data))

        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene, bus=bus)
        mgr.transition("客厅", new_phase="夜")

        assert len(received) == 1
        assert received[0]["to"]["name"] == "客厅"


class TestSceneParser:
    """Test LLM scene-tag parsing."""

    @pytest.mark.parametrize("text,expected_name,expected_phase", [
        ("<scene:回家,phase:夜>", "回家", "夜"),
        ("<scene:客厅,phase:午后>", "客厅", "午后"),
        ("<scene:卧室>", "卧室", None),
        ("< scene : 公园 , phase : 晨 >", "公园", "晨"),
        ("<SCENE:School,PHASE:午>", "School", "午"),  # case insensitive
    ])
    def test_strict_tag(self, text, expected_name, expected_phase):
        """Strict <scene:...,phase:...> tags parse correctly."""
        result = parse_scene_tag(text)
        assert result is not None
        name, phase = result
        assert name == expected_name
        assert phase == expected_phase

    def test_no_tag_returns_none(self):
        """Text without tag returns None."""
        assert parse_scene_tag("普通对话内容") is None
        assert parse_scene_tag("") is None

    def test_tag_in_middle_of_text(self):
        """Tag in the middle of text is still found."""
        text = "嗯，我们回家吧。<scene:家,phase:夜>"
        result = parse_scene_tag(text)
        assert result is not None
        assert result[0] == "家"


class TestEventBus:
    """Test event bus integration."""

    def test_subscribe_and_publish(self):
        """Basic pub/sub works."""
        bus = EventBus()
        received = []
        bus.subscribe(MESSAGE_RECEIVED, lambda e: received.append(e.data["text"]))
        bus.publish(MESSAGE_RECEIVED, {"text": "hello"}, source="test")

        assert received == ["hello"]

    def test_priority_ordering(self):
        """Lower priority runs first."""
        bus = EventBus()
        order = []
        bus.subscribe("E", lambda e: order.append("low"), priority=10)
        bus.subscribe("E", lambda e: order.append("high"), priority=0)
        bus.publish("E")

        assert order == ["high", "low"]

    def test_unsubscribe(self):
        """Unsubscribe stops handler from being called."""
        bus = EventBus()
        received = []

        def handler(e):
            received.append(1)

        bus.subscribe("E", handler)
        bus.publish("E")
        assert len(received) == 1

        bus.unsubscribe("E", handler)
        bus.publish("E")
        assert len(received) == 1  # still 1, not called again


class TestTimeSceneIntegration:
    """Integration: clock + scene + bus work together."""

    def test_clock_advance_does_not_change_scene(self):
        """Advancing clock alone doesn't move scene."""
        bus = EventBus()
        scene_events = []
        bus.subscribe(SCENE_TRANSITION, lambda e: scene_events.append(e))

        clock = WorldClock(day=1, minute=1020)
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene, bus=bus)

        clock.advance(bus)  # should only fire TIME_ADVANCE
        assert len(scene_events) == 0  # no scene transition

    def test_full_flow(self):
        """Full flow: advance time, transition scene, all events fire."""
        bus = EventBus()
        events = []
        for etype in [TIME_ADVANCE, SCENE_TRANSITION]:
            bus.subscribe(etype, lambda e: events.append((e.type, e.data)))

        clock = WorldClock(day=1, minute=1020)
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene, bus=bus)

        clock.advance(bus)
        mgr.transition("客厅", new_phase="夜")

        event_types = [t for t, _ in events]
        assert TIME_ADVANCE in event_types
        assert SCENE_TRANSITION in event_types
