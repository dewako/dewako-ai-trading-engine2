"""Bybit v5 public spot market data (candles and ticker). Not yet run against the live API.

Bybit returns candles newest first as [startMs, open, high, low, close, volume, turnover].
A candle's publication time is its open time plus the timeframe length.
"""
from __future__ import annotations

import asyncio
import math
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

from dewako.core import symbols
from dewako.core.types import Candle, DataQuality
from dewako.market_data.http import Transport, urllib_transport
from dewako.market_data.models import CandleRecord, Ticker
from dewako.market_data.provider import MarketDataProvider, ProviderError, ProviderRateLimited
from dewako.market_data.timeframes import delta, from_ms, to_ms

BASE = "https://api.bybit.com"
_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
_RATE_LIMIT_CODES = {10006, 10018}


def _num(x, what):
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ProviderError(f"bybit returned a non-numeric {what}") from None
    if not math.isfinite(v):
        raise ProviderError(f"bybit returned a non-finite {what}")
    return v


class BybitPublicProvider(MarketDataProvider):
    name = "bybit"
    max_candles_per_request = 1000

    def __init__(self, transport: Transport | None = None, clock: Callable[[], datetime] | None = None):
        self._t = transport or urllib_transport
        self._now = clock or (lambda: datetime.now(timezone.utc))

    async def _get(self, path, **params):
        d = await asyncio.to_thread(self._t, f"{BASE}{path}?{urllib.parse.urlencode(params)}")
        if not isinstance(d, dict) or "retCode" not in d:
            raise ProviderError("bybit: unexpected response")
        if d["retCode"] in _RATE_LIMIT_CODES:
            raise ProviderRateLimited("bybit rate limit")
        if d["retCode"] != 0:
            raise ProviderError(f"bybit error {d['retCode']}: {d.get('retMsg', '')}")
        return d.get("result") or {}

    def _q(self, ts, published):
        return DataQuality(source=self.name, data_timestamp=ts, publication_timestamp=published, ingested_at=self._now())

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=500):
        if timeframe not in _INTERVALS:
            raise ProviderError(f"bybit: unsupported timeframe {timeframe}")
        params = {"category": "spot", "symbol": symbols.to_native(symbol, "bybit"), "interval": _INTERVALS[timeframe],
                  "limit": min(limit, self.max_candles_per_request)}
        if start:
            params["start"] = to_ms(start)
        if end:
            params["end"] = to_ms(end) - 1
        rows = (await self._get("/v5/market/kline", **params)).get("list")
        if not isinstance(rows, list):
            raise ProviderError("bybit: unexpected kline list")
        now, step, out = self._now(), delta(timeframe), []
        for r in reversed(rows):  # oldest first
            if not isinstance(r, list) or len(r) < 6:
                raise ProviderError("bybit: malformed kline row")
            o, h, l, c, v = (_num(r[i], "price/volume") for i in range(1, 6))
            if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h) or v < 0:
                raise ProviderError("bybit: kline failed sanity checks")
            opened = from_ms(int(r[0]))
            published = opened + step
            if published > now:
                continue
            if start and opened < start or end and opened >= end:
                continue
            out.append(CandleRecord(symbol=symbol, timeframe=timeframe, candle=Candle(t=opened, o=o, h=h, l=l, c=c, v=v),
                                    quality=self._q(opened, published)))
        return out

    async def get_ticker(self, symbol):
        res = await self._get("/v5/market/tickers", category="spot", symbol=symbols.to_native(symbol, "bybit"))
        try:
            d, now = res["list"][0], self._now()
            return Ticker(symbol=symbol, last=_num(d["lastPrice"], "last"), bid=_num(d["bid1Price"], "bid"),
                          ask=_num(d["ask1Price"], "ask"), volume_24h=_num(d["volume24h"], "volume"), quality=self._q(now, now))
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bybit: unexpected ticker response") from None
