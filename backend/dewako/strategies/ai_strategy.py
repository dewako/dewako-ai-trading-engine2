"""AI Strategy v0.3 (experimental): trades only from the Market Intelligence layer.

It reads the normalized MarketState labels (trend, momentum, volatility, regime, volume, event timing) and
uses candles only to size the stop. The labels come from rule-based code (see market_data/state_builder.py),
so this is a rules engine over market state, not a trained model. Marked experimental, so it can never be
activated for live trading.
"""
from __future__ import annotations

from dewako.core.types import MarketState, Side
from dewako.strategies.base import Strategy, register
from dewako.strategies.helpers import make_signal


@register
class AIStrategy(Strategy):
    name = "AI Strategy"
    version = "0.3"
    description = "Acts on the Market State: trend, momentum, volatility, regime, volume and upcoming events."
    risk_profile = "Experimental"
    supported_timeframes = ["1h", "4h"]
    experimental = True
    default_parameters = {"min_volume_ratio": 1.2, "event_block_minutes": 30, "atr_stop": 2.0}
    parameter_ranges = {"min_volume_ratio": (0.5, 10), "event_block_minutes": (0, 240), "atr_stop": (0.5, 10)}

    def generate_signal(self, state: MarketState):
        p = self.parameters
        ev = state.upcoming_high_impact_event_minutes
        if ev is not None and 0 <= ev <= p["event_block_minutes"]:
            return None
        if state.regime != "TRENDING" or state.volatility == "HIGH" or state.momentum == "WEAK":
            return None
        if state.volume_ratio < p["min_volume_ratio"] or len(state.candles) < 20:
            return None
        if state.trend == "BULLISH":
            side = Side.BUY
        elif state.trend == "BEARISH":
            side = Side.SELL
        else:
            return None
        why = [f"Trend {state.trend}", f"Regime {state.regime}", f"Momentum {state.momentum}", f"Volatility {state.volatility}",
               f"Volume {state.volume_ratio:.1f}x average"]
        return make_signal(self, state, side, why, p["atr_stop"], base_confidence=0.45, per_reason=0.06)
