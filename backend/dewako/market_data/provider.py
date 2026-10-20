"""MarketDataProvider contract. Providers translate venue formats into normalized records."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from dewako.market_data.models import CandleRecord, FundingRate, OpenInterest, OrderBookSnapshot, Ticker


class ProviderError(Exception):
    """Request failed or the response could not be trusted."""


class ProviderRateLimited(ProviderError):
    """The venue asked us to slow down. Callers back off and retry."""


class Unsupported(ProviderError):
    """This provider does not offer that data type."""


class MarketDataProvider(ABC):
    name: str = "base"
    max_candles_per_request: int = 500

    @abstractmethod
    async def get_candles(self, symbol: str, timeframe: str, start: Optional[datetime] = None,
                          end: Optional[datetime] = None, limit: int = 500) -> list[CandleRecord]:
        """Closed candles only, oldest first. An unfinished candle must never be returned.

        `start` is inclusive and `end` exclusive, both on candle open time.
        """

    async def get_ticker(self, symbol: str) -> Ticker:
        raise Unsupported(f"{self.name} does not provide tickers")

    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        raise Unsupported(f"{self.name} does not provide order books")

    async def get_funding(self, symbol: str) -> FundingRate:
        raise Unsupported(f"{self.name} does not provide funding rates")

    async def get_open_interest(self, symbol: str) -> OpenInterest:
        raise Unsupported(f"{self.name} does not provide open interest")
