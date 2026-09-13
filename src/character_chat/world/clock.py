"""WorldClock — real-time wall clock aligned with system time.

Time is read from the system clock (datetime.now). The clock tracks:
  - day:       integer day counter since first_interaction_date (real-world day N)
  - minute:    minutes since day start, 0..1439 (real wall-clock)
  - phase:     derived from minute (晨/午/黄昏/夜/深夜)

Phase boundaries:
  晨    06:00 - 11:59
  午    12:00 - 16:59
  黄昏  17:00 - 19:59
  夜    20:00 - 23:59
  深夜  00:00 - 05:59

Advance policy:
  - `advance(bus)` is called on every user message (lazy sync).
  - It publishes TIME_ADVANCE ONLY when (phase OR day) changes vs. last read,
    so subscribers (sleep_reset / fade_memories / fatigue) fire at most a few
    times per day instead of every message.
  - Day rollover (real midnight) → day_changed=True → triggers mood sleep_reset.
  - Phase transition → day_changed=False → triggers fade + fatigue.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..event.bus import EventBus
from ..event.types import TIME_ADVANCE


PHASES = ("深夜", "晨", "午", "黄昏", "夜")

# First-interaction anchor file: {"first_interaction_date": "YYYY-MM-DD"}
FIRST_INTERACTION_FILE = "first_interaction.json"


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
    """Real-time wall clock with persistent first-interaction anchor.

    day = (today - first_interaction_date).days + 1
    minute = now.hour * 60 + now.minute  (real wall-clock)
    """
    data_dir: Optional[Path] = field(default=None)
    _first_date: date = field(default=None, init=False, repr=False)
    _last_minute: int = field(default=-1, init=False, repr=False)
    _last_day: int = field(default=-1, init=False, repr=False)
    _last_phase: str = field(default="", init=False, repr=False)
    _last_advance_ts: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self):
        self._first_date = self._load_or_init_first_date(self.data_dir)

    # ------------------------------------------------------------------
    # Properties (same API as the old narrative clock)
    # ------------------------------------------------------------------
    @property
    def day(self) -> int:
        return (date.today() - self._first_date).days + 1

    @property
    def minute(self) -> int:
        now = datetime.now()
        return now.hour * 60 + now.minute

    @property
    def phase(self) -> str:
        return minute_to_phase(self.minute)

    @property
    def hhmm(self) -> str:
        return minute_to_hhmm(self.minute)

    def describe(self) -> str:
        """Human-readable current time string for prompt injection.

        Format: `第{day}天 {hhmm}（{phase}）` — same as the old narrative clock
        so consumers (_build_system_prompt, render_status) need zero changes.
        """
        return f"第{self.day}天 {self.hhmm}（{self.phase}）"

    def to_dict(self) -> dict:
        return {"day": self.day, "minute": self.minute, "phase": self.phase,
                "hhmm": self.hhmm}

    # ------------------------------------------------------------------
    # Advance — lazy sync to real wall-clock, publishes only on change
    # ------------------------------------------------------------------
    def advance(self, bus: EventBus | None = None) -> int:
        """Sync to real time; publish TIME_ADVANCE only on phase/day change.

        Returns minutes since last advance (real delta), or 0 on first call.
        """
        now = datetime.now()
        real_minute = now.hour * 60 + now.minute
        real_day = (now.date() - self._first_date).days + 1
        real_phase = minute_to_phase(real_minute)

        # First call — just snapshot, no publish
        if self._last_minute < 0:
            self._last_minute = real_minute
            self._last_day = real_day
            self._last_phase = real_phase
            self._last_advance_ts = now.timestamp()
            return 0

        # Real delta since last advance (for subscribers that want it)
        delta_ts = now.timestamp() - self._last_advance_ts
        delta_minutes = max(0, int(delta_ts / 60.0))

        day_changed = (real_day != self._last_day)
        phase_changed = (real_phase != self._last_phase)

        # Update snapshot
        self._last_minute = real_minute
        self._last_day = real_day
        self._last_phase = real_phase
        self._last_advance_ts = now.timestamp()

        if bus is not None and (day_changed or phase_changed):
            bus.publish(
                TIME_ADVANCE,
                {
                    "day": real_day,
                    "minute": real_minute,
                    "phase": real_phase,
                    "hhmm": minute_to_hhmm(real_minute),
                    "delta": delta_minutes,
                    "day_changed": day_changed,
                    "phase_changed": phase_changed,
                },
                source="world_clock",
            )
        return delta_minutes

    # ------------------------------------------------------------------
    # jump_to — kept for API compat but not called by anyone
    # ------------------------------------------------------------------
    def jump_to(self, minute: int, day: int | None = None):
        """No-op under real-time clock. Kept for API compatibility.

        Real time can't be overridden by manual jump; this is intentionally
        a no-op so callers that still reference it don't crash.
        """
        return

    # ------------------------------------------------------------------
    # Persistence — first-interaction anchor
    # ------------------------------------------------------------------
    def _load_or_init_first_date(self, data_dir: Path | None) -> date:
        """Load first_interaction_date from disk, or create today.

        The file is {data_dir}/first_interaction.json with shape
        {"first_interaction_date": "YYYY-MM-DD"}.
        """
        path = self._first_interaction_path(data_dir)
        if path is None:
            return date.today()

        if path.exists():
            try:
                import json
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                return date.fromisoformat(payload["first_interaction_date"])
            except Exception:
                # Corrupted → reset to today (defensive)
                pass

        # Initialize
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        payload = {"first_interaction_date": date.today().isoformat()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return date.today()

    def _first_interaction_path(self, data_dir: Path | None) -> Path | None:
        if data_dir is None:
            return None
        return data_dir / FIRST_INTERACTION_FILE
