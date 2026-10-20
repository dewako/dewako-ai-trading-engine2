"""Phase 3: indicators, strategies, version lock, signal engine, switching and selection."""
import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import scenarios as S

from dewako.core.types import Candle, MarketState, Side, Signal
from dewako.strategies import base, indicators as I, library  # noqa: F401
from dewako.strategies.base import Strategy, StrategyParameterError
from dewako.strategies.selector import CONFIDENCE_NOTE, StrategyMode, StrategySelector, recommend
from dewako.strategies.signal_engine import SignalEngine, validate_signal
from dewako.strategies.switching import PolicyRequired, PositionPolicy, StrategySwitcher, SwitchBlocked
from dewako.strategies.trend_ai import TrendAI
from dewako.strategies.trend_ai_v22 import TrendAIv22
from dewako.strategies.versions import StrategyVersionConflict, StrategyVersionLock, fingerprint


def sig_of(name, cs, vr=2.0, **params):
    return base.latest(name)(**params).generate_signal(S.state(cs, volume_ratio=vr))


# ---------------------------------------------------------------- indicators
def test_adx_strong_trend_vs_sideways_vs_short():
    assert I.adx(S.steady_uptrend())[0] > 50
    assert I.adx(S.sideways())[0] < 20
    assert I.adx(S.steady_uptrend()[:20]) is None


def test_bollinger_macd_roc_vwap_donchian():
    mid, up, lo = I.bollinger([100.0] * 20)
    assert mid == up == lo == 100.0
    assert abs(I.roc([100, 101, 102, 110], 3) - 0.10) < 1e-12
    line, sig, hist = I.macd_series([100 + i for i in range(60)])
    assert line[-1] > 0 and abs(hist[-1] - (line[-1] - sig[-1])) < 1e-12
    cs = S.candles_from([100 + i for i in range(30)])
    hi, lo = I.donchian(cs, 10)
    assert hi == max(c.h for c in cs[-11:-1]) and hi < cs[-1].h  # the last candle is excluded from its own range
    flat = [c.model_copy(update={"v": 1.0}) for c in cs]
    assert I.vwap(flat, 5) == sum((c.h + c.l + c.c) / 3 for c in flat[-5:]) / 5


def test_zscore_of_constant_series_is_zero():
    assert I.zscore([5.0] * 10, 5) == 0.0


# ---------------------------------------------------------------- parameters, registry, versions
def test_parameter_ranges_enforced():
    base.latest("Momentum AI")()  # defaults are valid
    for bad in ({"vol_mult": 0.1}, {"atr_stop": 99}, {"rsi_len": float("nan")}, {"macd_fast": 40, "macd_slow": 30}):
        with pytest.raises(StrategyParameterError):
            base.latest("Momentum AI")(**bad)
    with pytest.raises(StrategyParameterError):
        base.latest("Breakout AI")(lookback=True)  # a bool is not a number
    with pytest.raises(StrategyParameterError):
        base.latest("Breakout AI")(unknown=1)


def test_registry_versions_and_latest():
    assert base.versions("Trend AI") == ["2.1", "2.2"]
    assert base.latest("Trend AI") is TrendAIv22
    assert base.get("Trend AI", "2.1") is TrendAI
    assert {"Momentum AI", "Mean Reversion", "Breakout AI", "Trend AI", "AI Strategy"} <= set(base.names())


def test_old_version_behaviour_is_untouched_by_new_version():
    cs = S.steady_uptrend()
    old = TrendAI().generate_signal(S.state(cs))
    assert old and old.strategy_version == "2.1"  # v2.1 still only needs EMA + volume
    new = TrendAIv22().generate_signal(S.state(cs))
    assert new and new.strategy_version == "2.2"


def test_version_lock_records_then_detects_change():
    path = os.path.join(tempfile.mkdtemp(), "versions.lock.json")
    lock = StrategyVersionLock(path)
    assert "Trend AI v2.1" in lock.verify([TrendAI, TrendAIv22])
    assert lock.verify([TrendAI, TrendAIv22]) == []  # unchanged: nothing new, no error

    class Tweaked(TrendAI):  # same name and version, different default parameters
        default_parameters = {**TrendAI.default_parameters, "atr_stop": 5.0}

    assert fingerprint(Tweaked) != fingerprint(TrendAI)
    with pytest.raises(StrategyVersionConflict):
        lock.verify([Tweaked])
    assert "Trend AI v2.1" in json.load(open(path))


