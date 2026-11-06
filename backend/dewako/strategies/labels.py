"""Which sides each built-in strategy can signal (read from its source: every BUY / SELL entry it emits).

Kept OUT of the strategy classes on purpose: a strategy's fingerprint is a hash of its source, so adding a label line to the class would
change the fingerprint of every released strategy and mark it INVALID at startup, for a change that alters no behaviour.
A strategy that is not listed here (and is not rule-built) has undeclared directions; a test requires every built-in to be listed.
"""
from __future__ import annotations

BUILTIN_DIRECTIONS: dict[str, tuple[str, ...]] = {
    "Trend AI": ("LONG", "SHORT"),
    "Momentum AI": ("LONG", "SHORT"),
    "Breakout AI": ("LONG", "SHORT"),
    "Mean Reversion": ("LONG", "SHORT"),
    "AI Strategy": ("LONG", "SHORT"),
}


def directions_of(cls) -> tuple[str, ...]:
    declared = cls.__dict__.get("directions")                     # rule-built strategies carry it from their spec
    if declared is not None:
        return tuple(declared)
    return BUILTIN_DIRECTIONS.get(cls.name, ())
