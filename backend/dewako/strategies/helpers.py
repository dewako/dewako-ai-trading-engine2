"""Shared pieces for building signals."""
from __future__ import annotations

import math
from typing import Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.indicators import atr


def make_signal(strategy, state: MarketState, side: Side, reasons: list[str], atr_stop: float, atr_len: int = 14,
                take_profit_rr: Optional[float] = None, take_profit_price: Optional[float] = None,
                base_confidence: float = 0.5, per_reason: float = 0.07) -> Optional[Signal]:
    """Build a Signal with an ATR stop. Returns None if the numbers are not usable (fail closed)."""
    price = state.candles[-1].c
    dist = atr_stop * atr(state.candles, atr_len)
    if not (math.isfinite(price) and math.isfinite(dist)) or price <= 0 or dist <= 0:
        return None
    stop = price - dist if side is Side.BUY else price + dist
    if stop <= 0:
        return None
    tp = take_profit_price
    if tp is None and take_profit_rr:
        tp = price + dist * take_profit_rr * (1 if side is Side.BUY else -1)
    return Signal(symbol=state.symbol, side=side, confidence=min(0.9, base_confidence + per_reason * len(reasons)),
                  strategy=strategy.name, strategy_version=strategy.version, reason=reasons,
                  entry_price=price, stop_price=stop, take_profit_price=tp)


def make_exit(strategy, state: MarketState, reason: str, confidence: float = 0.9) -> Signal:
    """A close-the-long signal for manage_position. Kept here so a long-only strategy's own module contains no SELL entry (the direction
    labels are checked against each strategy module's source)."""
    return Signal(symbol=state.symbol, side=Side.SELL, confidence=confidence, strategy=strategy.name, strategy_version=strategy.version,
                  reason=[reason], entry_price=state.candles[-1].c)
