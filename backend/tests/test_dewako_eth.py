"""Dewako ETH 1.0 (owner-approved addition after the 2026-11-08 gate run on ETHUSDT 4h). Rules checked on hand-built candles."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import dewako.strategies.library  # noqa: E402,F401
from dewako.core.types import Candle, MarketState, Side  # noqa: E402
from dewako.runtime.bot_service import BotError, BotService  # noqa: E402
from dewako.strategies import base  # noqa: E402

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _state(closes, symbol="ETH/USDT"):
    cs = [Candle(t=T0 + timedelta(hours=4 * i), o=c, h=c * 1.001, l=c * 0.999, c=c, v=1.0) for i, c in enumerate(closes)]
    return MarketState(symbol=symbol, as_of=cs[-1].t + timedelta(hours=4), candles=cs)


def _d():
    return base.get("Dewako ETH", "1.0")()


def test_dewako_eth_is_a_registered_long_only_eth_4h_candidate_that_never_becomes_a_default():
    cls = base.get("Dewako ETH", "1.0")
    assert cls.candidate is True and cls.supported_markets == ["ETH/USDT"] and cls.supported_timeframes == ["4h"]
    assert cls.describe(cls())["directions"] == ["LONG"]
    with pytest.raises(KeyError):
        base.latest("Dewako ETH")                     # a candidate is never what a bare name resolves to


def test_dewako_eth_buys_a_20_bar_breakout_only_above_the_200_day_average_and_with_full_history():
    up = [100 + i * 0.01 for i in range(1200)] + [140]
    s = _d().generate_signal(_state(up))
    assert s is not None and s.side is Side.BUY and s.entry_price == 140 and 0 < s.stop_price < 140
    assert _d().generate_signal(_state([200.0] * 1180 + [100.0] * 20 + [105.0])) is None       # breaks the 20-bar high (100.1), below the average (~197)
    assert _d().generate_signal(_state(up[-1000:])) is None                                       # fewer than 1,200 candles
    assert _d().generate_signal(_state(up[:-1] + [up[-2]])) is None                               # no breakout


def test_dewako_eth_exits_on_a_10_bar_low_or_a_lost_200_day_trend_and_otherwise_holds():
    pos = {"side": "BUY", "quantity": 1, "entry_price": 110}
    up = [100 + i * 0.01 for i in range(1200)]
    assert _d().manage_position(_state(up + [up[-1] + 0.01]), pos) is None
    s = _d().manage_position(_state(up + [up[-1] - 0.5]), pos)
    assert s.side is Side.SELL and "10-bar low" in s.reason[0] and s.entry_price == up[-1] - 0.5
    s = _d().manage_position(_state([200.0] * 1190 + [100.0] * 10 + [101.0]), pos)
    assert s.side is Side.SELL and "uptrend lost" in s.reason[0]


def test_a_dewako_eth_bot_cannot_be_created_for_btc_where_it_failed_the_gate():
    svc = BotService(None, {}, None, None)
    with pytest.raises(BotError, match="does not support BTC/USDT"):
        asyncio.run(svc.create(bot_id="d1", strategy="Dewako ETH", version="1.0", symbol="BTC/USDT", timeframe="4h",
                               environment="PAPER", exchange="paper"))
    with pytest.raises(BotError, match="does not support timeframe 1h"):
        asyncio.run(svc.create(bot_id="d1", strategy="Dewako ETH", version="1.0", symbol="ETH/USDT", timeframe="1h",
                               environment="PAPER", exchange="paper"))
