"""Dewako ETH 1.0: the owner-approved addition of the research strategy "Dewako 1.0" (research/dewako_strategy_2026-11-08), ETH only.

Same rules and numbers as the research plugin. It passed the promotion gate on ETHUSDT 4h, 2020-01..2026-10, with --variants-tried 76; it
FAILED on BTC, so BTC is not a supported market. The pass is fragile (the 6.8-year result is negative without its 3 best trades; one trade
decides the hold-out), so it is a candidate: never the default, never live, until the owner promotes it after forward paper results.
"""
from __future__ import annotations

from typing import Any, Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.base import Strategy, register
from dewako.strategies.helpers import make_exit, make_signal
from dewako.strategies.indicators import donchian, sma


@register
class DewakoETH(Strategy):
    name = "Dewako ETH"
    version = "1.0"
    candidate = True
    description = ("Long-only trend follower for ETH on 4h spot: buys a 20-bar breakout only while price is above its 200-day average; exits "
                   "on a 10-bar low or when the 200-day trend is lost. Textbook (Turtle System 1) settings, untuned. Needs 1,200 candles of "
                   "history (lookback >= 1200).")
    risk_profile = "Medium"
    supported_markets = ["ETH/USDT"]
    supported_timeframes = ["4h"]
    default_parameters = {"regime_bars": 1200, "entry_bars": 20, "exit_bars": 10, "atr_stop": 2.0}
    parameter_ranges = {"regime_bars": (50, 2000), "entry_bars": (5, 200), "exit_bars": (2, 200), "atr_stop": (0.5, 10)}

    def _ready(self, cs) -> bool:
        p = self.parameters
        return len(cs) >= max(p["regime_bars"], p["entry_bars"] + 1, p["exit_bars"] + 1, 15)

    def generate_signal(self, state: MarketState) -> Optional[Signal]:
        p, cs = self.parameters, state.candles
        if not self._ready(cs):
            return None
        price = cs[-1].c
        trend = sma([c.c for c in cs], p["regime_bars"])
        high, _ = donchian(cs, p["entry_bars"])
        if price > trend and price > high:
            return make_signal(self, state, Side.BUY, [f"Close above the {p['regime_bars']}-bar average (long-term uptrend)",
                                                       f"Close above the previous {p['entry_bars']}-bar high (breakout)"], p["atr_stop"])
        return None

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        p, cs = self.parameters, state.candles
        if not self._ready(cs):
            return None
        price = cs[-1].c
        _, low = donchian(cs, p["exit_bars"])
        why = (f"Close below the previous {p['exit_bars']}-bar low" if price < low else
               f"Close below the {p['regime_bars']}-bar average (uptrend lost)" if price < sma([c.c for c in cs], p["regime_bars"]) else None)
        return None if why is None else make_exit(self, state, why)
