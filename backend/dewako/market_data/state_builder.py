"""Turn closed candles into the normalized MarketState that strategies and the AI consume.

Pure and deterministic: the same candles always give the same state. Callers pass only candles
known at the time (see CandleStore.get_candles with as_of), so this cannot look ahead.
Classifications are simple rules, not a trained model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from dewako.core.types import Candle, MarketState
from dewako.market_data.freshness import validate_candles
from dewako.strategies.indicators import atr, ema

MIN_CANDLES = 60


class InsufficientData(ValueError):
    pass


def build_market_state(symbol: str, candles: list[Candle], as_of: datetime, timeframe: str = "15m",
                       minutes_to_high_impact_event: Optional[int] = None) -> MarketState:
    validate_candles(candles, timeframe, as_of)
    if len(candles) < MIN_CANDLES:
        raise InsufficientData(f"need at least {MIN_CANDLES} closed candles, got {len(candles)}")
    closes = [c.c for c in candles]
    price = closes[-1]
    fast, slow = ema(closes, 20)[-1], ema(closes, 50)[-1]
    trend = "BULLISH" if fast > slow and price > fast else "BEARISH" if fast < slow and price < fast else "NEUTRAL"

    atr_now = atr(candles, 14)
    atr_pct = atr_now / price
    ret10 = abs(price / closes[-11] - 1)
    strength = ret10 / atr_pct if atr_pct else 0.0
    momentum = "STRONG" if strength >= 2.5 else "MODERATE" if strength >= 1.0 else "WEAK"

    history = sorted(atr(candles[: i + 1], 14) / candles[i].c for i in range(len(candles) - 50, len(candles)))
    median = history[len(history) // 2]
    rel = atr_pct / median if median else 1.0
    volatility = "HIGH" if rel > 1.5 else "LOW" if rel < 0.7 else "MEDIUM"

    prev = [c.v for c in candles[-21:-1]]
    vol_ratio = candles[-1].v / (sum(prev) / len(prev)) if prev and sum(prev) else 1.0

    if trend != "NEUTRAL" and momentum != "WEAK":
        regime = "TRENDING"
    elif volatility == "HIGH":
        regime = "VOLATILE"
    else:
        regime = "RANGING"

    return MarketState(symbol=symbol, as_of=as_of, candles=candles, trend=trend, momentum=momentum, volatility=volatility,
                       volume_ratio=vol_ratio, regime=regime, upcoming_high_impact_event_minutes=minutes_to_high_impact_event)
