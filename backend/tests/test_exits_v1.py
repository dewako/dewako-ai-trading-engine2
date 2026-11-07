"""Trend AI 2.3, Momentum AI 1.5 and AI Strategy 0.4: the originals could only end at their stop; the new versions add the opposite-of-entry exit."""
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import dewako.strategies.library  # noqa: E402,F401
from dewako.core.types import Candle, MarketState  # noqa: E402
from dewako.market_data.state_builder import build_market_state  # noqa: E402
from dewako.strategies import base, versions  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
H1 = timedelta(hours=1)
PAIRS = [("Trend AI", "2.2", "2.3"), ("Momentum AI", "1.4", "1.5"), ("AI Strategy", "0.3", "0.4")]


def candles(prices):
    return [Candle(t=T0 + H1 * i, o=p, h=p * 1.002, l=p * 0.998, c=p, v=1000.0) for i, p in enumerate(prices)]


def up_then_down(n_up=120, n_down=60):
    ps = [100.0 * (1.004 ** i) for i in range(n_up)]
    ps += [ps[-1] * (0.994 ** i) for i in range(1, n_down + 1)]
    return ps


def test_the_originals_are_untouched_and_have_no_exit_the_new_versions_do():
    for name, old, new in PAIRS:
        o, n = base.get(name, old), base.get(name, new)
        assert o.manage_position is base.Strategy.manage_position, f"{name} {old} must stay exit-less (backtest evidence refers to it)"
        assert "manage_position" in vars(n)
        assert (n.default_parameters, n.parameter_ranges, n.supported_timeframes) == (o.default_parameters, o.parameter_ranges, o.supported_timeframes)
        assert n.generate_signal is o.generate_signal                        # the ENTRY logic is the very same function


def test_trend_exit_fires_only_after_the_fast_ema_drops_below_the_slow():
    s = base.get("Trend AI", "2.3")()
    holding = {"quantity": 1.0}
    rising = build_market_state("BTC/USDT", candles(up_then_down(120, 0)), T0 + H1 * 121, "1h")
    assert s.manage_position(rising, holding) is None
    falling = build_market_state("BTC/USDT", candles(up_then_down(120, 60)), T0 + H1 * 181, "1h")
    sig = s.manage_position(falling, holding)
    assert sig is not None and sig.side.value == "SELL" and sig.entry_price == falling.candles[-1].c and sig.stop_price is None
    assert "fast EMA below slow EMA" in sig.reason[0]


def test_momentum_exit_fires_when_the_macd_histogram_is_negative_and_not_while_positive():
    s = base.get("Momentum AI", "1.5")()
    rising = build_market_state("BTC/USDT", candles(up_then_down(120, 0)), T0 + H1 * 121, "1h")
    falling = build_market_state("BTC/USDT", candles(up_then_down(120, 40)), T0 + H1 * 161, "1h")
    assert s.manage_position(rising, {}) is None
    sig = s.manage_position(falling, {})
    assert sig is not None and sig.side.value == "SELL" and "histogram negative" in sig.reason[0]


def test_ai_strategy_exit_follows_the_state_trend_label():
    s = base.get("AI Strategy", "0.4")()
    cs = candles(up_then_down(100, 0))
    for trend, exits in (("BULLISH", False), ("BEARISH", True), ("NEUTRAL", True)):
        st = MarketState(symbol="BTC/USDT", as_of=T0 + H1 * 101, candles=cs, trend=trend)
        assert (s.manage_position(st, {}) is not None) is exits, trend


def test_too_little_history_never_produces_an_exit():
    for name, _o, new in PAIRS[:2]:
        s = base.get(name, new)()
        st = MarketState(symbol="BTC/USDT", as_of=T0 + H1 * 6, candles=candles([100.0 - i for i in range(5)]))
        assert s.manage_position(st, {}) is None


def test_a_replay_ends_trades_on_the_strategy_exit_and_can_book_a_profit():
    from test_stage3a_replay import M5, SYM, TF, T0 as R0, ReplayConfig, dataset, replay
    random.seed(5)
    cs, p = [], 100.0
    for i in range(900):
        p *= 1 + (0.0012 if (i // 150) % 2 == 0 else -0.0012) + random.gauss(0, 0.003)
        cs.append(Candle(t=R0 + M5 * i, o=p, h=p * 1.004, l=p * 0.996, c=p * 1.001, v=1000.0 * (1 + abs(math.sin(i / 9)))))
    ds = dataset(cs)
    old = replay(ds, ReplayConfig(strategy="Trend AI", version="2.2", symbol=SYM, timeframe=TF, lookback=300))
    new = replay(ds, ReplayConfig(strategy="Trend AI", version="2.3", symbol=SYM, timeframe=TF, lookback=300))
    assert old.trades and all(t.exit_reason == "STOP" for t in old.trades) and all(t.pnl < 0 for t in old.trades)
    assert new.trades and any(t.exit_reason == "STRATEGY" for t in new.trades) and any(t.pnl > 0 for t in new.trades)


def test_a_version_built_on_another_changes_fingerprint_when_its_parent_changes():
    child = base.get("Trend AI", "2.3")
    before = versions.fingerprint(child)
    real = versions._source

    def edited(cls):
        return real(cls) + ("\n# parent edited" if cls is base.get("Trend AI", "2.2") else "")
    versions._source = edited
    try:
        assert versions.fingerprint(child) != before
    finally:
        versions._source = real
    assert versions.fingerprint(child) == before


def test_a_strategy_with_no_strategy_parent_hashes_exactly_as_before():
    import hashlib
    import inspect
    import json
    cls = base.get("Trend AI", "2.1")
    payload = json.dumps({"name": cls.name, "version": cls.version, "params": cls.default_parameters,
                          "ranges": {k: list(v) for k, v in cls.parameter_ranges.items()}, "body": inspect.getsource(cls)}, sort_keys=True, default=str)
    assert versions.fingerprint(cls) == hashlib.sha256(payload.encode()).hexdigest()


def test_research_versions_never_become_what_a_bare_strategy_name_resolves_to():
    for name, old, new in PAIRS:
        assert base.get(name, new).candidate is True and base.get(name, old).candidate is False
        assert base.latest(name) is base.get(name, old), f"{name}: an unproven newer version must not become the default"
        assert new in base.versions(name) and base.get(name, new)().describe()["candidate"] is True


def test_when_every_version_is_a_candidate_there_is_no_default():
    import pytest

    class OnlyCandidate(base.Strategy):
        name, version, candidate = "Only Candidate", "0.1", True

        def generate_signal(self, state):
            return None
    base.register(OnlyCandidate)
    with pytest.raises(KeyError):
        base.latest("Only Candidate")
