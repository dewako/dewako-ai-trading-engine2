"""Binance public market data (no API key).

Response shapes follow Binance's public docs. They have NOT been run against the live API
from this build environment (outbound access is blocked), so treat the first live run as a
test and compare a few candles against the exchange's own chart.

Publication time of a candle is its close time: nobody can know a 15m candle before it closes.
"""
from __future__ import annotations

import asyncio
import math
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Optional

from dewako.core import symbols
from dewako.core.types import Candle, DataQuality
from dewako.market_data.http import Transport, urllib_transport
from dewako.market_data.models import CandleRecord, FundingRate, OpenInterest, OrderBookSnapshot, Ticker
from dewako.market_data.provider import MarketDataProvider, ProviderError
from dewako.market_data.timeframes import from_ms, to_ms

SPOT = "https://api.binance.com"
FUTURES = "https://fapi.binance.com"
_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}


def _num(x, what: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ProviderError(f"binance returned a non-numeric {what}") from None
    if not math.isfinite(v):
        raise ProviderError(f"binance returned a non-finite {what}")
    return v


class BinancePublicProvider(MarketDataProvider):
    name = "binance"
    max_candles_per_request = 1000

    def __init__(self, transport: Transport | None = None, clock: Callable[[], datetime] | None = None):
        self._t = transport or urllib_transport
        self._now = clock or (lambda: datetime.now(timezone.utc))

    async def _get(self, base: str, path: str, **params):
        url = f"{base}{path}?{urllib.parse.urlencode(params)}"
        return await asyncio.to_thread(self._t, url)

    def _q(self, ts: datetime, published: datetime, confidence: float = 1.0) -> DataQuality:
        return DataQuality(source=self.name, data_timestamp=ts, publication_timestamp=published,
                           ingested_at=self._now(), confidence=confidence)

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
        if timeframe not in _INTERVALS:
            raise ProviderError(f"binance: unsupported timeframe {timeframe}")
        params = {"symbol": symbols.to_native(symbol, "binance"), "interval": _INTERVALS[timeframe],
                  "limit": min(limit, self.max_candles_per_request)}
        if start:
            params["startTime"] = to_ms(start)
        if end:
            params["endTime"] = to_ms(end) - 1  # Binance endTime is inclusive; ours is exclusive
        rows = await self._get(SPOT, "/api/v3/klines", **params)
        if not isinstance(rows, list):
            raise ProviderError("binance: unexpected klines response")
        now, out = self._now(), []
        for r in rows:
            if not isinstance(r, list) or len(r) < 7:
                raise ProviderError("binance: malformed kline row")
            o, h, l, c, v = (_num(r[i], "price/volume") for i in range(1, 6))
            if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h) or v < 0:
                raise ProviderError("binance: kline failed sanity checks")
            published = from_ms(int(r[6]) + 1)  # closeTime is the last millisecond of the candle
            if published > now:
                continue  # candle still forming
            opened = from_ms(int(r[0]))
            out.append(CandleRecord(symbol=symbol, timeframe=timeframe,
                                    candle=Candle(t=opened, o=o, h=h, l=l, c=c, v=v), quality=self._q(opened, published)))
        return out

    async def get_ticker(self, symbol):
        d = await self._get(SPOT, "/api/v3/ticker/24hr", symbol=symbols.to_native(symbol, "binance"))
        try:
            now = self._now()
            return Ticker(symbol=symbol, last=_num(d["lastPrice"], "last"), bid=_num(d["bidPrice"], "bid"),
                          ask=_num(d["askPrice"], "ask"), volume_24h=_num(d["volume"], "volume"),
                          quality=self._q(now, now))
        except (KeyError, TypeError):
            raise ProviderError("binance: unexpected ticker response") from None

    async def get_orderbook(self, symbol, depth=20):
        d = await self._get(SPOT, "/api/v3/depth", symbol=symbols.to_native(symbol, "binance"), limit=depth)
        try:
            now = self._now()
            return OrderBookSnapshot(symbol=symbol,
                                     bids=[(_num(p, "bid"), _num(q, "size")) for p, q in d["bids"]],
                                     asks=[(_num(p, "ask"), _num(q, "size")) for p, q in d["asks"]],
                                     quality=self._q(now, now))
        except (KeyError, TypeError, ValueError):
            raise ProviderError("binance: unexpected depth response") from None

    async def get_funding(self, symbol):
        d = await self._get(FUTURES, "/fapi/v1/premiumIndex", symbol=symbols.to_native(symbol, "binance"))
        try:
            ts = from_ms(int(d["time"]))
            return FundingRate(symbol=symbol, rate=_num(d["lastFundingRate"], "funding"),
                               next_funding_time=from_ms(int(d["nextFundingTime"])), quality=self._q(ts, ts))
        except (KeyError, TypeError, ValueError):
            raise ProviderError("binance: unexpected funding response") from None

    async def get_open_interest(self, symbol):
        d = await self._get(FUTURES, "/fapi/v1/openInterest", symbol=symbols.to_native(symbol, "binance"))
        try:
            ts = from_ms(int(d["time"]))
            return OpenInterest(symbol=symbol, value=_num(d["openInterest"], "open interest"), quality=self._q(ts, ts))
        except (KeyError, TypeError, ValueError):
            raise ProviderError("binance: unexpected open interest response") from None
