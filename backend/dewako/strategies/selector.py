"""Strategy selector: Manual, AI Assisted and Fully Automated modes.

The recommendation comes from fixed rules over the Market State, not from a trained model, and it is
always explained. It can only recommend a strategy. It has no access to risk limits, credentials or
orders. Even in Fully Automated mode it never switches while a position is open, never goes live on its
own, and waits out a cooldown so it cannot flip back and forth.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from dewako.core.types import MarketState
from dewako.strategies import base

CONFIDENCE_NOTE = ("Confidence is how well the current market matches this strategy's conditions. "
                   "It is not a probability of profit.")


class StrategyMode(str, Enum):
    MANUAL = "MANUAL"
    AI_ASSISTED = "AI_ASSISTED"
    FULLY_AUTOMATED = "FULLY_AUTOMATED"


class Recommendation(BaseModel):
    action: Literal["USE", "WAIT"]
    strategy: Optional[str] = None
    version: Optional[str] = None
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence_note: str = CONFIDENCE_NOTE


class Decision(BaseModel):
    action: Literal["KEEP_CURRENT", "PROPOSE", "APPLY"]
    recommendation: Optional[Recommendation] = None
    explanation: str


def recommend(state: MarketState, event_block_minutes: int = 30) -> Recommendation:
    risks: list[str] = []
    reasons = [f"Trend {state.trend}", f"Regime {state.regime}", f"Momentum {state.momentum}",
               f"Volatility {state.volatility}", f"Volume {state.volume_ratio:.1f}x average"]
    ev = state.upcoming_high_impact_event_minutes
    if ev is not None and 0 <= ev <= 240:
        risks.append(f"High-impact event in {ev} minutes")
    if state.volatility == "HIGH":
        risks.append("High volatility")
    if ev is not None and 0 <= ev <= event_block_minutes:
        return Recommendation(action="WAIT", confidence=0.6, reasons=reasons + ["Event window is too close to enter"], risks=risks)

    name, support = None, 0
    if state.regime == "TRENDING" and state.trend in ("BULLISH", "BEARISH"):
        if state.momentum == "STRONG" and state.volume_ratio >= 1.5:
            name, support = "Momentum AI", 3
            reasons.append("Strong momentum with volume expansion suits a momentum strategy")
        else:
            name, support = "Trend AI", 2 + (state.momentum == "STRONG") + (state.volume_ratio >= 1.2)
            reasons.append("Established trend suits trend following")
    elif state.regime == "RANGING":
        if state.volatility == "LOW":
            name, support = "Breakout AI", 2
            reasons.append("Quiet range: watching for an escape")
            risks.append("Breakouts from quiet ranges often fail")
        else:
            name, support = "Mean Reversion", 2 + (state.momentum == "WEAK")
            reasons.append("Range-bound market suits fading stretched moves")
    else:
        return Recommendation(action="WAIT", confidence=0.55, reasons=reasons + ["No regime clearly fits a strategy"], risks=risks)

    try:
        cls = base.latest(name)
    except KeyError:
        return Recommendation(action="WAIT", confidence=0.5, reasons=reasons, risks=risks + [f"{name} is not installed"])
    risks.append(f"Risk profile: {cls.risk_profile}")
    return Recommendation(action="USE", strategy=cls.name, version=cls.version, confidence=min(0.9, 0.45 + 0.1 * support),
                          reasons=reasons, risks=risks)


class StrategySelector:
    def __init__(self, mode: StrategyMode = StrategyMode.MANUAL, min_confidence: float = 0.7,
                 cooldown: timedelta = timedelta(hours=4)):
        self.mode, self.min_confidence, self.cooldown = mode, min_confidence, cooldown
        self._last_switch: Optional[datetime] = None

    def decide(self, state: MarketState, current: tuple[str, str], has_position: Optional[bool], bot_mode: str = "PAPER") -> Decision:
        """`has_position` None means unknown, which counts as "yes"."""
        if self.mode is StrategyMode.MANUAL:
            return Decision(action="KEEP_CURRENT", explanation="Manual mode: the strategy only changes when you change it.")
        rec = recommend(state)
        if rec.action == "WAIT":
            return Decision(action="KEEP_CURRENT", recommendation=rec, explanation="No strategy is recommended right now.")
        if (rec.strategy, rec.version) == current:
            return Decision(action="KEEP_CURRENT", recommendation=rec, explanation="The current strategy already fits.")
        if self.mode is StrategyMode.AI_ASSISTED:
            return Decision(action="PROPOSE", recommendation=rec, explanation="Recommendation only. A person must approve the switch.")
        # Fully automated: every guard must pass, otherwise it only proposes.
        blocks = []
        if has_position is None or has_position:
            blocks.append("a position is open or its state is unknown")
        if bot_mode == "LIVE":
            blocks.append("live bots are never switched automatically")
        if rec.confidence < self.min_confidence:
            blocks.append(f"confidence {rec.confidence:.2f} is below {self.min_confidence:.2f}")
        if base.latest(rec.strategy).experimental:
            blocks.append("the strategy is experimental")
        if self._last_switch and state.as_of - self._last_switch < self.cooldown:
            blocks.append("the last switch was too recent")
        if blocks:
            return Decision(action="PROPOSE", recommendation=rec, explanation="Not applied automatically: " + "; ".join(blocks) + ".")
        self._last_switch = state.as_of
        return Decision(action="APPLY", recommendation=rec, explanation="Applied automatically within the automation guards.")
