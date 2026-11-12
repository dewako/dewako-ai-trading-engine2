"""Server-side market-data plumbing for the runtime: a MarketState provider backed by the MarketDataHub, and the paper-price refresher.

Public market data only (no credentials). Nothing here is reachable from an API route that accepts prices: the only way prices reach the
paper adapter is through the hub's providers (or, in tests, a provider injected by test code).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

from dewako.core.types import MarketState
from dewako.market_data.hub import MarketDataHub
from dewako.market_data.state_builder import build_market_state


FEED_HISTORY = 1210   # candles per MarketState: Dewako ETH 1.0 needs 1,200 (its 200-day average on 4h) plus a small margin


def make_state_provider(hub: MarketDataHub, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                        history: int = FEED_HISTORY) -> Callable[[str, str], Awaitable[MarketState]]:
    async def provider(symbol: str, timeframe: str) -> MarketState:
        recs, _used = await hub.recent_candles(symbol, timeframe, history)
        return build_market_state(symbol, [r.candle for r in recs], clock(), timeframe)
    return provider


async def refresh_paper_prices(hub: MarketDataHub, adapters: dict, symbols: set[str]) -> list[str]:
    """Set each paper adapter's price from a live public ticker, stamped with the ticker's own timestamp (so a stalled feed makes the quote
    stale and the quote guard blocks, rather than the paper adapter inventing a fresh timestamp). Returns the symbols that failed."""
    failed: list[str] = []
    paper = [a for a in adapters.values() if getattr(a, "environment", "") == "PAPER" and hasattr(a, "set_price")]
    for sym in sorted(symbols):
        try:
            t = await hub.ticker(sym)
        except Exception:
            failed.append(sym)
            continue
        for a in paper:
            a.set_price(sym, t.last, as_of=t.quality.data_timestamp)
    return failed
