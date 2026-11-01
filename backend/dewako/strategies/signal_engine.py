"""Signal engine: runs one strategy over a MarketState and returns a validated, deterministic signal.

A signal is an opinion. Nothing here talks to an exchange or sizes an order: the Risk Engine decides
whether a signal may become an order. Failures never produce a signal (fail closed).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Optional

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.base import Strategy


@dataclass
class SignalResult:
    signal: Optional[Signal] = None
    reason: str = ""          # why there is no signal (empty when there is one)
    duplicate: bool = False   # True when this bar already produced this signal
    kind: str = "ENTRY"       # ENTRY or MANAGE (an open position was passed in)


def validate_signal(sig: Signal, strategy: Strategy, state: MarketState) -> Optional[str]:
    """Return a reason if the signal is not acceptable, else None."""
    if sig.symbol != state.symbol:
        return f"symbol {sig.symbol} does not match market {state.symbol}"
    if sig.strategy != strategy.name or sig.strategy_version != strategy.version:
        return "signal does not carry the strategy name and version that produced it"
    if not sig.reason:
        return "signal has no reasons"
    for label, v in (("entry", sig.entry_price), ("stop", sig.stop_price), ("take profit", sig.take_profit_price)):
        if v is not None and (not math.isfinite(v) or v <= 0):
            return f"{label} price is not a positive finite number"
    if not math.isfinite(sig.confidence):
        return "confidence is not finite"
    if sig.entry_price is not None and sig.stop_price is not None:
        if sig.side is Side.BUY and sig.stop_price >= sig.entry_price:
            return "stop must be below entry for a BUY"
        if sig.side is Side.SELL and sig.stop_price <= sig.entry_price:
            return "stop must be above entry for a SELL"
    if sig.entry_price is not None and state.candles:
        reference = state.candles[-1].c
        if not math.isclose(sig.entry_price, reference, rel_tol=1e-12, abs_tol=0.0):
            return "entry price must match the newest validated candle close"
    if sig.entry_price is not None and sig.take_profit_price is not None:
        if sig.side is Side.BUY and sig.take_profit_price <= sig.entry_price:
            return "take profit must be above entry for a BUY"
        if sig.side is Side.SELL and sig.take_profit_price >= sig.entry_price:
            return "take profit must be below entry for a SELL"
    return None


class SignalEngine:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self._seen: set[str] = set()
        self.rejections: list[dict[str, Any]] = []

    @staticmethod
    def _id(strategy: Strategy, state: MarketState, side: Side, kind: str) -> str:
        last = state.candles[-1].t.isoformat() if state.candles else ""
        raw = f"{kind}|{strategy.name}|{strategy.version}|{state.symbol}|{side.value}|{last}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def evaluate(self, state: MarketState, position: Optional[dict[str, Any]] = None) -> SignalResult:
        """With an open position the strategy may only manage it (exit/adjust). It never opens a second entry."""
        kind = "MANAGE" if position else "ENTRY"
        try:
            sig = (self.strategy.manage_position(state, position) if position else self.strategy.generate_signal(state))
        except Exception as e:  # a crashing strategy must not take the engine down or leak a half-built signal
            self._reject(state, f"strategy error: {type(e).__name__}")
            return SignalResult(reason=f"strategy error: {type(e).__name__}", kind=kind)
        if sig is None:
            return SignalResult(reason="no setup", kind=kind)
        problem = validate_signal(sig, self.strategy, state)
        if problem:
            self._reject(state, problem)
            return SignalResult(reason=f"rejected: {problem}", kind=kind)
        sid = self._id(self.strategy, state, sig.side, kind)
        if sid in self._seen:
            return SignalResult(reason="duplicate: this bar already produced this signal", duplicate=True, kind=kind)
        self._seen.add(sid)
        return SignalResult(signal=sig.model_copy(update={"timestamp": state.as_of, "signal_id": sid}), kind=kind)

    def _reject(self, state: MarketState, why: str) -> None:
        self.rejections.append({"strategy": self.strategy.name, "version": self.strategy.version,
                                "symbol": state.symbol, "as_of": state.as_of.isoformat(), "reason": why})
