"""Regression coverage for candle freshness and executable-quote safety gates."""
import asyncio
from datetime import datetime, timedelta, timezone
import math

import pytest

import scenarios as S
from dewako.core.types import Candle, MarketState, OrderRequest, OrderType, Side
from dewako.execution.engine import ExecutionEngine
from dewako.execution.validation import QuoteGuardConfig, validate_executable_quote
from dewako.market_data.freshness import CandleFreshnessError, validate_candles
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.risk.engine import RiskEngine
from dewako.risk.state import RiskStateStore
from dewako.runtime.bot_runner import BotConfig, BotRunner
from dewako.safety.controller import SafetyController
from dewako.strategies import library  # noqa: F401
from dewako.strategies.base import Strategy
from dewako.strategies.signal_engine import SignalEngine
from hardening_helpers import TEST_QUOTE_GUARD, decision, tmp_db

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
QUOTE_CONFIG = QuoteGuardConfig(max_spread_pct=0.2, max_price_deviation_pct=0.2,
                                max_quote_age_seconds=10, clock_skew_tolerance_seconds=1)


def run(coro):
    return asyncio.run(coro)


def candles():
    return S.steady_uptrend()


def latest_close(candles_):
    return candles_[-1].t + S.STEP


def test_provider_success_with_stale_candles_is_blocked_before_intent_creation():
    db, target = tmp_db()
    safety = SafetyController(db)
    adapter = QuotePaper()
    adapter.set_price("BTC/USDT", 110)
    engine = ExecutionEngine(db, safety, {"paper": adapter}, quote_guard=TEST_QUOTE_GUARD)
    risk_state = RiskStateStore(db)
    risk_state.init_scope("PAPER", 10_000)
    data = candles()
    as_of = latest_close(data) + S.STEP * 3
    calls = []

    async def provider(symbol, timeframe):
        calls.append((symbol, timeframe))
        return MarketState(symbol=symbol, as_of=as_of, candles=data)

    runner = BotRunner(engine, RiskEngine(), risk_state, provider, clock=lambda: as_of)
    result = run(runner.tick(BotConfig("bot", "Trend AI", "2.2", "BTC/USDT", "15m", "PAPER", "paper")))
    with db.read() as conn:
        intents = conn.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0]
        signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert calls == [("BTC/USDT", "15m")]
    assert result.outcome == "BLOCKED" and "market data could not be validated" in result.detail
    assert intents == signals == adapter.create_calls == 0


def test_missing_candle_timestamp_fails_closed():
    malformed = Candle.model_construct(t=None, o=100, h=101, l=99, c=100, v=1)
    with pytest.raises(CandleFreshnessError, match="missing or timezone-naive"):
        validate_candles([malformed], "15m", NOW)


def test_future_dated_candle_fails_closed():
    future = Candle(t=NOW + timedelta(minutes=15), o=100, h=101, l=99, c=100, v=1)
    with pytest.raises(CandleFreshnessError, match="future-dated"):
        validate_candles([future], "15m", NOW)


def test_out_of_order_candle_sequence_fails_closed():
    rows = candles()
    now = latest_close(rows)
    with pytest.raises(CandleFreshnessError, match="out of order or duplicated"):
        validate_candles([rows[-1], rows[-2]], "15m", now)


def test_fresh_valid_closed_candle_sequence_is_accepted():
    rows = candles()
    validate_candles(rows, "15m", latest_close(rows) + timedelta(seconds=5))


def test_missing_market_data_fails_closed():
    with pytest.raises(CandleFreshnessError, match="no candles"):
        validate_candles([], "15m", NOW)


