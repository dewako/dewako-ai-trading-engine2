"""Breakout AI v1.8: range escape confirmed by volume and expanding volatility."""
from __future__ import annotations

from dewako.core.types import MarketState, Side
from dewako.strategies.base import Strategy, register
from dewako.strategies.helpers import make_signal
from dewako.strategies.indicators import atr, donchian


@register
class BreakoutAI(Strategy):
    name = "Breakout AI"
    version = "1.8"
    description = "Trades a close beyond the previous range when volume is high and volatility is expanding."
    risk_profile = "High"
    supported_timeframes = ["15m", "30m", "1h"]
    default_parameters = {"lookback": 48, "atr_len": 14, "vol_mult": 1.8, "min_break_atr": 0.1, "atr_expansion": 1.1,
                          "atr_stop": 2.0, "take_profit_rr": 2.0}
    parameter_ranges = {"lookback": (10, 500), "atr_len": (2, 100), "vol_mult": (1.0, 10), "min_break_atr": (0.0, 3.0),
                        "atr_expansion": (1.0, 5.0), "atr_stop": (0.5, 10), "take_profit_rr": (0.5, 10)}

    def generate_signal(self, state: MarketState):
        p, cs = self.parameters, state.candles
        n = p["atr_len"]
        if len(cs) < p["lookback"] + n + 25:
            return None
        if state.volume_ratio < p["vol_mult"]:
            return None
        high, low = donchian(cs, p["lookback"])
        price, now_atr = cs[-1].c, atr(cs, n)
        before = atr(cs[:-20], n)
        if before <= 0 or now_atr / before < p["atr_expansion"]:
            return None  # volatility is not expanding: likely a fake-out inside a quiet range
        margin = p["min_break_atr"] * now_atr
        if price > high + margin:
            side, why = Side.BUY, [f"Close above the prior {p['lookback']}-bar high"]
        elif price < low - margin:
            side, why = Side.SELL, [f"Close below the prior {p['lookback']}-bar low"]
        else:
            return None
        why += [f"Volume {state.volume_ratio:.1f}x average", f"ATR up {now_atr / before:.1f}x versus 20 bars ago"]
        return make_signal(self, state, side, why, p["atr_stop"], atr_len=n, take_profit_rr=p["take_profit_rr"])
