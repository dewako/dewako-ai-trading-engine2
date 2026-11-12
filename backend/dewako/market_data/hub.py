"""Provider hub: failover between sources and cross-source validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from dewako.market_data.models import CandleRecord
from dewako.market_data.provider import MarketDataProvider, ProviderError


class AllProvidersFailed(ProviderError):
    pass


@dataclass
class Divergence:
    open_time: str
    source_a: str
    source_b: str
    close_a: float
    close_b: float
    pct: float


def divergences(a: list[CandleRecord], b: list[CandleRecord], tolerance: float = 0.005) -> list[Divergence]:
    """Candles present in both sources whose closes differ by more than `tolerance` (0.005 = 0.5%)."""
    bm = {r.candle.t: r for r in b}
    out = []
    for r in a:
        o = bm.get(r.candle.t)
        if o and o.candle.c > 0:
            pct = abs(r.candle.c - o.candle.c) / o.candle.c
            if pct > tolerance:
                out.append(Divergence(r.candle.t.isoformat(), r.quality.source, o.quality.source, r.candle.c, o.candle.c, pct))
    return out


class MarketDataHub:
    def __init__(self, providers: list[MarketDataProvider]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers

    def by_name(self, name: str) -> MarketDataProvider:
        for p in self.providers:
            if p.name == name:
                return p
        raise KeyError(name)

    async def candles(self, symbol: str, timeframe: str, limit: int = 200, prefer: Optional[str] = None, **kw
                      ) -> tuple[list[CandleRecord], str]:
        """Try providers in order. Returns (records, provider name). Which source answered is never hidden."""
        order = sorted(self.providers, key=lambda p: p.name != prefer) if prefer else self.providers
        errors = []
        for p in order:
            try:
                return await p.get_candles(symbol, timeframe, limit=limit, **kw), p.name
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
        raise AllProvidersFailed("; ".join(errors))

    async def recent_candles(self, symbol: str, timeframe: str, count: int) -> tuple[list[CandleRecord], str]:
        """The latest `count` closed candles, oldest first, paging backwards past a provider's per-request cap. Every page comes from ONE
        provider (sources are never mixed); if any page fails, the next provider starts over from scratch. A provider that has less
        history than `count` returns what it has (strategies that need more simply stay out)."""
        if count < 1:
            raise ValueError("count must be positive")
        errors = []
        for p in self.providers:
            try:
                return await self._page_back(p, symbol, timeframe, count), p.name
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
        raise AllProvidersFailed("; ".join(errors))

    @staticmethod
    async def _page_back(p: MarketDataProvider, symbol: str, timeframe: str, count: int) -> list[CandleRecord]:
        page = max(1, int(getattr(p, "max_candles_per_request", count) or count))
        by_t: dict = {}
        end = None
        for _ in range(count // page + 3):                  # bounded: never loops on a provider that keeps answering the same page
            recs = await p.get_candles(symbol, timeframe, end=end, limit=min(page, count - len(by_t)))
            new = [r for r in recs if r.candle.t not in by_t and (end is None or r.candle.t < end)]
            if not new:
                break
            for r in new:
                by_t[r.candle.t] = r
            if len(by_t) >= count:
                break
            end = min(r.candle.t for r in new)
        return [by_t[t] for t in sorted(by_t)][-count:]

    async def ticker(self, symbol: str):
        """First provider that answers. Returns the Ticker (its `quality` says which source and when)."""
        errors = []
        for p in self.providers:
            try:
                return await p.get_ticker(symbol)
            except ProviderError as e:
                errors.append(f"{p.name}: {e}")
        raise AllProvidersFailed("; ".join(errors))

    async def cross_check(self, symbol: str, timeframe: str, limit: int = 100, tolerance: float = 0.005) -> list[Divergence]:
        if len(self.providers) < 2:
            return []
        a = await self.providers[0].get_candles(symbol, timeframe, limit=limit)
        b = await self.providers[1].get_candles(symbol, timeframe, limit=limit)
        return divergences(a, b, tolerance)
