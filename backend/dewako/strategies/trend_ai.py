"""Trend AI v2.1: demo implementation (EMA alignment + momentum + volume)."""
from __future__ import annotations

from typing import Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.base import Strategy, StrategyParameterError, register


def ema(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


@register
class TrendAI(Strategy):
    name = "Trend AI"
    version = "2.1"
    description = "Follows established trends. Needs EMA alignment, momentum and volume confirmation."
    risk_profile = "Medium"
    default_parameters = {"ema_fast": 20, "ema_slow": 50, "min_volume_ratio": 1.2, "atr_stop": 2.0}

    def validate_parameters(self, p):
        if not 2 <= p["ema_fast"] < p["ema_slow"]:
            raise StrategyParameterError("ema_fast must be >= 2 and smaller than ema_slow")
        if p["atr_stop"] <= 0:
            raise StrategyParameterError("atr_stop must be positive")

    def generate_signal(self, state: MarketState) -> Optional[Signal]:
        p = self.parameters
        closes = [c.c for c in state.candles]
        if len(closes) < p["ema_slow"] + 5:
            return None
        fast, slow = ema(closes, p["ema_fast"])[-1], ema(closes, p["ema_slow"])[-1]
        price = closes[-1]
        reasons: list[str] = []
        if fast > slow and price > fast:
            side = Side.BUY
            reasons.append("EMA alignment (fast above slow, price above fast)")
        elif fast < slow and price < fast:
            side = Side.SELL
            reasons.append("EMA alignment (fast below slow, price below fast)")
        else:
            return None
        if state.volume_ratio < p["min_volume_ratio"]:
            return None
        reasons.append("Volume confirmation")
        if state.momentum == "STRONG":
            reasons.append("Momentum strong")
        window = state.candles[-14:]
        atr = sum(c.h - c.l for c in window) / len(window)
        stop = price - p["atr_stop"] * atr if side is Side.BUY else price + p["atr_stop"] * atr
        conf = min(0.95, 0.55 + 0.1 * len(reasons))
        return Signal(symbol=state.symbol, side=side, confidence=conf, strategy=self.name,
                      strategy_version=self.version, reason=reasons, stop_price=stop, entry_price=price)