# ---------------------------------------------------------------- strategies
def test_trend_ai_v22_long_short_and_filters():
    up = sig_of("Trend AI", S.steady_uptrend())
    assert up.side is Side.BUY and up.stop_price < up.entry_price and any("ADX" in r for r in up.reason)
    assert sig_of("Trend AI", S.steady_downtrend()).side is Side.SELL
    assert sig_of("Trend AI", S.sideways()) is None  # no trend strength
    assert sig_of("Trend AI", S.steady_uptrend(), min_volume_ratio=3.0) is None  # volume filter (state ratio is 2.0)


def test_momentum_ai_buys_accelerating_strength_and_declines_stretched_moves():
    s = sig_of("Momentum AI", S.momentum_uptrend())
    assert s and s.side is Side.BUY and any("MACD" in r for r in s.reason) and s.stop_price < s.entry_price
    assert sig_of("Momentum AI", S.overstretched_uptrend()) is None  # RSI pinned near 100
    assert sig_of("Momentum AI", S.momentum_uptrend(), vr=1.0) is None  # no volume confirmation


def test_mean_reversion_buys_dip_with_target_at_the_mean_and_sells_spike():
    s = sig_of("Mean Reversion", S.range_then_drop())
    assert s and s.side is Side.BUY and s.take_profit_price > s.entry_price > s.stop_price
    t = sig_of("Mean Reversion", S.range_then_spike())
    assert t and t.side is Side.SELL and t.take_profit_price < t.entry_price < t.stop_price


def test_mean_reversion_stands_aside_when_market_is_trending():
    assert sig_of("Mean Reversion", S.range_then_drop(drop_bars=4, drop=0.008)) is None  # sustained sell-off: ADX high
    assert sig_of("Mean Reversion", S.steady_downtrend()) is None


def test_breakout_ai_both_directions_and_confirmations():
    up = sig_of("Breakout AI", S.quiet_range_then_breakout(up=True))
    dn = sig_of("Breakout AI", S.quiet_range_then_breakout(up=False))
    assert up.side is Side.BUY and up.take_profit_price > up.entry_price > up.stop_price
    assert dn.side is Side.SELL and dn.take_profit_price < dn.entry_price < dn.stop_price
    assert sig_of("Breakout AI", S.quiet_range_then_breakout(), vr=1.0) is None  # no volume
    assert sig_of("Breakout AI", S.quiet_range_then_breakout(), atr_expansion=5.0) is None  # volatility not expanding enough
    assert sig_of("Breakout AI", S.sideways()) is None  # price still inside the range


def test_strategies_are_deterministic_and_need_enough_history():
    cs = S.steady_uptrend()
    a, b = sig_of("Trend AI", cs), sig_of("Trend AI", cs)
    assert a.model_dump(exclude={"timestamp"}) == b.model_dump(exclude={"timestamp"})
    for name in ("Trend AI", "Momentum AI", "Mean Reversion", "Breakout AI"):
        assert sig_of(name, cs[:15]) is None


def test_ai_strategy_uses_market_state_and_is_experimental():
    cs = S.steady_uptrend()
    st = lambda **kw: S.state(cs, volume_ratio=1.5, trend="BULLISH", regime="TRENDING", momentum="STRONG", volatility="MEDIUM", **kw)
    strat = base.latest("AI Strategy")()
    assert strat.experimental and strat.generate_signal(st()).side is Side.BUY
    assert strat.generate_signal(st(upcoming_high_impact_event_minutes=10)) is None  # event too close
    assert strat.generate_signal(S.state(cs, volume_ratio=1.5, trend="BULLISH", regime="TRENDING", momentum="STRONG", volatility="HIGH")) is None
    assert strat.generate_signal(S.state(cs, volume_ratio=1.5, trend="BULLISH", regime="RANGING", momentum="STRONG")) is None


# ---------------------------------------------------------------- signal engine
def test_engine_stamps_deterministic_id_and_time_and_flags_duplicates():
    eng, state = SignalEngine(TrendAIv22()), S.state(S.steady_uptrend())
    r = eng.evaluate(state)
    assert r.signal and r.signal.timestamp == state.as_of and len(r.signal.signal_id) == 16
    again = eng.evaluate(state)
    assert again.signal is None and again.duplicate
    other = SignalEngine(TrendAIv22()).evaluate(state)
    assert other.signal.signal_id == r.signal.signal_id  # same bar, same id, even from a fresh engine


