"""Core behaviour tests. Plain asserts; async code driven with asyncio.run (no plugins needed)."""
import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest

from dewako.backtesting.engine import BacktestConfig, overfitting_warnings, run
from dewako.core import symbols
from dewako.core.types import Candle, MarketState, OrderStatus, Side, Signal
from dewako.exchanges.base import ExchangeError, NotConnected, OrderOutcomeUnknown
from dewako.exchanges.binance import BinanceAdapter
from dewako.execution.order_engine import OrderEngine, OrderRejected
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.risk.engine import RiskContext, RiskEngine, RiskLimits
from dewako.strategies.base import StrategyParameterError
from dewako.strategies.trend_ai import TrendAI


def candles(n=160, start=100.0, step=0.4, vol=1.0):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out, p = [], start
    for i in range(n):
        o, c = p, p + step + (0.3 if i % 5 == 0 else -0.1)
        out.append(Candle(t=t0 + timedelta(minutes=15 * i), o=o, h=max(o, c) + 0.2, l=min(o, c) - 0.2, c=c, v=vol * (2 if i % 7 == 0 else 1)))
        p = c
    return out


def sig(**kw):
    base = dict(symbol="BTC/USDT", side=Side.BUY, confidence=0.82, strategy="Trend AI", strategy_version="2.1",
                entry_price=100.0, stop_price=98.0, reason=["EMA crossover"])
    base.update(kw)
    return Signal(**base)


# ---- symbols
def test_symbol_roundtrip():
    assert symbols.normalize("BTCUSDT", "binance") == "BTC/USDT"
    assert symbols.normalize("BTCUSDT", "bybit") == "BTC/USDT"
    assert symbols.normalize("BTC-USDT", "okx") == "BTC/USDT"
    assert symbols.to_native("BTC/USDT", "okx") == "BTC-USDT"
    assert symbols.to_native("BTC/USDT", "binance") == "BTCUSDT"


def test_symbol_invalid():
    with pytest.raises(ValueError):
        symbols.to_native("BTCUSDT", "binance")


# ---- strategy
def test_strategy_signal_shape_and_no_execution():
    st = TrendAI()
    state = MarketState(symbol="BTC/USDT", as_of=datetime.now(timezone.utc), candles=candles(), volume_ratio=1.8)
    s = st.generate_signal(state)
    assert s is not None and s.side is Side.BUY and s.stop_price < s.entry_price
    assert s.strategy == "Trend AI" and s.strategy_version == "2.1" and s.reason


def test_strategy_parameter_validation():
    with pytest.raises(StrategyParameterError):
        TrendAI(ema_fast=60, ema_slow=50)
    with pytest.raises(StrategyParameterError):
        TrendAI(nonsense=1)


def test_strategy_needs_volume_confirmation():
    state = MarketState(symbol="BTC/USDT", as_of=datetime.now(timezone.utc), candles=candles(), volume_ratio=0.5)
    assert TrendAI().generate_signal(state) is None


# ---- risk
def ctx(**kw):
    return RiskContext(equity=10_000, **kw)


def test_position_sizing_risks_configured_percent():
    eng = RiskEngine()
    qty = eng.size_position(entry=100, stop=90, equity=10_000)
    assert math.isclose(qty * 10, 10_000 * 0.005)  # losing 10 per unit at the stop = 0.5% of equity


def test_position_sizing_tight_stop_hits_position_cap_not_risk():
    qty = RiskEngine().size_position(entry=100, stop=98, equity=10_000)
    assert math.isclose(qty * 100, 10_000 * 0.10)  # 25 units by risk, but capped at 10% of equity


def test_position_size_capped_by_max_position():
    eng = RiskEngine(RiskLimits(risk_per_trade_pct=5, max_position_pct=10))
    qty = eng.size_position(entry=100, stop=99.9, equity=10_000)
    assert qty * 100 <= 10_000 * 0.10 + 1e-9


def test_risk_requires_stop_loss():
    d = RiskEngine().evaluate(sig(stop_price=None), ctx())
    assert not d.approved and any("stop loss" in r for r in d.reasons)


def test_risk_blocks_on_limits():
    eng = RiskEngine()
    assert not eng.evaluate(sig(), ctx(daily_pnl_pct=-3.0)).approved
    assert not eng.evaluate(sig(), ctx(drawdown_pct=10.0)).approved
    assert not eng.evaluate(sig(), ctx(open_positions=5)).approved
    assert not eng.evaluate(sig(), ctx(exposure_pct=70)).approved
    assert not eng.evaluate(sig(), ctx(consecutive_losses=4)).approved
    assert not eng.evaluate(sig(), ctx(minutes_to_high_impact_event=5)).approved
    assert not eng.evaluate(sig(), ctx(correlated_risk_pct=7.8)).approved
    assert not eng.evaluate(sig(), ctx(), leverage=10).approved


def test_risk_approves_clean_signal():
    d = RiskEngine().evaluate(sig(), ctx())
    assert d.approved and d.quantity > 0


def test_cooldown_after_loss():
    now = datetime.now(timezone.utc)
    d = RiskEngine().evaluate(sig(), ctx(last_loss_at=now - timedelta(minutes=5), now=now))
    assert not d.approved and any("cooldown" in r for r in d.reasons)


