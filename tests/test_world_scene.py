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


# ---------------------------------------------------------------------------
# 时间冻结工具（2026-09-12 加）
#
# WorldClock 从"叙事虚构时间"重写成了"真实墙钟"：day/minute/phase 不再是构造
# 参数，而是从 datetime.now() / date.today() 算出来的只读属性。所以测试不能再
# "构造一个 17:00 的时钟"，必须把系统时间钉住。
#
# 只补丁 clock 模块里引用的 datetime / date 两个名字，不动全局，别的测试不受影响。
# ---------------------------------------------------------------------------
import datetime as _dt


class _FrozenDateTime(_dt.datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


class _FrozenDate(_dt.date):
    @classmethod
    def today(cls):
        # 从冻结的"现在"推导，这样 set_now() 推进会同时带动 date.today()。
        # （第一版写成静态快照，结果 set_now 之后 date.today() 还答旧日期，
        #   day 属性永远算成 1——跨天测试因此假失败。）
        return _FrozenDateTime._now.date()


def _freeze(monkeypatch, moment: _dt.datetime, first_date: _dt.date | None = None):
    """把 character_chat.world.clock 看到的时间钉在 `moment`。

    first_date 是"第一次互动"的锚点（决定 day 从哪天算起）；不传时由 moment 推导。

    返回一个 setter：调用它就能把时间推到新的一刻，用来测 phase/day 变化。
    """
    from character_chat.world import clock as clock_mod

    _FrozenDateTime._now = moment
    if first_date is not None:
        _FrozenDate._today = first_date   # 显式锚点（默认走 today() 推导）
    monkeypatch.setattr(clock_mod, "datetime", _FrozenDateTime, raising=False)
    monkeypatch.setattr(clock_mod, "date", _FrozenDate, raising=False)

    def set_now(new_moment: _dt.datetime):
        _FrozenDateTime._now = new_moment

    return set_now


def _clock_at(monkeypatch, moment: _dt.datetime, **kw):
    """钉住时间并造一个 WorldClock。**必须用 tmp_path 当 data_dir**。

    不传 data_dir 时 clock.py 会退到 Path("data")，也就是往工作目录里
    写 first_interaction.json —— 测试不该有这种副作用。
    """
    data_dir = kw.pop("data_dir", None)
    if data_dir is None:
        raise ValueError("_clock_at 必须给 data_dir=tmp_path（否则会往工作目录写文件）")
    _freeze(monkeypatch, moment)
    return WorldClock(data_dir=data_dir)


class TestWorldClock:
    """Test narrative time clock."""

    def test_initial_state(self, monkeypatch, tmp_path):
        """Clock reads the pinned wall-clock time."""
        clock = _clock_at(monkeypatch, _dt.datetime(2026, 9, 12, 17, 0), data_dir=tmp_path)
        assert clock.day == 1          # 锚点默认=今天 → 第 1 天
        assert clock.minute == 1020    # 17:00
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

    def test_advance_is_lazy_sync_not_progress(self, monkeypatch, tmp_path):
        """advance() 是"同步到真实时间"，不是"推进时间"。

        旧契约（narrative clock）：每轮加几分钟，所以 `advance()` 后 minute 变大。
        新契约（wall clock）：minute 直接读系统时钟。第一次调用只做快照、
        返回 0、不发事件——这正是它避免每轮都唤醒订阅者的机制。
        """
        clock = _clock_at(monkeypatch, _dt.datetime(2026, 9, 12, 17, 0), data_dir=tmp_path)
        bus = EventBus()
        received = []
        bus.subscribe(TIME_ADVANCE, lambda e: received.append(e.data))

        delta = clock.advance(bus)

        assert delta == 0            # 首次调用只快照
        assert received == []        # 因此不发事件
        assert clock.minute == 1020  # 时间没有被"推进"，它就是墙钟

    def test_advance_publishes_on_phase_change(self, monkeypatch, tmp_path):
        """phase 真的变了，才发 TIME_ADVANCE。"""
        set_now = _freeze(monkeypatch, _dt.datetime(2026, 9, 12, 15, 0))
        clock = WorldClock(data_dir=tmp_path)   # 15:00 → 午
        bus = EventBus()
        received = []
        bus.subscribe(TIME_ADVANCE, lambda e: received.append(e.data))

        clock.advance(bus)                      # 首次：快照，不发
        assert received == []

        set_now(_dt.datetime(2026, 9, 12, 17, 0))   # 午 → 黄昏
        clock.advance(bus)

        assert len(received) == 1
        assert received[0]["phase"] == "黄昏"
        assert "delta" in received[0]

    def test_advance_is_silent_when_nothing_changes(self, monkeypatch, tmp_path):
        """phase 和 day 都没变时，重复 advance 不该刷事件。

        （旧 day_rollover 测试测的是"分钟溢出跨天"——墙钟不会溢出，
          跨天是真实午夜，见下面的 test_day_rollover_on_real_midnight。）
        """
        set_now = _freeze(monkeypatch, _dt.datetime(2026, 9, 12, 17, 0))
        clock = WorldClock(data_dir=tmp_path)
        bus = EventBus()
        received = []
        bus.subscribe(TIME_ADVANCE, lambda e: received.append(e.data))

        clock.advance(bus)                            # 快照
        set_now(_dt.datetime(2026, 9, 12, 17, 30))    # 同一 phase 内
        clock.advance(bus)
        clock.advance(bus)

        assert received == []

    def test_day_rollover_on_real_midnight(self, monkeypatch, tmp_path):
        """真实午夜跨天：day +1，phase 落回深夜，并标记 day_changed。"""
        set_now = _freeze(monkeypatch, _dt.datetime(2026, 9, 12, 23, 50))
        clock = WorldClock(data_dir=tmp_path)
        assert clock.day == 1

        bus = EventBus()
        received = []
        bus.subscribe(TIME_ADVANCE, lambda e: received.append(e.data))
        clock.advance(bus)                            # 快照（夜）

        set_now(_dt.datetime(2026, 9, 13, 0, 5))      # 午夜之后
        clock.advance(bus)

        assert clock.day == 2
        assert clock.minute == 5
        assert clock.phase == "深夜"
        assert len(received) == 1
        assert received[0].get("day_changed") is True

    def test_describe(self, monkeypatch, tmp_path):
        """describe() 的格式是 `第N天 HH:MM（phase）`。"""
        import dataclasses

        clock = _clock_at(
            monkeypatch, _dt.datetime(2026, 9, 14, 17, 0), data_dir=tmp_path,
        )
        # 锚点改成 9/12 → 9/14 就是第 3 天（dataclasses.replace 会重跑 __post_init__，
        # 但 first_interaction.json 已存在且日期是 9/14，所以随后手动覆盖锚点）
        clock = dataclasses.replace(clock, data_dir=tmp_path)
        clock._first_date = _dt.date(2026, 9, 12)

        desc = clock.describe()
        assert "第3天" in desc
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

    def test_clock_advance_does_not_change_scene(self, monkeypatch, tmp_path):
        """Advancing clock alone doesn't move scene."""
        bus = EventBus()
        scene_events = []
        bus.subscribe(SCENE_TRANSITION, lambda e: scene_events.append(e))

        clock = WorldClock(data_dir=tmp_path)
        _freeze(monkeypatch, _dt.datetime(2026, 9, 12, 17, 0))
        scene = SceneState(name="天台", phase="黄昏")
        mgr = SceneManager(scene, bus=bus)

        clock.advance(bus)  # should only fire TIME_ADVANCE
        assert len(scene_events) == 0  # no scene transition

    def test_full_flow(self, monkeypatch, tmp_path):
        """Full flow: advance time, transition scene, all events fire."""
        bus = EventBus()
        events = []
        for etype in [TIME_ADVANCE, SCENE_TRANSITION]:
            bus.subscribe(etype, lambda e: events.append((e.type, e.data)))

        set_now = _freeze(monkeypatch, _dt.datetime(2026, 9, 12, 15, 0))
        clock = WorldClock(data_dir=tmp_path)   # 15:00 → 午
        scene = SceneState(name="天台", phase="午")
        mgr = SceneManager(scene, bus=bus)

        clock.advance(bus)                      # 首次只快照，不发事件
        set_now(_dt.datetime(2026, 9, 12, 17, 0))   # 午 → 黄昏，这次会发
        clock.advance(bus)
        # 场景转移要遵守 SceneManager 的转移表：午 → ["黄昏"]。
        # 原来这里写的是 new_phase="夜"，午 到 夜 不合法，transition() 会返回 False
        # 且不发事件——那个断言从一开始就是错的（只是以前构造函数先抛了，没跑到）。
        assert mgr.transition("客厅", new_phase="黄昏") is True

        event_types = [t for t, _ in events]
        assert TIME_ADVANCE in event_types
        assert SCENE_TRANSITION in event_types