class _Bad(Strategy):
    name, version, default_parameters = "Bad", "1.0", {}
    mode = "stop"

    def generate_signal(self, state):
        px = state.candles[-1].c
        kw = dict(symbol=state.symbol, side=Side.BUY, confidence=0.8, strategy="Bad", strategy_version="1.0", reason=["x"],
                  entry_price=px, stop_price=px * 0.99)
        if self.mode == "stop":
            kw["stop_price"] = px * 1.01
        elif self.mode == "symbol":
            kw["symbol"] = "ETH/USDT"
        elif self.mode == "version":
            kw["strategy_version"] = "9.9"
        elif self.mode == "nan":
            kw["entry_price"] = float("nan")
        elif self.mode == "tp":
            kw["take_profit_price"] = px * 0.9
        elif self.mode == "reasons":
            kw["reason"] = []
        elif self.mode == "boom":
            raise RuntimeError("secret internal detail")
        return Signal(**kw)


def test_engine_rejects_invalid_signals_and_never_leaks_exceptions():
    state = S.state(S.steady_uptrend())
    for mode in ("stop", "symbol", "version", "nan", "tp", "reasons", "boom"):
        strat = _Bad()
        strat.mode = mode
        eng = SignalEngine(strat)
        r = eng.evaluate(state)
        assert r.signal is None and eng.rejections, mode
    strat = _Bad()
    strat.mode = "boom"
    assert "secret" not in SignalEngine(strat).evaluate(state).reason  # only the exception type is reported
    strat.mode = "ok"
    assert SignalEngine(strat).evaluate(state).signal is not None


def test_engine_with_open_position_only_manages_never_enters():
    eng = SignalEngine(TrendAIv22())
    r = eng.evaluate(S.state(S.steady_uptrend()), position={"symbol": "BTC/USDT", "side": "LONG"})
    assert r.signal is None and r.kind == "MANAGE"


# ---------------------------------------------------------------- switching
def test_switch_requires_policy_when_position_open_or_unknown():
    sw = StrategySwitcher(has_position=lambda bot: True)
    sw.assign("b1", "Trend AI", "2.1")  # first assignment: nothing to disturb
    with pytest.raises(PolicyRequired):
        sw.assign("b1", "Mean Reversion")
    assert sw.bindings["b1"].strategy == "Trend AI"  # unchanged after the refusal
    unknown = StrategySwitcher(has_position=None)
    unknown.assign("b1", "Trend AI", "2.1")
    with pytest.raises(PolicyRequired):  # unknown position state is treated as "position open"
        unknown.assign("b1", "Mean Reversion")


def test_switch_policies():
    sw = StrategySwitcher(has_position=lambda bot: True)
    sw.assign("keep", "Trend AI", "2.1")
    r = sw.assign("keep", "Momentum AI", policy=PositionPolicy.KEEP)
    assert r.actions == ["NEW_STRATEGY_MANAGES_POSITION"] and sw.managing_strategy("keep") == ("Momentum AI", "1.4")
    sw.assign("close", "Trend AI", "2.1")
    r = sw.assign("close", "Breakout AI", policy=PositionPolicy.CLOSE)
    assert r.actions == ["REQUEST_CLOSE"] and r.binding.close_requested and r.binding.entries_blocked
    sw.assign("prev", "Trend AI", "2.1")
    r = sw.assign("prev", "Mean Reversion", policy=PositionPolicy.PREVIOUS_MANAGES)
    assert sw.managing_strategy("prev") == ("Trend AI", "2.1") and r.binding.entries_blocked
    sw.position_closed("prev")
    assert sw.managing_strategy("prev") == ("Mean Reversion", "1.2") and not sw.bindings["prev"].entries_blocked
    assert any(e["event"] == "strategy_switch" for e in sw.audit)


def test_switch_without_position_needs_no_policy():
    sw = StrategySwitcher(has_position=lambda bot: False)
    sw.assign("b", "Trend AI", "2.1")
    assert sw.assign("b", "Mean Reversion").binding.strategy == "Mean Reversion"


def test_switch_guards_experimental_live_and_ai_actor():
    sw = StrategySwitcher(has_position=lambda bot: False)
    with pytest.raises(SwitchBlocked):
        sw.assign("b", "AI Strategy", mode="LIVE")
    sw.assign("live", "Trend AI", "2.2", mode="LIVE")
    with pytest.raises(SwitchBlocked):
        sw.assign("live", "Mean Reversion", mode="LIVE", actor="ai")
    with pytest.raises(SwitchBlocked):
        sw.assign("live", "Mean Reversion", mode="PAPER", actor="selector")
    with pytest.raises(SwitchBlocked):
        sw.assign("b", "Nope")
    assert sw.assign("live", "Mean Reversion", mode="LIVE", actor="admin").binding.mode == "LIVE"