def test_freshness_validation_exception_blocks_tick_and_creates_no_intent(monkeypatch):
    import dewako.runtime.bot_runner as bot_runner

    db, _ = tmp_db()
    safety = SafetyController(db)
    adapter = QuotePaper()
    adapter.set_price("BTC/USDT", 110)
    engine = ExecutionEngine(db, safety, {"paper": adapter}, quote_guard=TEST_QUOTE_GUARD)
    risk_state = RiskStateStore(db)
    risk_state.init_scope("PAPER", 10_000)
    data = candles()
    now = latest_close(data)

    async def provider(symbol, timeframe):
        return MarketState(symbol=symbol, as_of=now, candles=data)

    def fail_validation(*args, **kwargs):
        raise RuntimeError("freshness validator failed")

    monkeypatch.setattr(bot_runner, "validate_candles", fail_validation)
    runner = BotRunner(engine, RiskEngine(), risk_state, provider, clock=lambda: now)
    result = run(runner.tick(BotConfig("bot", "Trend AI", "2.2", "BTC/USDT", "15m", "PAPER", "paper")))
    with db.read() as conn:
        intents = conn.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0]
    assert result.outcome == "BLOCKED" and "RuntimeError" in result.detail
    assert intents == adapter.create_calls == 0


def test_strategy_plugin_cannot_choose_a_favorable_reference_price():
    class ForgedEntry(Strategy):
        name, version = "Forged Entry", "1.0"

        def generate_signal(self, state):
            from dewako.core.types import Side, Signal
            return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name,
                          strategy_version=self.version, reason=["test"], entry_price=state.candles[-1].c * 1.1,
                          stop_price=state.candles[-1].c, take_profit_price=state.candles[-1].c * 1.2)

    data = candles()
    state = MarketState(symbol="BTC/USDT", as_of=latest_close(data), candles=data)
    result = SignalEngine(ForgedEntry()).evaluate(state)
    assert result.signal is None and "newest validated candle close" in result.reason


class QuotePaper(PaperAdapter):
    def __init__(self, quote_sequence=None):
        super().__init__(balance=10_000)
        self.quote_sequence = list(quote_sequence or [])
        self.quote_calls = 0
        self.create_calls = 0

    async def get_ticker(self, symbol):
        self.quote_calls += 1
        if self.quote_sequence:
            return self.quote_sequence.pop(0)
        return quote(100, 100, NOW)

    async def create_order(self, req):
        self.create_calls += 1
        return await super().create_order(req)


def quote(bid, ask, timestamp, last=100):
    return {"last": last, "bid": bid, "ask": ask, "timestamp": timestamp}


def execute_quote(quote_sequence, config=QUOTE_CONFIG, *, signal_id="quote"):
    db, _ = tmp_db()
    adapter = QuotePaper(quote_sequence)
    adapter.set_price("BTC/USDT", 100)
    engine = ExecutionEngine(db, SafetyController(db), {"paper": adapter}, quote_guard=config, clock=lambda: NOW)
    result = run(engine.execute(decision(signal_id), bot_id="bot", environment="PAPER", exchange="paper"))
    with db.read() as conn:
        intent_count = conn.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0]
        order_states = [row[0] for row in conn.execute("SELECT state FROM orders").fetchall()]
    return result, adapter, intent_count, order_states


def test_quote_within_configured_spread_and_deviation_executes():
    result, adapter, intents, _ = execute_quote([quote(99.96, 100.04, NOW)] * 2)
    assert result.outcome == "EXECUTED" and adapter.create_calls == 1 and intents == 1
    checked = validate_executable_quote(quote(99.96, 100.04, NOW), side="BUY", reference_price=100,
                                        now=NOW, config=QUOTE_CONFIG)
    assert checked.executable_price == 100.04
    assert checked.spread_pct == pytest.approx(0.08)
    assert checked.deviation_pct == pytest.approx(0.04)


@pytest.mark.parametrize(("side", "quote_side"), [(Side.BUY, "ask"), (Side.SELL, "bid")])
def test_paper_quote_matches_its_simulated_executable_price(side, quote_side):
    async def scenario():
        adapter = PaperAdapter(balance=1_000, slippage=0.001)
        adapter.set_price("BTC/USDT", 100)
        adapter.holdings["BTC/USDT"] = 1.0          # a SELL needs something to sell (the adapter refuses short sales)
        ticker = await adapter.get_ticker("BTC/USDT")
        request = OrderRequest(symbol="BTC/USDT", side=side, type=OrderType.MARKET, quantity=1,
                               client_order_id="paper-price")
        order = await adapter.create_order(request)
        return ticker, order

    ticker, order = run(scenario())
    assert order.price == ticker[quote_side]


