"""Dewako Flow 1.0 — see PROTOCOL.md. Loaded only with --plugin; not part of the strategy library."""
from __future__ import annotations

from typing import Any, Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.base import Strategy, register
from dewako.strategies.helpers import make_exit, make_signal
from dewako.strategies.indicators import atr, sma, vwap


def _true_range(cs, i) -> float:
    c, prev = cs[i], cs[i - 1].c
    return max(c.h - c.l, abs(c.h - prev), abs(c.l - prev))


def shock_in_last(cs, bars: int, mult: float, atr_len: int = 14) -> bool:
    """True if any of the last `bars` candles has a true range above mult x the ATR of the atr_len candles before it."""
    n = len(cs)
    for i in range(n - bars, n):
        before = cs[:i]
        if len(before) < atr_len + 1:
            continue
        if _true_range(cs, i) > mult * atr(before, atr_len):
            return True
    return False


@register
class DewakoFlow(Strategy):
    name = "Dewako Flow"
    version = "1.0"
    candidate = True
    directions = ("LONG",)
    description = ("Long-only 4h spot: buys a close back above the 5-day VWAP on above-average volume, only above the 200-day average, "
                   "never within 24h of a shock bar or on a rejection wick; exits 1 ATR below VWAP or when the 200-day trend is lost.")
    risk_profile = "Medium"
    supported_timeframes = ["4h"]
    default_parameters = {"regime_bars": 1200, "vwap_bars": 30, "volume_bars": 20, "shock_atr": 3.0, "shock_cooldown_bars": 6,
                          "max_upper_wick": 0.5, "atr_stop": 2.0, "exit_atr": 1.0}
    parameter_ranges = {"regime_bars": (50, 2000), "vwap_bars": (5, 500), "volume_bars": (5, 200), "shock_atr": (1.5, 10),
                        "shock_cooldown_bars": (0, 100), "max_upper_wick": (0.1, 1.0), "atr_stop": (0.5, 10), "exit_atr": (0, 10)}

    def _ready(self, cs) -> bool:
        p = self.parameters
        return len(cs) >= max(p["regime_bars"], p["vwap_bars"] + 1, p["volume_bars"] + 1, p["shock_cooldown_bars"] + 16)

    def generate_signal(self, state: MarketState) -> Optional[Signal]:
        p, cs = self.parameters, state.candles
        if not self._ready(cs):
            return None
        last, prev = cs[-1], cs[-2]
        if last.c <= sma([c.c for c in cs], p["regime_bars"]):
            return None
        if not (last.c > vwap(cs, p["vwap_bars"]) and prev.c <= vwap(cs[:-1], p["vwap_bars"])):
            return None
        if last.v <= sum(c.v for c in cs[-p["volume_bars"] - 1:-1]) / p["volume_bars"]:
            return None
        rng = last.h - last.l
        if rng > 0 and (last.h - max(last.o, last.c)) / rng > p["max_upper_wick"]:
            return None
        if shock_in_last(cs, p["shock_cooldown_bars"], p["shock_atr"]):
            return None
        return make_signal(self, state, Side.BUY, [f"Close above the {p['regime_bars']}-bar average (long-term uptrend)",
                                                   f"Close back above the {p['vwap_bars']}-bar VWAP on above-average volume",
                                                   "No shock bar in the last 24h and no rejection wick"], p["atr_stop"])

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        p, cs = self.parameters, state.candles
        if not self._ready(cs):
            return None
        price = cs[-1].c
        if price < vwap(cs, p["vwap_bars"]) - p["exit_atr"] * atr(cs, 14):
            return make_exit(self, state, f"Close more than {p['exit_atr']} ATR below the {p['vwap_bars']}-bar VWAP")
        if price < sma([c.c for c in cs], p["regime_bars"]):
            return make_exit(self, state, f"Close below the {p['regime_bars']}-bar average (uptrend lost)")
        return None