# ---------------------------------------------------------------- selector
def ms(**kw):
    d = dict(symbol="BTC/USDT", as_of=S.T0, candles=[], trend="BULLISH", momentum="MODERATE", volatility="MEDIUM",
             volume_ratio=1.3, regime="TRENDING")
    d.update(kw)
    return MarketState(**d)


def test_recommendations_map_regimes_and_explain_themselves():
    r = recommend(ms())
    assert (r.action, r.strategy, r.version) == ("USE", "Trend AI", "2.2") and r.reasons and r.risks and r.confidence_note == CONFIDENCE_NOTE
    assert recommend(ms(momentum="STRONG", volume_ratio=2.0)).strategy == "Momentum AI"
    assert recommend(ms(regime="RANGING", trend="NEUTRAL")).strategy == "Mean Reversion"
    low = recommend(ms(regime="RANGING", trend="NEUTRAL", volatility="LOW"))
    assert low.strategy == "Breakout AI" and any("fail" in x for x in low.risks)
    assert recommend(ms(regime="VOLATILE", trend="NEUTRAL")).action == "WAIT"
    ev = recommend(ms(upcoming_high_impact_event_minutes=10))
    assert ev.action == "WAIT" and any("event" in x.lower() for x in ev.risks)
    assert all(0 <= recommend(ms(momentum=m)).confidence <= 0.9 for m in ("WEAK", "MODERATE", "STRONG"))


def test_recommendation_never_suggests_experimental_strategy():
    for kw in ({}, {"regime": "RANGING", "trend": "NEUTRAL"}, {"momentum": "STRONG", "volume_ratio": 3.0}):
        assert recommend(ms(**kw)).strategy != "AI Strategy"


def test_manual_mode_never_changes_anything():
    d = StrategySelector(StrategyMode.MANUAL).decide(ms(), ("Mean Reversion", "1.2"), has_position=False)
    assert d.action == "KEEP_CURRENT" and d.recommendation is None


def test_ai_assisted_only_proposes():
    d = StrategySelector(StrategyMode.AI_ASSISTED).decide(ms(), ("Mean Reversion", "1.2"), has_position=False)
    assert d.action == "PROPOSE" and d.recommendation.strategy == "Trend AI"
    same = StrategySelector(StrategyMode.AI_ASSISTED).decide(ms(), ("Trend AI", "2.2"), has_position=False)
    assert same.action == "KEEP_CURRENT"


def test_fully_automated_guards():
    cur = ("Mean Reversion", "1.2")
    auto = lambda: StrategySelector(StrategyMode.FULLY_AUTOMATED)
    assert auto().decide(ms(volume_ratio=1.3, momentum="STRONG"), cur, False).action in ("APPLY", "PROPOSE")
    ok = auto().decide(ms(momentum="STRONG", volume_ratio=1.3), cur, False)  # Trend AI, support 4 -> confidence 0.85
    assert ok.action == "APPLY"
    assert auto().decide(ms(momentum="STRONG"), cur, True).action == "PROPOSE"  # position open
    assert auto().decide(ms(momentum="STRONG"), cur, None).action == "PROPOSE"  # state unknown
    assert auto().decide(ms(momentum="STRONG"), cur, False, bot_mode="LIVE").action == "PROPOSE"  # never auto on live
    assert auto().decide(ms(momentum="WEAK", volume_ratio=1.0), cur, False).action == "PROPOSE"  # confidence too low
    sel = auto()
    assert sel.decide(ms(momentum="STRONG"), cur, False).action == "APPLY"
    later = sel.decide(ms(momentum="STRONG", as_of=S.T0 + timedelta(hours=1)), cur, False)
    assert later.action == "PROPOSE" and "recent" in later.explanation  # cooldown stops flip-flopping
    again = sel.decide(ms(momentum="STRONG", as_of=S.T0 + timedelta(hours=5)), cur, False)
    assert again.action == "APPLY"


def test_selector_has_no_route_to_risk_or_orders():
    import dewako.strategies.selector as mod
    import ast
    tree = ast.parse(open(mod.__file__).read())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    for forbidden in ("risk", "execution", "exchanges", "services", "paper_trading"):
        assert not any(f"dewako.{forbidden}" in m for m in imported), forbidden
