"""WorldClock — narrative time axis (no real-time dependency).

Time is purely fictional/narrative. Each user turn advances the clock
by a small, context-sensitive delta. The clock tracks:
  - day:       integer day counter (day 1, day 2, ...)
  - minute:    minutes since day start, 0..1439
  - phase:     derived from minute (晨/午/黄昏/夜/深夜)

Phase boundaries (configurable):
  晨    06:00 - 11:59
  午    12:00 - 16:59
  黄昏  17:00 - 19:59
  夜    20:00 - 23:59
  深夜  00:00 - 05:59
"""
from dataclasses import dataclass, field
from typing import Tuple

from ..event.bus import EventBus
from ..event.types import TIME_ADVANCE


PHASES = ("深夜", "晨", "午", "黄昏", "夜")


def minute_to_phase(minute: int) -> str:
    """Map minute-of-day [0,1439] to a phase label."""
    if minute < 360:        # 00:00 - 05:59
        return "深夜"
    elif minute < 720:      # 06:00 - 11:59
        return "晨"
    elif minute < 1020:     # 12:00 - 16:59
        return "午"
    elif minute < 1200:     # 17:00 - 19:59
        return "黄昏"
    else:                   # 20:00 - 23:59
        return "夜"


def minute_to_hhmm(minute: int) -> str:
    """Format minute-of-day as HH:MM."""
    h = (minute // 60) % 24
    m = minute % 60
    return f"{h:02d}:{m:02d}"


@dataclass
class WorldClock:
    """Narrative time. Starts at the scenario's opening time."""
    day: int = 1
    minute: int = 480  # 08:00 by default
    # Per-turn advance range (minutes). The actual delta is sampled
    # deterministically by turn index to avoid Math.random (banned in
    # some contexts); we use a simple rotation here.
    advance_schedule: Tuple[int, ...] = field(
        default_factory=lambda: (15, 30, 20, 45, 10, 60, 25)
    )
    _turn: int = 0

    @property
    def phase(self) -> str:
        return minute_to_phase(self.minute)

    @property
    def hhmm(self) -> str:
        return minute_to_hhmm(self.minute)

    def advance(self, bus: EventBus | None = None) -> int:
        """Advance narrative time by one turn's delta.

        Returns the number of minutes advanced.
        """
        delta = self.advance_schedule[self._turn % len(self.advance_schedule)]
        self._turn += 1
        self.minute += delta
        # Roll over days
        while self.minute >= 1440:
            self.minute -= 1440
            self.day += 1
        if bus is not None:
            bus.publish(
                TIME_ADVANCE,
                {"day": self.day, "minute": self.minute,
                 "phase": self.phase, "hhmm": self.hhmm,
                 "delta": delta},
                source="world_clock",
            )
        return delta

    def jump_to(self, minute: int, day: int | None = None):
        """Jump to a specific time (used by scene transitions)."""
        if day is not None:
            self.day = day
        self.minute = minute % 1440

    def describe(self) -> str:
        """Human-readable narrative time string for prompt injection."""
        return f"第{self.day}天 {self.hhmm}（{self.phase}）"

    def to_dict(self) -> dict:
        return {"day": self.day, "minute": self.minute, "phase": self.phase,
                "hhmm": self.hhmm}
