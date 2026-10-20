"""Trend AI v2.2: v2.1 plus an ADX strength filter and a market-structure check.

v2.1 stays exactly as it was (backtests refer to it). This is a new version, not an edit.
"""
from __future__ import annotations

from dewako.core.types import MarketState, Side
from dewako.strategies.base import Strategy, StrategyParameterError, register
from dewako.strategies.helpers import make_signal
from dewako.strategies.indicators import adx, ema


def _structure(cs, half: int = 10):
    """+1 higher highs and higher lows, -1 lower highs and lower lows, 0 otherwise (last two windows)."""
    a, b = cs[-2 * half:-half], cs[-half:]
    hh, hl = max(c.h for c in b) > max(c.h for c in a), min(c.l for c in b) > min(c.l for c in a)
    lh, ll = max(c.h for c in b) < max(c.h for c in a), min(c.l for c in b) < min(c.l for c in a)
    return 1 if hh and hl else -1 if lh and ll else 0


@register
class TrendAIv22(Strategy):
    name = "Trend AI"
    version = "2.2"
    description = "Follows established trends. Needs EMA alignment, ADX strength with matching direction, clean market structure and volume."
    risk_profile = "Medium"
    default_parameters = {"ema_fast": 20, "ema_slow": 50, "adx_min": 25, "min_volume_ratio": 1.0, "atr_stop": 2.0}
    parameter_ranges = {"ema_fast": (2, 200), "ema_slow": (5, 400), "adx_min": (10, 60), "min_volume_ratio": (0.0, 10), "atr_stop": (0.5, 10)}

    def validate_parameters(self, p):
        if p["ema_fast"] >= p["ema_slow"]:
            raise StrategyParameterError("ema_fast must be smaller than ema_slow")

    def generate_signal(self, state: MarketState):
        p, cs = self.parameters, state.candles
        if len(cs) < max(p["ema_slow"] + 5, 2 * 14 + 1, 20):
            return None
        closes = [c.c for c in cs]
        fast, slow, price = ema(closes, p["ema_fast"])[-1], ema(closes, p["ema_slow"])[-1], closes[-1]
        trend = adx(cs, 14)
        if trend is None or trend[0] < p["adx_min"] or state.volume_ratio < p["min_volume_ratio"]:
            return None
        a, plus_di, minus_di = trend
        struct = _structure(cs)
        if fast > slow and price > fast and plus_di > minus_di and struct == 1:
            side, why = Side.BUY, ["EMA alignment up", f"ADX {a:.0f} with +DI above -DI", "Higher highs and higher lows"]
        elif fast < slow and price < fast and minus_di > plus_di and struct == -1:
            side, why = Side.SELL, ["EMA alignment down", f"ADX {a:.0f} with -DI above +DI", "Lower highs and lower lows"]
        else:
            return None
        why.append(f"Volume {state.volume_ratio:.1f}x average")
        return make_signal(self, state, side, why, p["atr_stop"])
