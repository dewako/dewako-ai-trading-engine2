"""Fail-closed validation for closed candle sequences used to make decisions."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dewako.core.types import Candle
from dewako.market_data.timeframes import seconds


class CandleFreshnessError(ValueError):
    """Candle data is malformed, incomplete, unclosed, or too old to trust."""


@dataclass(frozen=True)
class CandleFreshnessPolicy:
    max_age_intervals: int = 2
    clock_skew_tolerance_seconds: float = 30.0
    # Exchange maintenance leaves permanent holes (Binance halted for 1-6 hours about 16 times in 2020-2023). With 0 (the default) any hole
    # inside the decision window refuses every decision until the hole has scrolled out of the window: 300 candles = 50 days on 4h.
    # N > 0 accepts holes of at most N missing candles each, still on the timeframe grid. The newest candle must still be closed and fresh.
    max_gap_intervals: int = 0

    def __post_init__(self) -> None:
        if (isinstance(self.max_age_intervals, bool) or not isinstance(self.max_age_intervals, int)
                or not 1 <= self.max_age_intervals <= 10):
            raise ValueError("max_age_intervals must be an integer in [1, 10]")
        if (isinstance(self.max_gap_intervals, bool) or not isinstance(self.max_gap_intervals, int)
                or not 0 <= self.max_gap_intervals <= 12):
            raise ValueError("max_gap_intervals must be an integer in [0, 12]")
        if (isinstance(self.clock_skew_tolerance_seconds, bool)
                or not isinstance(self.clock_skew_tolerance_seconds, (int, float))
                or not math.isfinite(self.clock_skew_tolerance_seconds)
                or self.clock_skew_tolerance_seconds < 0):
            raise ValueError("clock_skew_tolerance_seconds must be finite and non-negative")

    @classmethod
    def from_env(cls) -> "CandleFreshnessPolicy":
        return cls(
            max_age_intervals=int(os.environ.get("DEWAKO_CANDLE_MAX_AGE_INTERVALS", "2")),
            clock_skew_tolerance_seconds=float(os.environ.get("DEWAKO_CANDLE_CLOCK_SKEW_SECONDS", "30")),
            max_gap_intervals=int(os.environ.get("DEWAKO_CANDLE_MAX_GAP_INTERVALS", "0")),
        )


def validate_candles(candles: list[Candle], timeframe: str, now: datetime, *,
                     policy: CandleFreshnessPolicy | None = None) -> None:
    """Validate timestamps, continuity, candle values, closure and age without rewriting source data."""
    if not candles:
        raise CandleFreshnessError("market data contains no candles")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CandleFreshnessError("validation clock must return a timezone-aware datetime")
    p = policy or CandleFreshnessPolicy.from_env()
    interval = seconds(timeframe)
    step = timedelta(seconds=interval)
    now = now.astimezone(timezone.utc)
    previous: datetime | None = None

    for index, candle in enumerate(candles):
        ts = getattr(candle, "t", None)
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            raise CandleFreshnessError(f"candle {index} has a missing or timezone-naive timestamp")
        ts = ts.astimezone(timezone.utc)
        if ts.timestamp() % interval != 0:
            raise CandleFreshnessError(f"candle {index} timestamp is not aligned to {timeframe}")
        if ts > now + timedelta(seconds=p.clock_skew_tolerance_seconds):
            raise CandleFreshnessError(f"candle {index} timestamp is future-dated beyond clock-skew tolerance")
        if previous is not None:
            if ts <= previous:
                raise CandleFreshnessError("candle timestamps are out of order or duplicated")
            if ts - previous != step:
                missing = (ts - previous) // step - 1
                if (ts - previous) % step or missing > p.max_gap_intervals:
                    raise CandleFreshnessError("candle sequence has a missing or invalid timeframe interval")
        previous = ts

        values = (getattr(candle, "o", None), getattr(candle, "h", None), getattr(candle, "l", None),
                  getattr(candle, "c", None), getattr(candle, "v", None))
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise CandleFreshnessError(f"candle {index} contains a non-finite or invalid OHLCV value")
        o, h, low, close, volume = values
        if min(o, h, low, close) <= 0 or volume < 0 or h < max(o, low, close) or low > min(o, h, close):
            raise CandleFreshnessError(f"candle {index} has invalid OHLCV bounds")

    validate_latest_candle_timestamp(previous, timeframe, now, policy=p)


def validate_latest_candle_timestamp(timestamp: datetime, timeframe: str, now: datetime, *,
                                     policy: CandleFreshnessPolicy | None = None) -> None:
    """Validate the actual open timestamp of a known closed candle."""
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise CandleFreshnessError("newest candle timestamp is missing or timezone-naive")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise CandleFreshnessError("validation clock must return a timezone-aware datetime")
    p = policy or CandleFreshnessPolicy.from_env()
    interval = seconds(timeframe)
    step = timedelta(seconds=interval)
    timestamp = timestamp.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if timestamp.timestamp() % interval != 0:
        raise CandleFreshnessError(f"newest candle timestamp is not aligned to {timeframe}")
    if timestamp > now + timedelta(seconds=p.clock_skew_tolerance_seconds):
        raise CandleFreshnessError("newest candle timestamp is future-dated beyond clock-skew tolerance")
    newest_close = timestamp + step
    if newest_close > now:
        raise CandleFreshnessError("newest candle is not closed")
    age = now - newest_close
    if age > step * p.max_age_intervals:
        raise CandleFreshnessError(
            f"newest closed candle is stale ({age.total_seconds():.0f}s old; "
            f"maximum is {interval * p.max_age_intervals}s)"
        )
