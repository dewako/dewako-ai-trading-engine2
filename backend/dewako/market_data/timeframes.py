from __future__ import annotations

from datetime import datetime, timedelta, timezone

SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def seconds(tf: str) -> int:
    try:
        return SECONDS[tf]
    except KeyError:
        raise ValueError(f"unsupported timeframe {tf!r}. Supported: {sorted(SECONDS, key=SECONDS.get)}") from None


def delta(tf: str) -> timedelta:
    return timedelta(seconds=seconds(tf))


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: use timezone-aware UTC datetimes")
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def floor(dt: datetime, tf: str) -> datetime:
    s = seconds(tf)
    return from_ms((to_ms(dt) // 1000 // s * s) * 1000)
