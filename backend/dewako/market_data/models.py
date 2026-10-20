"""Normalized market data records. Every record carries DataQuality so the backtester can
tell when a fact became public. Symbols are always BASE/QUOTE."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from dewako.core.types import Candle, DataQuality


class CandleRecord(BaseModel):
    symbol: str
    timeframe: str
    candle: Candle
    quality: DataQuality


class Ticker(BaseModel):
    symbol: str
    last: float
    bid: float
    ask: float
    volume_24h: float
    quality: DataQuality


class OrderBookSnapshot(BaseModel):
    symbol: str
    bids: list[tuple[float, float]]  # (price, size), best first
    asks: list[tuple[float, float]]
    quality: DataQuality

    @property
    def bid_pressure(self) -> float:
        """Share of resting size on the bid side across the returned depth (0..1)."""
        b, a = sum(s for _, s in self.bids), sum(s for _, s in self.asks)
        return b / (a + b) if (a + b) else 0.5


class FundingRate(BaseModel):
    symbol: str
    rate: float  # per funding interval, e.g. 0.0001 = 0.01%
    next_funding_time: Optional[datetime] = None
    quality: DataQuality


class OpenInterest(BaseModel):
    symbol: str
    value: float  # contracts or base units, as reported by the venue
    quality: DataQuality
