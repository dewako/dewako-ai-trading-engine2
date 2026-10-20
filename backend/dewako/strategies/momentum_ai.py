"""Momentum AI v1.4: RSI + MACD + volume + rate of change. Rule-based, no learning."""
from __future__ import annotations

from dewako.core.types import MarketState, Side
from dewako.strategies.base import Strategy, StrategyParameterError, register
from dewako.strategies.helpers import make_signal
from dewako.strategies.indicators import macd_series, roc, rsi


@register
class MomentumAI(Strategy):
    name = "Momentum AI"
    version = "1.4"
    description = "Buys strength when MACD momentum is accelerating, RSI is strong but not stretched, and volume confirms."
    risk_profile = "Medium-High"
    supported_timeframes = ["5m", "15m", "1h"]
    default_parameters = {"rsi_len": 14, "rsi_long_max": 75, "rsi_short_min": 25, "macd_fast": 12, "macd_slow": 26,
                          "macd_signal": 9, "roc_len": 10, "vol_mult": 1.5, "atr_stop": 2.0}
    parameter_ranges = {"rsi_len": (2, 100), "rsi_long_max": (55, 95), "rsi_short_min": (5, 45), "macd_fast": (2, 50),
                        "macd_slow": (5, 120), "macd_signal": (2, 50), "roc_len": (2, 100), "vol_mult": (1.0, 10), "atr_stop": (0.5, 10)}

    def validate_parameters(self, p):
        if p["macd_fast"] >= p["macd_slow"]:
            raise StrategyParameterError("macd_fast must be smaller than macd_slow")

    def generate_signal(self, state: MarketState):
        p = self.parameters
        closes = [c.c for c in state.candles]
        if len(closes) < p["macd_slow"] + p["macd_signal"] + 5:
            return None
        if state.volume_ratio < p["vol_mult"]:
            return None
        line, sig, hist = macd_series(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])
        r, mom = rsi(closes, p["rsi_len"]), roc(closes, p["roc_len"])
        if hist[-1] > 0 and hist[-1] > hist[-2] and line[-1] > sig[-1] and 50 <= r <= p["rsi_long_max"] and mom > 0:
            side, why = Side.BUY, [f"MACD histogram positive and rising", f"RSI {r:.0f} strong but below {p['rsi_long_max']}",
                                   f"Rate of change {mom * 100:.1f}% over {p['roc_len']} bars"]
        elif hist[-1] < 0 and hist[-1] < hist[-2] and line[-1] < sig[-1] and p["rsi_short_min"] <= r <= 50 and mom < 0:
            side, why = Side.SELL, [f"MACD histogram negative and falling", f"RSI {r:.0f} weak but above {p['rsi_short_min']}",
                                    f"Rate of change {mom * 100:.1f}% over {p['roc_len']} bars"]
        else:
            return None
        why.append(f"Volume {state.volume_ratio:.1f}x average")
        return make_signal(self, state, side, why, p["atr_stop"])
