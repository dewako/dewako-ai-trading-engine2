"""Phase 2 tests. Providers run against fake transports shaped like the venues' documented responses."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from dewako.core.types import Candle, DataQuality
from dewako.market_data.backfill import backfill
from dewako.market_data.binance_public import BinancePublicProvider
from dewako.market_data.bybit_public import BybitPublicProvider
from dewako.market_data.hub import AllProvidersFailed, MarketDataHub, divergences
from dewako.market_data.models import CandleRecord, OrderBookSnapshot
from dewako.market_data.okx_public import OKXPublicProvider
from dewako.market_data.provider import MarketDataProvider, ProviderError, ProviderRateLimited
from dewako.market_data.state_builder import InsufficientData, build_market_state
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import floor, from_ms, to_ms

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


def rec(i, c=100.0, source="binance", published=None, tf="15m"):
    t = T0 + STEP * i
    return CandleRecord(symbol="BTC/USDT", timeframe=tf, candle=Candle(t=t, o=c, h=c + 1, l=c - 1, c=c, v=10),
                        quality=DataQuality(source=source, data_timestamp=t, publication_timestamp=published or t + STEP,
                                            ingested_at=T0 + timedelta(days=30)))


def run(coro):
    return asyncio.run(coro)


# ---- timeframes
def test_floor_aligns_to_grid():
    assert floor(T0 + timedelta(minutes=37), "15m") == T0 + timedelta(minutes=30)
    with pytest.raises(ValueError):
        floor(T0, "7m")


# ---- store: point-in-time behaviour
def test_store_roundtrip_and_idempotent():
    s = CandleStore()
    assert s.put_candles([rec(0), rec(1)]).new == 2
    r = s.put_candles([rec(0), rec(1)])
    assert (r.new, r.unchanged, r.revised) == (0, 2, 0)
    got = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP * 2)
    assert [g.candle.t for g in got] == [T0, T0 + STEP]


def test_as_of_hides_candles_not_yet_closed():
    s = CandleStore()
    s.put_candles([rec(i) for i in range(4)])
    as_of = T0 + STEP * 2 + timedelta(minutes=1)  # candles 0 and 1 closed; candle 2 still forming
    got = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP * 4, as_of=as_of)
    assert [g.candle.t for g in got] == [T0, T0 + STEP]


def test_revision_not_visible_before_it_was_known():
    s = CandleStore()
    s.put_candles([rec(0, c=100)], now=T0)
    s.put_candles([rec(0, c=105)], now=T0 + timedelta(days=2))  # correction ingested two days later
    before = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP, as_of=T0 + timedelta(days=1))
    after = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP, as_of=T0 + timedelta(days=3))
    latest = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP)
    assert before[0].candle.c == 100 and before[0].quality.revision == 0
    assert after[0].candle.c == 105 and after[0].quality.revision == 1
    assert latest[0].candle.c == 105


def test_find_gaps_ignores_unclosed_and_reports_missing():
    s = CandleStore()
    s.put_candles([rec(0), rec(1), rec(3), rec(4)])
    gaps = s.find_gaps("BTC/USDT", "15m", "binance", T0, T0 + STEP * 6)
    assert gaps == [T0 + STEP * 2, T0 + STEP * 5]
    as_of = T0 + STEP * 5  # candle 5 has not closed yet at this moment, so it is not a gap
    assert s.find_gaps("BTC/USDT", "15m", "binance", T0, T0 + STEP * 6, as_of=as_of) == [T0 + STEP * 2]


# ---- Binance provider
def kline(i, close_extra_ms=-1, o=100, h=101, l=99, c=100.5, v=12):
    t = to_ms(T0 + STEP * i)
    return [t, str(o), str(h), str(l), str(c), str(v), t + 900_000 + close_extra_ms, "0", 1, "0", "0", "0"]


def test_binance_parses_and_drops_forming_candle():
    now = T0 + STEP * 2 + timedelta(minutes=5)  # candle 2 is still open
    seen = {}

    def tr(url):
        seen["url"] = url
        return [kline(0), kline(1), kline(2)]

    p = BinancePublicProvider(transport=tr, clock=lambda: now)
    out = run(p.get_candles("BTC/USDT", "15m", start=T0, end=T0 + STEP * 3, limit=3))
    assert [r.candle.t for r in out] == [T0, T0 + STEP]
    assert out[0].quality.publication_timestamp == T0 + STEP  # closeTime + 1ms == next open
    assert "symbol=BTCUSDT" in seen["url"] and "interval=15m" in seen["url"]
    assert f"endTime={to_ms(T0 + STEP * 3) - 1}" in seen["url"]  # exclusive end converted to Binance's inclusive


def test_binance_rejects_bad_data():
    now = T0 + timedelta(days=1)
    for bad in ([kline(0, h=90)], [kline(0, c="NaN")], [["x"]], {"code": -1121}):
        p = BinancePublicProvider(transport=lambda u, b=bad: b, clock=lambda: now)
        with pytest.raises(ProviderError):
            run(p.get_candles("BTC/USDT", "15m"))


def test_binance_ticker_book_funding_oi():
    now = T0 + timedelta(days=1)

    def tr(url):
        if "ticker/24hr" in url:
            return {"lastPrice": "64000.5", "bidPrice": "64000.4", "askPrice": "64000.6", "volume": "1234.5"}
        if "depth" in url:
            return {"bids": [["64000.4", "2"], ["64000.3", "1"]], "asks": [["64000.6", "1"]]}
        if "premiumIndex" in url:
            return {"time": to_ms(T0), "lastFundingRate": "0.00012", "nextFundingTime": to_ms(T0 + timedelta(hours=8))}
        if "openInterest" in url:
            return {"openInterest": "9876.5", "time": to_ms(T0)}

    p = BinancePublicProvider(transport=tr, clock=lambda: now)
    assert run(p.get_ticker("BTC/USDT")).last == 64000.5
    book = run(p.get_orderbook("BTC/USDT"))
    assert isinstance(book, OrderBookSnapshot) and abs(book.bid_pressure - 0.75) < 1e-9
    assert run(p.get_funding("BTC/USDT")).rate == 0.00012
    assert run(p.get_open_interest("BTC/USDT")).value == 9876.5


# ---- Bybit / OKX
def test_bybit_reverses_to_oldest_first_and_checks_retcode():
    now = T0 + timedelta(days=1)
    rows = [[str(to_ms(T0 + STEP * i)), "100", "101", "99", "100.5", "5", "500"] for i in (1, 0)]  # newest first
    p = BybitPublicProvider(transport=lambda u: {"retCode": 0, "result": {"list": rows}}, clock=lambda: now)
    out = run(p.get_candles("BTC/USDT", "15m"))
    assert [r.candle.t for r in out] == [T0, T0 + STEP]
    assert out[0].quality.publication_timestamp == T0 + STEP
    with pytest.raises(ProviderRateLimited):
        run(BybitPublicProvider(transport=lambda u: {"retCode": 10006, "retMsg": "too many"}, clock=lambda: now).get_candles("BTC/USDT", "15m"))
    with pytest.raises(ProviderError):
        run(BybitPublicProvider(transport=lambda u: {"retCode": 10001, "retMsg": "bad"}, clock=lambda: now).get_candles("BTC/USDT", "15m"))


def test_okx_skips_unconfirmed_and_uses_dash_symbol():
    now = T0 + timedelta(days=1)
    seen = {}

    def tr(url):
        seen["url"] = url
        return {"code": "0", "data": [[str(to_ms(T0 + STEP)), "100", "101", "99", "100.5", "5", "1", "1", "0"],
                                      [str(to_ms(T0)), "100", "101", "99", "100.5", "5", "1", "1", "1"]]}

    out = run(OKXPublicProvider(transport=tr, clock=lambda: now).get_candles("BTC/USDT", "15m"))
    assert [r.candle.t for r in out] == [T0] and "instId=BTC-USDT" in seen["url"]


# ---- backfill
class FakeProvider(MarketDataProvider):
    name = "binance"
    max_candles_per_request = 4

    def __init__(self, total=10, fail_first=0, missing=()):
        self.total, self.fail, self.missing, self.calls = total, fail_first, set(missing), 0

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise ProviderRateLimited("slow down")
        out = []
        for i in range(self.total):
            t = T0 + STEP * i
            if start <= t < end and i not in self.missing:
                out.append(rec(i))
        return out[:limit]


def test_backfill_pages_through_range_and_is_rerunnable():
    s, p = CandleStore(), FakeProvider(total=10)
    rep = run(backfill(p, s, "BTC/USDT", "15m", T0, T0 + STEP * 10))
    assert rep.new == 10 and rep.gaps_remaining == 0 and rep.requests == 3
    rep2 = run(backfill(p, s, "BTC/USDT", "15m", T0, T0 + STEP * 10))
    assert rep2.new == 0 and rep2.unchanged == 10


def test_backfill_backs_off_on_rate_limit_and_gives_up():
    waits = []

    async def sleep(x):
        waits.append(x)

    rep = run(backfill(FakeProvider(total=4, fail_first=2), CandleStore(), "BTC/USDT", "15m", T0, T0 + STEP * 4, sleep=sleep))
    assert waits == [1, 2] and rep.new == 4 and rep.rate_limit_waits == 2
    with pytest.raises(ProviderRateLimited):
        run(backfill(FakeProvider(total=4, fail_first=99), CandleStore(), "BTC/USDT", "15m", T0, T0 + STEP * 4, sleep=sleep, max_retries=2))


def test_backfill_reports_gaps():
    rep = run(backfill(FakeProvider(total=8, missing={3}), CandleStore(), "BTC/USDT", "15m", T0, T0 + STEP * 8))
    assert rep.gaps_remaining == 1


# ---- hub
class Broken(MarketDataProvider):
    name = "broken"

    async def get_candles(self, *a, **k):
        raise ProviderError("down")


def test_hub_fails_over_and_reports_source():
    hub = MarketDataHub([Broken(), FakeProvider(total=3)])
    out, src = run(hub.candles("BTC/USDT", "15m", start=T0, end=T0 + STEP * 3))
    assert src == "binance" and len(out) == 3


def test_hub_all_fail_is_loud():
    with pytest.raises(AllProvidersFailed):
        run(MarketDataHub([Broken(), Broken()]).candles("BTC/USDT", "15m"))


def test_divergence_detection():
    a = [rec(0, c=100), rec(1, c=100)]
    b = [rec(0, c=100.1, source="bybit"), rec(1, c=103, source="bybit")]
    d = divergences(a, b, tolerance=0.005)
    assert len(d) == 1 and d[0].open_time == (T0 + STEP).isoformat()


# ---- market state
def series(n=120, drift=0.002, vol_last=1.0):
    out, p = [], 100.0
    for i in range(n):
        o, p = p, p * (1 + drift + (0.001 if i % 2 else -0.001))
        out.append(Candle(t=T0 + STEP * i, o=o, h=max(o, p) * 1.001, l=min(o, p) * 0.999, c=p, v=10 * (vol_last if i == n - 1 else 1)))
    return out


def test_state_uptrend_and_volume_ratio():
    st = build_market_state("BTC/USDT", series(vol_last=2.0), T0 + STEP * 120)
    assert st.trend == "BULLISH" and st.regime == "TRENDING" and abs(st.volume_ratio - 2.0) < 1e-9


def test_state_downtrend():
    assert build_market_state("BTC/USDT", series(drift=-0.002), T0 + STEP * 120).trend == "BEARISH"


def test_state_needs_enough_history_and_is_deterministic():
    as_of = T0 + STEP * 120
    with pytest.raises(InsufficientData):
        build_market_state("BTC/USDT", series(30), T0 + STEP * 30)
    a, b = build_market_state("BTC/USDT", series(), as_of), build_market_state("BTC/USDT", series(), as_of)
    assert a.model_dump() == b.model_dump()


def test_state_from_store_as_of_cannot_see_future():
    s = CandleStore()
    cs = series(150)
    s.put_candles([CandleRecord(symbol="BTC/USDT", timeframe="15m", candle=c,
                                quality=DataQuality(source="binance", data_timestamp=c.t, publication_timestamp=c.t + STEP,
                                                    ingested_at=T0)) for c in cs])
    as_of = T0 + STEP * 100
    got = s.get_candles("BTC/USDT", "15m", "binance", T0, T0 + STEP * 150, as_of=as_of)
    assert len(got) == 100 and got[-1].candle.t == T0 + STEP * 99
    assert build_market_state("BTC/USDT", [g.candle for g in got], as_of).candles[-1].t == T0 + STEP * 99


# ---- recent history for the live feed (paging backwards past the per-request cap)
class LatestFirst(MarketDataProvider):
    """Behaves like the real exchanges: with no start, returns the latest `limit` closed candles before `end` (oldest first)."""
    name = "binance"
    max_candles_per_request = 4

    def __init__(self, total=10, fail_on_call=None, missing=()):
        self.total, self.fail_on_call, self.missing, self.calls = total, fail_on_call, set(missing), []

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
        self.calls.append((end, limit))
        if self.fail_on_call == len(self.calls):
            raise ProviderError("down mid-way")
        out = [rec(i) for i in range(self.total) if i not in self.missing and (end is None or T0 + STEP * i < end)]
        return out[-min(limit, self.max_candles_per_request):]


def test_recent_candles_pages_back_past_the_cap_oldest_first_without_duplicates():
    p = LatestFirst(total=10)
    out, src = run(MarketDataHub([p]).recent_candles("BTC/USDT", "15m", 9))
    assert src == "binance" and [r.candle.t for r in out] == [T0 + STEP * i for i in range(1, 10)]
    assert len(p.calls) == 3 and p.calls[0][0] is None and p.calls[1][0] == T0 + STEP * 6   # pages end where the last one began
    assert p.calls[2][1] == 1                                                                 # last page asks only for what is missing


def test_recent_candles_returns_all_it_has_when_history_is_short_and_stops():
    p = LatestFirst(total=5)
    out, _ = run(MarketDataHub([p]).recent_candles("BTC/USDT", "15m", 1210))
    assert [r.candle.t for r in out] == [T0 + STEP * i for i in range(5)] and len(p.calls) == 3   # 4, 1, then an empty page


def test_recent_candles_never_mixes_sources_a_failed_page_restarts_on_the_next_provider():
    class Other(LatestFirst):
        name = "bybit"
    first, second = LatestFirst(total=10, fail_on_call=2), Other(total=10)
    out, src = run(MarketDataHub([first, second]).recent_candles("BTC/USDT", "15m", 8))
    assert src == "bybit" and len(out) == 8 and len(second.calls) == 2   # the first provider's page 1 is thrown away
    with pytest.raises(AllProvidersFailed):
        run(MarketDataHub([LatestFirst(fail_on_call=1)]).recent_candles("BTC/USDT", "15m", 8))


def test_feed_builds_state_from_1210_candles_by_default():
    from dewako.runtime.market_feed import FEED_HISTORY, make_state_provider

    class Many(LatestFirst):
        max_candles_per_request = 1000
    p = Many(total=1500)
    state = run(make_state_provider(MarketDataHub([p]), clock=lambda: T0 + STEP * 1501)("BTC/USDT", "15m"))
    assert FEED_HISTORY == 1210 and len(state.candles) == 1210 and state.candles[-1].t == T0 + STEP * 1499
    assert [c.t for c in state.candles] == sorted({c.t for c in state.candles})


def test_recent_candles_stops_at_once_if_a_provider_ignores_the_end_time():
    class Stuck(LatestFirst):
        async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
            self.calls.append((end, limit))
            return [rec(i) for i in range(6, 10)]          # always the same latest page
    p = Stuck()
    out, _ = run(MarketDataHub([p]).recent_candles("BTC/USDT", "15m", 1210))
    assert len(out) == 4 and len(p.calls) == 2              # second page adds nothing new: stop, do not spin through 300+ requests
