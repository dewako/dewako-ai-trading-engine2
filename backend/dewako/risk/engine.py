"""DEWAKO Risk Engine. Final authority over every signal.

Design rules enforced here:
- Limits are immutable objects. Replacing them requires a human actor; "ai" and "strategy" are refused.
- A stop price is mandatory on every entry.
- Anyone may trigger the emergency stop. Only a human can release it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from dewako.core.types import RiskDecision, Signal, utcnow
from dewako.risk.sizing import REL_TOL, SizingError, executable_entry_quantity, loss_at_stop

NON_HUMAN_ACTORS = {"ai", "strategy", "bot", "system-ai"}


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_per_trade_pct: float = Field(0.5, gt=0, le=5)
    max_position_pct: float = Field(10.0, gt=0, le=100)
    max_leverage: float = Field(5.0, ge=1)
    max_daily_loss_pct: float = Field(3.0, gt=0)
    max_drawdown_pct: float = Field(10.0, gt=0)
    max_exposure_pct: float = Field(70.0, gt=0, le=100)
    max_open_positions: int = Field(5, ge=1)
    max_correlated_risk_pct: float = Field(8.0, gt=0)
    max_consecutive_losses: int = Field(4, ge=1)
    cooldown_minutes: int = Field(30, ge=0)
    min_confidence: float = Field(0.5, ge=0, le=1)
    block_minutes_before_high_impact_event: int = Field(15, ge=0)
    require_stop_loss: bool = True


class RiskContext(BaseModel):
    equity: float
    daily_pnl_pct: float = 0.0  # negative = loss
    drawdown_pct: float = 0.0  # positive number
    open_positions: int = 0
    exposure_pct: float = 0.0
    correlated_risk_pct: float = 0.0  # risk budget already used by correlated open positions
    consecutive_losses: int = 0
    last_loss_at: Optional[datetime] = None
    minutes_to_high_impact_event: Optional[int] = None
    now: datetime = Field(default_factory=utcnow)


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None, kill_switch_active=None):
        """`kill_switch_active` is a callable backed by durable storage. If it raises, the engine counts as halted."""
        self._limits = limits or RiskLimits()
        self._halted = False
        self._external_halt = kill_switch_active
        self.audit: list[dict] = []

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def halted(self) -> bool:
        if self._external_halt is not None:
            try:
                if self._external_halt():
                    return True
            except Exception:
                return True  # cannot tell: fail closed
        return self._halted

    def update_limits(self, actor: str, new_limits: RiskLimits) -> None:
        if actor.lower() in NON_HUMAN_ACTORS:
            self.audit.append({"event": "limits_change_refused", "actor": actor})
            raise PermissionError(f"{actor!r} may not change risk limits")
        self.audit.append({"event": "limits_changed", "actor": actor})
        self._limits = new_limits

    def emergency_stop(self, actor: str, reason: str = "") -> None:
        self._halted = True
        self.audit.append({"event": "emergency_stop", "actor": actor, "reason": reason})

    def release_emergency_stop(self, actor: str) -> None:
        if actor.lower() in NON_HUMAN_ACTORS:
            self.audit.append({"event": "release_refused", "actor": actor})
            raise PermissionError(f"{actor!r} may not release the emergency stop")
        self._halted = False
        self.audit.append({"event": "emergency_stop_released", "actor": actor})

    def size_position(self, entry: float, stop: float, equity: float, leverage: float = 1.0) -> float:
        """Quantity such that hitting the stop loses risk_per_trade_pct of equity, capped by limits."""
        lim = self._limits
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0.0
        qty = equity * lim.risk_per_trade_pct / 100 / per_unit_risk
        lev = min(leverage, lim.max_leverage)
        max_notional = equity * lim.max_position_pct / 100 * lev
        return max(0.0, min(qty, max_notional / entry))

    def size_for_executable_entry(self, decision: RiskDecision, *, executable_price: float) -> RiskDecision:
        """Final sizing approval for an ENTRY, made from the EXECUTABLE price the order can really cost (the caller supplies the validated
        quote; the Risk Engine does no I/O). Returns the decision with a quantity that is never above the approved one and whose loss at the
        stop is never above the approved loss. Anything that cannot be verified returns a NOT approved decision with the reasons."""
        sig = decision.signal
        if not decision.approved:
            return decision
        try:
            qty = executable_entry_quantity(decision.quantity, reference_price=sig.entry_price, executable_price=executable_price,
                                            stop_price=sig.stop_price)
        except (SizingError, TypeError) as e:
            out = RiskDecision(approved=False, signal=sig, quantity=0.0, reasons=[str(e)])
            self.audit.append({"event": "executable_sizing", "approved": False, "reasons": out.reasons, "symbol": sig.symbol})
            return out
        out = decision.model_copy(update={"quantity": qty})
        self.audit.append({"event": "executable_sizing", "approved": True, "symbol": sig.symbol, "approved_quantity": decision.quantity,
                           "quantity": qty, "reference_price": sig.entry_price, "executable_price": executable_price})
        return out

    def final_entry_problem(self, decision: RiskDecision, *, quantity: float, executable_price: float) -> Optional[str]:
        """The last check before an order exists: the quantity actually going out (after exchange rounding) must not exceed the quantity
        the Risk Engine approved, and its loss at the stop must not exceed the approved loss. Returns the problem, or None."""
        sig = decision.signal
        try:
            # NaN and infinity make every comparison below False, which would read as "no problem": every figure must be finite first.
            for value in (quantity, executable_price, decision.quantity, sig.entry_price, sig.stop_price):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return "final order risk cannot be verified (a figure is missing or not finite)"
            if quantity <= 0 or executable_price <= 0:
                return "final order quantity is invalid"
            if quantity > decision.quantity * (1 + REL_TOL):
                return f"order quantity {quantity:g} exceeds the approved quantity {decision.quantity:g}"
            approved_loss = loss_at_stop(decision.quantity, sig.entry_price, sig.stop_price)
            if loss_at_stop(quantity, executable_price, sig.stop_price) > approved_loss * (1 + REL_TOL):
                return "loss at the stop would exceed the approved risk"
        except (TypeError, ValueError):
            return "final order risk cannot be verified"
        return None

    def approve_exit(self, signal: Signal, position_qty: float, position_side: str) -> RiskDecision:
        """Exits reduce risk, so they skip entry limits, but they still go through the Risk Engine: the signal must be the
        opposite side of a real position, never larger than it, and nothing runs while the system is halted."""
        why = []
        if self.halted:
            why.append("emergency stop active")
        if position_qty <= 0:
            why.append("no position to exit")
        if signal.side.value == position_side:
            why.append("exit signal must be the opposite side of the position")
        d = RiskDecision(approved=not why, signal=signal, quantity=position_qty if not why else 0.0, reasons=why or ["exit of an existing position"])
        self.audit.append({"event": "exit_decision", "approved": d.approved, "reasons": d.reasons, "symbol": signal.symbol})
        return d

    def evaluate(self, signal: Signal, ctx: RiskContext, leverage: float = 1.0) -> RiskDecision:
        lim, why = self._limits, []
        if self.halted:
            why.append("emergency stop active")
        if lim.require_stop_loss and signal.stop_price is None:
            why.append("stop loss is mandatory")
        if signal.entry_price is None:
            why.append("entry price missing")
        if signal.confidence < lim.min_confidence:
            why.append(f"confidence {signal.confidence:.2f} below minimum {lim.min_confidence:.2f}")
        if leverage > lim.max_leverage:
            why.append(f"leverage {leverage}x exceeds maximum {lim.max_leverage}x")
        if ctx.daily_pnl_pct <= -lim.max_daily_loss_pct:
            why.append("daily loss limit reached")
        if ctx.drawdown_pct >= lim.max_drawdown_pct:
            why.append("maximum drawdown reached")
        if ctx.open_positions >= lim.max_open_positions:
            why.append("maximum open positions reached")
        if ctx.exposure_pct >= lim.max_exposure_pct:
            why.append("maximum exposure reached")
        if ctx.consecutive_losses >= lim.max_consecutive_losses:
            why.append("maximum consecutive losses reached")
        if ctx.last_loss_at and ctx.now - ctx.last_loss_at < timedelta(minutes=lim.cooldown_minutes):
            why.append("cooldown after loss")
        if (ctx.minutes_to_high_impact_event is not None
                and 0 <= ctx.minutes_to_high_impact_event <= lim.block_minutes_before_high_impact_event):
            why.append("high-impact event imminent")
        if ctx.correlated_risk_pct + lim.risk_per_trade_pct > lim.max_correlated_risk_pct:
            why.append("correlated exposure limit would be exceeded")

        qty = 0.0
        if not why and signal.entry_price is not None and signal.stop_price is not None:
            qty = self.size_position(signal.entry_price, signal.stop_price, ctx.equity, leverage)
            if qty <= 0:
                why.append("computed position size is zero")

        decision = RiskDecision(approved=not why, signal=signal, quantity=qty if not why else 0.0,
                                reasons=why or ["within all limits"])
        self.audit.append({"event": "decision", "approved": decision.approved, "reasons": decision.reasons,
                           "symbol": signal.symbol, "strategy": signal.strategy})
        return decision
