"""One place for 'what time is it' for stored timestamps and engine defaults.

In production nothing overrides it, so `now()` is the wall clock and behaviour is unchanged. The backtest replay uses `virtual_time`
so that every timestamp that is stored or compared (orders, positions, audit, exit ages, stall detection, freshness) follows
simulated time, not the machine's clock. `virtual_time` is for research code only: a test pins that the live application never imports it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

_override: Optional[Callable[[], datetime]] = None


def now() -> datetime:
    if _override is None:
        return datetime.now(timezone.utc)
    t = _override()
    if t.tzinfo is None:
        raise ValueError("virtual time must be timezone-aware")
    return t


def is_virtual() -> bool:
    return _override is not None


@contextmanager
def virtual_time(source: Callable[[], datetime]) -> Iterator[None]:
    global _override
    if _override is not None:
        raise RuntimeError("virtual time is already active; nested replays would corrupt each other's timestamps")
    _override = source
    try:
        yield
    finally:
        _override = None
