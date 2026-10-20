"""Mean Reversion v1.2: Bollinger Bands + RSI + VWAP, switched off when ADX says the market is trending."""
from __future__ import annotations

from dewako.core.types import MarketState, Side
from dewako.strategies.base import Strategy, register
from dewako.strategies.helpers import make_signal
from dewako.strategies.indicators import adx, bollinger, rsi, vwap, zscore


@register
class MeanReversion(Strategy):
    name = "Mean Reversion"
    version = "1.2"
    description = "Fades stretched moves back toward the average in range-bound markets. Stands aside when ADX shows a strong trend."
    risk_profile = "Low"
    supported_timeframes = ["30m", "1h", "4h"]
    default_parameters = {"bb_len": 20, "bb_dev": 2.0, "rsi_len": 14, "rsi_low": 35, "rsi_high": 65, "vwap_len": 48,
                          "adx_max": 30, "atr_stop": 2.5}
    parameter_ranges = {"bb_len": (5, 200), "bb_dev": (1.0, 4.0), "rsi_len": (2, 100), "rsi_low": (5, 45), "rsi_high": (55, 95),
                        "vwap_len": (5, 500), "adx_max": (10, 60), "atr_stop": (0.5, 10)}

    def generate_signal(self, state: MarketState):
        p, cs = self.parameters, state.candles
        closes = [c.c for c in cs]
        need = max(p["bb_len"], p["vwap_len"], 2 * 14 + 1, p["rsi_len"] + 2)
        if len(cs) < need:
            return None
        trend = adx(cs, 14)
        if trend is None or trend[0] > p["adx_max"]:
            return None  # trending market: mean reversion loses here, so do nothing
        mid, upper, lower = bollinger(closes, p["bb_len"], p["bb_dev"])
        r, vw, price, z = rsi(closes, p["rsi_len"]), vwap(cs, p["vwap_len"]), closes[-1], zscore(closes, p["bb_len"])
        if price < lower and r < p["rsi_low"] and price < vw:
            return make_signal(self, state, Side.BUY, [f"Close below lower Bollinger band (z-score {z:.1f})", f"RSI {r:.0f} oversold",
                                                       "Price below VWAP", f"ADX {trend[0]:.0f}: no strong trend"],
                               p["atr_stop"], take_profit_price=mid)
        if price > upper and r > p["rsi_high"] and price > vw:
            return make_signal(self, state, Side.SELL, [f"Close above upper Bollinger band (z-score {z:.1f})", f"RSI {r:.0f} overbought",
                                                        "Price above VWAP", f"ADX {trend[0]:.0f}: no strong trend"],
                               p["atr_stop"], take_profit_price=mid)
        return None
