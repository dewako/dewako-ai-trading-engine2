"""Versions that finish the entry-only strategies with the plain opposite-of-entry exit they were missing.

Trend AI 2.1/2.2, Momentum AI 1.4 and AI Strategy 0.3 set an ATR stop and nothing else: no take-profit, no trailing stop, no exit signal, so every
trade ended at its initial stop and none could book a profit (found in the first real-data run, docs/REAL_DATA_FIRST_RUN_2026-11-07.md).
The originals stay exactly as they were (backtest evidence refers to them); each fix is a NEW version with ONE added exit and no new parameter:

  Trend AI 2.3      = 2.2 + exit when the fast EMA is no longer above the slow EMA (the alignment the entry required is gone)
  Momentum AI 1.5   = 1.4 + exit when the MACD histogram turns negative (the momentum the entry required is gone)
  AI Strategy 0.4   = 0.3 + exit when the market state trend is no longer BULLISH

The exits were chosen as the logical inverse of each entry BEFORE any run with them; they were not searched for or tuned on data.
"""
from __future__ import annotations

from typing import Any, Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.ai_strategy import AIStrategy
from dewako.strategies.base import register
from dewako.strategies.indicators import ema, macd_series
from dewako.strategies.momentum_ai import MomentumAI
from dewako.strategies.trend_ai_v22 import TrendAIv22


def _exit(strategy, state: MarketState, why: str) -> Signal:
    return Signal(symbol=state.symbol, side=Side.SELL, confidence=0.9, strategy=strategy.name, strategy_version=strategy.version,
                  reason=[why], entry_price=state.candles[-1].c)


@register
class TrendAIv23(TrendAIv22):
    version = "2.3"
    candidate = True                      # research version: not the default until a human promotes it
    description = TrendAIv22.description + " Exits when the fast EMA falls below the slow EMA."

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        p, closes = self.parameters, [c.c for c in state.candles]
        if len(closes) < p["ema_slow"] + 5:
            return None
        if ema(closes, p["ema_fast"])[-1] < ema(closes, p["ema_slow"])[-1]:
            return _exit(self, state, "Trend alignment lost: fast EMA below slow EMA")
        return None


@register
class MomentumAIv15(MomentumAI):
    version = "1.5"
    candidate = True
    description = MomentumAI.description + " Exits when the MACD histogram turns negative."

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        p, closes = self.parameters, [c.c for c in state.candles]
        if len(closes) < p["macd_slow"] + p["macd_signal"] + 5:
            return None
        _line, _sig, hist = macd_series(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])
        if hist[-1] < 0:
            return _exit(self, state, "Momentum lost: MACD histogram negative")
        return None


@register
class AIStrategyv04(AIStrategy):
    version = "0.4"
    candidate = True
    description = AIStrategy.description + " Exits when the market state trend is no longer bullish."

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        if state.trend != "BULLISH":
            return _exit(self, state, f"Trend no longer bullish ({state.trend})")
        return None
