"""Range-based costs are scaled to a 5-minute reference so that slower timeframes are not charged more for the same market."""
import math
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.replay import ReplayConfig  # noqa: E402
from dewako.backtesting.sim_adapter import SimConfig, SimulatedExchange  # noqa: E402
from dewako.backtesting.validation import scale_costs  # noqa: E402
from dewako.core.types import Candle  # noqa: E402

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR = Candle(t=T, o=100.0, h=103.0, l=100.0, c=102.0, v=1000.0)             # range about 2.94% of the close


def half(cfg):
    return SimulatedExchange(lambda: T, balance=1000.0, config=cfg).half_spread(BAR)


def test_with_no_bar_length_the_legacy_spread_is_unchanged():
    legacy = SimConfig()
    assert half(legacy) == pytest.approx(0.05 * 3.0 / 102.0)


def test_a_longer_bar_gets_the_range_scaled_down_by_the_square_root_of_the_length_ratio():
    base = half(SimConfig(bar_seconds=300.0))
    assert half(SimConfig(bar_seconds=3600.0)) == pytest.approx(base * math.sqrt(300 / 3600))
    assert half(SimConfig(bar_seconds=14400.0)) == pytest.approx(base * math.sqrt(300 / 14400))


def test_at_the_five_minute_reference_nothing_changes():
    assert half(SimConfig(bar_seconds=300.0)) == half(SimConfig())


def test_the_floor_spread_still_applies_when_the_scaled_range_is_tiny():
    assert half(SimConfig(bar_seconds=14400.0, spread_range_factor=0.0001)) == SimConfig().min_half_spread


def test_bad_bar_lengths_are_refused():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            SimConfig(bar_seconds=bad)
    with pytest.raises(ValueError):
        SimConfig(range_reference_seconds=0.0)


def test_a_replay_takes_the_bar_length_from_its_timeframe_and_records_it():
    for tf, secs in (("5m", 300.0), ("1h", 3600.0), ("4h", 14400.0)):
        c = ReplayConfig(strategy="x", version="1", symbol="BTC/USDT", timeframe=tf)
        assert c.effective_sim().bar_seconds == secs and c.record()["sim"]["bar_seconds"] == secs
    explicit = ReplayConfig(strategy="x", version="1", symbol="BTC/USDT", timeframe="1h", sim=SimConfig(bar_seconds=300.0))
    assert explicit.effective_sim().bar_seconds == 300.0                            # an explicit choice is honoured and recorded


def test_the_cost_stress_scales_the_cost_factors_but_keeps_the_bar_length():
    s = scale_costs(SimConfig(bar_seconds=3600.0), 2.0)
    assert s.bar_seconds == 3600.0 and s.spread_range_factor == pytest.approx(0.10) and s.fee_rate == pytest.approx(0.002)