def test_ai_cannot_change_limits_or_release_estop():
    eng = RiskEngine()
    for actor in ("ai", "strategy", "bot"):
        with pytest.raises(PermissionError):
            eng.update_limits(actor, RiskLimits(max_leverage=100))
    assert eng.limits.max_leverage == 5.0
    eng.emergency_stop("ai", "test")  # anyone may stop
    with pytest.raises(PermissionError):
        eng.release_emergency_stop("ai")
    assert eng.halted and not eng.evaluate(sig(), ctx()).approved
    eng.release_emergency_stop("admin")
    assert not eng.halted


def test_limits_are_immutable():
    with pytest.raises(Exception):
        RiskEngine().limits.max_leverage = 50


# ---- exchange / execution
def test_stub_adapters_refuse_to_trade():
    with pytest.raises(NotConnected):
        asyncio.run(BinanceAdapter().get_balance())


def _paper():
    a = PaperAdapter(balance=10_000)
    a.set_price("BTC/USDT", 100.0)
    return a


def test_order_engine_rejects_unapproved_decision():
    eng = RiskEngine()
    d = eng.evaluate(sig(stop_price=None), ctx())
    with pytest.raises(OrderRejected):
        asyncio.run(OrderEngine(_paper()).submit(d))


def test_order_engine_fills_and_deduplicates():
    d = RiskEngine().evaluate(sig(), ctx())
    paper = _paper()
    oe = OrderEngine(paper, bot_id="001")

    async def go():
        a = await oe.submit(d)
        b = await oe.submit(d)  # duplicate delivery
        return a, b

    a, b = asyncio.run(go())
    assert a.status is OrderStatus.FILLED and a.client_order_id == b.client_order_id
    assert len(paper.orders) == 1


def test_timeout_reconciles_instead_of_resubmitting():
    class Flaky(PaperAdapter):
        calls = 0

        async def create_order(self, req):
            Flaky.calls += 1
            await super().create_order(req)  # exchange actually filled it
            raise OrderOutcomeUnknown("timeout after send")

    paper = Flaky(balance=10_000)
    paper.set_price("BTC/USDT", 100.0)
    d = RiskEngine().evaluate(sig(), ctx())
    order = asyncio.run(OrderEngine(paper).submit(d))
    assert Flaky.calls == 1  # never resent
    assert order.status is OrderStatus.FILLED  # discovered through reconciliation


def test_unreconcilable_order_is_marked_unknown_not_retried():
    class Dead(PaperAdapter):
        async def create_order(self, req):
            raise OrderOutcomeUnknown("timeout")

        async def get_order(self, *a, **k):
            raise ExchangeError("down")

    paper = Dead()
    paper.set_price("BTC/USDT", 100.0)
    order = asyncio.run(OrderEngine(paper).submit(RiskEngine().evaluate(sig(), ctx())))
    assert order.status is OrderStatus.UNKNOWN


# ---- backtest
def test_backtest_is_reproducible_and_records_assumptions():
    cs = candles(300)
    cfg = BacktestConfig(symbol="BTC/USDT", timeframe="15m")
    r1 = run(TrendAI(min_volume_ratio=0.0), cs, cfg)
    r2 = run(TrendAI(min_volume_ratio=0.0), cs, cfg)
    assert r1.metrics == r2.metrics and r1.record["run_id"] == r2.record["run_id"]
    for key in ("strategy", "version", "parameters", "config", "data_start", "data_end"):
        assert key in r1.record
    assert r1.record["config"]["fee_rate"] == 0.001 and r1.record["config"]["slippage"] == 0.0005


def test_backtest_fees_and_slippage_reduce_profit():
    cs = candles(300)
    free = run(TrendAI(min_volume_ratio=0.0), cs, BacktestConfig("BTC/USDT", "15m", fee_rate=0, slippage=0))
    costly = run(TrendAI(min_volume_ratio=0.0), cs, BacktestConfig("BTC/USDT", "15m", fee_rate=0.002, slippage=0.002))
    assert free.metrics["trades"] > 0
    assert costly.metrics["net_profit"] < free.metrics["net_profit"]


def test_backtest_has_no_lookahead():
    """Changing candles AFTER bar k must not change any trade that was entered at or before bar k."""
    cs = candles(300)
    cfg = BacktestConfig("BTC/USDT", "15m")
    base = run(TrendAI(min_volume_ratio=0.0), cs, cfg)
    k = 200
    altered = cs[:k] + [c.model_copy(update={"o": c.o * 0.5, "h": c.h * 0.5, "l": c.l * 0.5, "c": c.c * 0.5}) for c in cs[k:]]
    alt = run(TrendAI(min_volume_ratio=0.0), altered, cfg)
    early = lambda r: [(t.entry_index, round(t.entry, 6)) for t in r.trades if t.entry_index < k - 1]
    assert early(base) == early(alt)


def test_overfitting_warnings():
    w = overfitting_warnings(n_params=9, trades=12, train_roi=40, validation_roi=5, fee_rate=0, slippage=0)
    assert len(w) == 4
    assert overfitting_warnings(3, 120, 20, 15, 0.001, 0.0005) == []
