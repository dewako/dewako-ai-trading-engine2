"""Virtual time for replay: nothing in a replay may read the wall clock."""
from __future__ import annotations

from datetime import datetime, timedelta


class VirtualClock:
    """A clock that only moves when told to, and never backwards. `clock()` returns the current virtual time (timezone-aware)."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("virtual time must be timezone-aware")
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    now = __call__

    def set(self, t: datetime) -> None:
        if t.tzinfo is None:
            raise ValueError("virtual time must be timezone-aware")
        if t < self._now:
            raise ValueError(f"virtual time cannot go backwards ({t.isoformat()} < {self._now.isoformat()})")
        self._now = t

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("virtual time cannot go backwards")
        self._now += delta
