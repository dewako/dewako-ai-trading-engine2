"""Test fixtures for the promotion gate. They build run records through the REAL RunStore from hand-made metrics, so the gate's own
re-evaluation is what is being tested. Nothing here pretends to be a real backtest."""
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dewako.backtesting.metrics import compute_metrics
from dewako.backtesting.thresholds import Thresholds, evaluate
from dewako.backtesting.validation import StrategyValidation

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
EQ = [(T0 + timedelta(hours=k), 1000.0) for k in range(1, 5)]


def m(**kw):
    return dataclasses.replace(compute_metrics([], EQ, [], 1000.0), **kw)


GOOD = dict(trades=150, net_pnl=50.0, expectancy=1.0, max_drawdown_pct=0.10)


def validation(name, version, *, full=None, stress2=None, ins=None, oos=None, periods=None, holdout=0.25, th=None):
    th = th or Thresholds()
    full = full or m(**GOOD)
    stress = {1.0: full, 2.0: stress2 or m(net_pnl=10.0), 3.0: m(net_pnl=-5.0)}
    ins, oos = ins or m(expectancy=1.0), oos or m(expectancy=0.8)
    periods = periods or [m(net_pnl=5.0), m(net_pnl=3.0), m(net_pnl=-1.0), m(net_pnl=2.0)]
    checks = evaluate(full, stress, ins, oos, periods, None, th)
    return StrategyValidation(name, version, full, stress, ins, oos, periods, None, th, checks, [], holdout_fraction=holdout,
                              cost_factors=(1.0, 2.0, 3.0), variants_tried=1,
                              results={"costs_1x": SimpleNamespace(digest="d" * 64)})


def dataset(symbol="XAUUSD", timeframe="5m", days=400, h="a" * 64):
    start = T0
    rec = {"symbol": symbol, "timeframe": timeframe, "source": "test", "start": start.isoformat(),
           "end": (start + timedelta(days=days)).isoformat(), "candles": 1000, "content_hash": h}
    return SimpleNamespace(symbol=symbol, timeframe=timeframe, content_hash=h, record=lambda: rec)


def config(name, version, symbol="XAUUSD", timeframe="5m", state_source="market_state", fee_rate=0.001):
    return SimpleNamespace(strategy=name, version=version, symbol=symbol, timeframe=timeframe,
                           record=lambda: {"strategy": name, "state_source": state_source, "sim": {"fee_rate": fee_rate}})


def record_run(store, actor, name, version, fingerprint, **kw):
    days = kw.pop("days", 400)
    v = validation(name, version, **kw)
    return store.save(v, dataset(days=days), config(name, version), fingerprint, actor), v
