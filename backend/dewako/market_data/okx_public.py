"""OKX v5 public market data (candles and ticker). Not yet run against the live API.

history-candles rows are newest first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm].
confirm == "1" means the candle is closed. OKX `after` returns records older than the value,
`before` returns records newer than it.
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

BASE = "https://www.okx.com"
_BARS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}


def _num(x, what):
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ProviderError(f"okx returned a non-numeric {what}") from None
    if not math.isfinite(v):
        raise ProviderError(f"okx returned a non-finite {what}")
    return v


class OKXPublicProvider(MarketDataProvider):
    name = "okx"
    max_candles_per_request = 100

    def __init__(self, transport: Transport | None = None, clock: Callable[[], datetime] | None = None):
        self._t = transport or urllib_transport
        self._now = clock or (lambda: datetime.now(timezone.utc))

    async def _get(self, path, **params):
        d = await asyncio.to_thread(self._t, f"{BASE}{path}?{urllib.parse.urlencode(params)}")
        if not isinstance(d, dict) or "code" not in d:
            raise ProviderError("okx: unexpected response")
        if str(d["code"]) in ("50011", "50061"):
            raise ProviderRateLimited("okx rate limit")
        if str(d["code"]) != "0":
            raise ProviderError(f"okx error {d['code']}: {d.get('msg', '')}")
        return d.get("data") or []

    def _q(self, ts, published):
        return DataQuality(source=self.name, data_timestamp=ts, publication_timestamp=published, ingested_at=self._now())

    async def get_candles(self, symbol, timeframe, start=None, end=None, limit=100):
        if timeframe not in _BARS:
            raise ProviderError(f"okx: unsupported timeframe {timeframe}")
        params = {"instId": symbols.to_native(symbol, "okx"), "bar": _BARS[timeframe],
                  "limit": min(limit, self.max_candles_per_request)}
        if end:
            params["after"] = to_ms(end)  # records older than end (exclusive)
        if start:
            params["before"] = to_ms(start) - 1  # records newer than this, so start is inclusive
        rows, step, out = await self._get("/api/v5/market/history-candles", **params), delta(timeframe), []
        for r in reversed(rows):
            if not isinstance(r, list) or len(r) < 9:
                raise ProviderError("okx: malformed candle row")
            if str(r[8]) != "1":
                continue  # unconfirmed (still forming)
            o, h, l, c, v = (_num(r[i], "price/volume") for i in range(1, 6))
            if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h) or v < 0:
                raise ProviderError("okx: candle failed sanity checks")
            opened = from_ms(int(r[0]))
            published = opened + step
            if published > self._now():
                continue
            out.append(CandleRecord(symbol=symbol, timeframe=timeframe, candle=Candle(t=opened, o=o, h=h, l=l, c=c, v=v),
                                    quality=self._q(opened, published)))
        return out

    async def get_ticker(self, symbol):
        rows = await self._get("/api/v5/market/ticker", instId=symbols.to_native(symbol, "okx"))
        try:
            d, now = rows[0], self._now()
            return Ticker(symbol=symbol, last=_num(d["last"], "last"), bid=_num(d["bidPx"], "bid"),
                          ask=_num(d["askPx"], "ask"), volume_24h=_num(d["vol24h"], "volume"), quality=self._q(now, now))
        except (KeyError, IndexError, TypeError):
            raise ProviderError("okx: unexpected ticker response") from None
