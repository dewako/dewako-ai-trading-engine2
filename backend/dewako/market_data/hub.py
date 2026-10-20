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

    async def cross_check(self, symbol: str, timeframe: str, limit: int = 100, tolerance: float = 0.005) -> list[Divergence]:
        if len(self.providers) < 2:
            return []
        a = await self.providers[0].get_candles(symbol, timeframe, limit=limit)
        b = await self.providers[1].get_candles(symbol, timeframe, limit=limit)
        return divergences(a, b, tolerance)