def test_excessive_spread_creates_no_intent_or_submission():
    result, adapter, intents, _ = execute_quote([quote(99, 101, NOW)])
    assert result.outcome == "BLOCKED" and "spread" in result.detail
    assert adapter.create_calls == intents == 0


def test_excessive_price_deviation_creates_no_intent_or_submission():
    result, adapter, intents, _ = execute_quote([quote(101.96, 102.04, NOW)])
    assert result.outcome == "BLOCKED" and "price deviation" in result.detail
    assert adapter.create_calls == intents == 0


def test_stale_quote_creates_no_intent_or_submission():
    result, adapter, intents, _ = execute_quote([quote(99.96, 100.04, NOW - timedelta(seconds=11))])
    assert result.outcome == "BLOCKED" and "stale" in result.detail
    assert adapter.create_calls == intents == 0


def test_missing_quote_fields_create_no_intent_or_submission():
    result, adapter, intents, _ = execute_quote([{"bid": 99.96, "timestamp": NOW}])
    assert result.outcome == "BLOCKED" and "ask" in result.detail
    assert adapter.create_calls == intents == 0


@pytest.mark.parametrize("bid,ask", [(100.1, 100.0), (0.0, 100.0)])
def test_invalid_bid_ask_create_no_intent_or_submission(bid, ask):
    result, adapter, intents, _ = execute_quote([quote(bid, ask, NOW)])
    assert result.outcome == "BLOCKED"
    assert adapter.create_calls == intents == 0


@pytest.mark.parametrize("bid,ask", [(math.nan, 100.0), (99.0, math.inf)])
def test_non_finite_quote_values_create_no_intent_or_submission(bid, ask):
    result, adapter, intents, _ = execute_quote([quote(bid, ask, NOW)])
    assert result.outcome == "BLOCKED"
    assert adapter.create_calls == intents == 0


def test_quote_movement_before_submission_is_revalidated():
    result, adapter, intents, states = execute_quote(
        [quote(99.96, 100.04, NOW), quote(101.96, 102.04, NOW)], signal_id="moving"
    )
    assert result.outcome == "BLOCKED" and "revalidated" in result.detail
    assert adapter.quote_calls == 2 and adapter.create_calls == 0
    assert intents == 1 and states == ["FAILED"]


def test_missing_quote_guard_configuration_fails_closed():
    result, adapter, intents, _ = execute_quote([quote(99.96, 100.04, NOW)], config=None)
    assert result.outcome == "BLOCKED" and "configuration is missing" in result.detail
    assert adapter.create_calls == intents == 0


def test_quote_configuration_rejects_non_sensical_limits():
    with pytest.raises(ValueError, match="max_spread_pct"):
        QuoteGuardConfig(max_spread_pct=0, max_price_deviation_pct=1, max_quote_age_seconds=10)


def test_quote_configuration_requires_all_runtime_limits(monkeypatch):
    for key in ("DEWAKO_MAX_SPREAD_PCT", "DEWAKO_MAX_PRICE_DEVIATION_PCT", "DEWAKO_MAX_QUOTE_AGE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    assert QuoteGuardConfig.from_env() is None
    monkeypatch.setenv("DEWAKO_MAX_SPREAD_PCT", "0.25")
    with pytest.raises(ValueError, match="configuration is incomplete"):
        QuoteGuardConfig.from_env()
    monkeypatch.setenv("DEWAKO_MAX_PRICE_DEVIATION_PCT", "0.50")
    monkeypatch.setenv("DEWAKO_MAX_QUOTE_AGE_SECONDS", "5")
    config = QuoteGuardConfig.from_env()
    assert config == QuoteGuardConfig(0.25, 0.50, 5)
