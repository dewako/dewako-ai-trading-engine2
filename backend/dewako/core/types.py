"""Normalized types shared by every layer.

Strategies, the AI layer and the risk engine only ever see these types. Exchange-specific
formats are translated at the adapter boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # sent, outcome not confirmed: must be reconciled, never blindly retried


class BotState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


class DataQuality(BaseModel):
    """Attached to every stored record so the backtester can prevent look-ahead bias."""

    source: str
    data_timestamp: datetime  # when the thing described happened
    publication_timestamp: datetime  # when it became publicly known
    ingested_at: datetime = Field(default_factory=utcnow)
    confidence: float = Field(1.0, ge=0, le=1)
    revision: int = 0  # 0 = first release; higher = later revision


class Candle(BaseModel):
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


class MarketState(BaseModel):
    """The single input handed to strategies and the AI layer. Symbol is always BASE/QUOTE."""

    symbol: str
    as_of: datetime
    candles: list[Candle]
    trend: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL
    momentum: str = "MODERATE"
    volatility: str = "MEDIUM"
    volume_ratio: float = 1.0
    regime: str = "RANGING"
    upcoming_high_impact_event_minutes: Optional[int] = None


class Signal(BaseModel):
    """A strategy's opinion. A signal never places an order."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    confidence: float = Field(ge=0, le=1)
    strategy: str
    strategy_version: str
    timestamp: datetime = Field(default_factory=utcnow)
    signal_id: str = ""  # set by the SignalEngine: deterministic, so the same bar never yields two different ids
    reason: list[str] = Field(default_factory=list)
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    entry_price: Optional[float] = None


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    type: OrderType
    quantity: float = Field(gt=0)
    price: Optional[float] = None
    stop_price: Optional[float] = None
    client_order_id: str
    strategy: str = ""
    bot_id: str = ""


class Order(BaseModel):
    exchange_order_id: Optional[str]
    client_order_id: str
    symbol: str
    side: Side
    type: OrderType
    quantity: float
    filled: float = 0.0
    price: Optional[float] = None
    status: OrderStatus
    timestamp: datetime = Field(default_factory=utcnow)


class RiskDecision(BaseModel):
    """Only an approved RiskDecision can reach the order engine."""

    approved: bool
    signal: Signal
    quantity: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=utcnow)
