"""Historical backfill with paging and rate-limit backoff."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from dewako.market_data.provider import MarketDataProvider, ProviderRateLimited
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import delta, floor


@dataclass
class BackfillReport:
    requests: int = 0
    new: int = 0
    unchanged: int = 0
    revised: int = 0
    gaps_remaining: int = 0
    rate_limit_waits: int = 0


async def backfill(provider: MarketDataProvider, store: CandleStore, symbol: str, timeframe: str,
                   start: datetime, end: datetime, sleep: Callable[[float], Awaitable] = asyncio.sleep,
                   max_retries: int = 5) -> BackfillReport:
    """Fetch [start, end) in pages and store it. Safe to re-run: unchanged candles are skipped."""
    if end <= start:
        raise ValueError("end must be after start")
    step, page = delta(timeframe), provider.max_candles_per_request
    cursor, rep = floor(start, timeframe), BackfillReport()
    if cursor < start:
        cursor += step
    while cursor < end:
        for attempt in range(max_retries + 1):
            try:
                recs = await provider.get_candles(symbol, timeframe, start=cursor, end=end, limit=page)
                break
            except ProviderRateLimited:
                if attempt == max_retries:
                    raise
                rep.rate_limit_waits += 1
                await sleep(min(60, 2 ** attempt))
        rep.requests += 1
        if not recs:
            cursor += step * page  # nothing in this window: move on instead of looping forever
            continue
        r = store.put_candles(recs)
        rep.new, rep.unchanged, rep.revised = rep.new + r.new, rep.unchanged + r.unchanged, rep.revised + r.revised
        nxt = max(x.candle.t for x in recs) + step
        cursor = nxt if nxt > cursor else cursor + step
    rep.gaps_remaining = len(store.find_gaps(symbol, timeframe, provider.name, start, end))
    return rep
