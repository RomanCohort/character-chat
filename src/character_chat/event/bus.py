"""Event bus — lifted from CLF core/event_bus.py (sync, lightweight).

Dropped the global singleton; instances are injected instead.
"""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


# Use a monotonic counter for timestamps instead of wall-clock,
# since this app uses narrative time (not real time). The timestamp
# here is just event metadata for logging/debugging.
_counter = [0]


def _next_seq() -> int:
    _counter[0] += 1
    return _counter[0]


@dataclass
class Event:
    """Event object."""
    type: str
    data: dict[str, Any]
    source: str
    seq: int = field(default_factory=_next_seq)


@dataclass
class Subscription:
    """Subscription record."""
    handler: Callable
    priority: int = 0  # lower runs first
    name: str = ""


class EventBus:
    """Lightweight synchronous event bus.

    Usage:
        bus = EventBus()
        bus.subscribe("MESSAGE_RECEIVED", self.on_msg, priority=0)
        bus.publish("MESSAGE_RECEIVED", {"text": "hi"}, source="cli")
    """

    def __init__(self, log_enabled: bool = False):
        self._subscribers: dict[str, list[Subscription]] = defaultdict(list)
        self._log_enabled = log_enabled
        self._log: list[Event] = []
        self._publish_count: dict[str, int] = defaultdict(int)
        self._error_counts: dict[str, int] = defaultdict(int)

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        priority: int = 0,
        name: str = "",
    ) -> None:
        sub = Subscription(
            handler=handler, priority=priority, name=name or handler.__name__
        )
        self._subscribers[event_type].append(sub)
        self._subscribers[event_type].sort(key=lambda s: s.priority)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        self._subscribers[event_type] = [
            s for s in self._subscribers[event_type] if s.handler is not handler
        ]

    def publish(
        self,
        event_type: str,
        data: dict[str, Any] = None,
        source: str = "",
    ) -> dict[str, Any]:
        data = data or {}
        event = Event(type=event_type, data=data, source=source)
        collected: dict[str, Any] = {}
        self._publish_count[event_type] += 1
        if self._log_enabled:
            self._log.append(event)
        for sub in self._subscribers.get(event_type, []):
            try:
                result = sub.handler(event)
                if result is not None and isinstance(result, dict):
                    collected[sub.name] = result
            except Exception:
                key = f"{event_type}->{sub.name}"
                self._error_counts[key] = self._error_counts.get(key, 0) + 1
        return collected

    def get_stats(self) -> dict[str, Any]:
        return {
            "publish_counts": dict(self._publish_count),
            "subscriber_counts": {
                etype: len(subs) for etype, subs in self._subscribers.items()
            },
            "log_size": len(self._log),
            "error_counts": dict(self._error_counts),
        }

    def reset(self) -> None:
        self._subscribers.clear()
        self._log.clear()
        self._publish_count.clear()
