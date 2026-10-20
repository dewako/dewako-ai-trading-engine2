"""Server-side bot tick: MarketState -> strategy -> signal -> Risk Engine -> Execution Engine.

No browser is involved. Every stage is persisted, so a tick that dies half way is safe to run again: the same bar yields
the same signal id, and the execution intent's UNIQUE constraint stops a second order.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from dewako.core.types import MarketState
from dewako.execution.engine import ExecutionEngine
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import now_iso
from dewako.risk.engine import RiskEngine
from dewako.risk.state import RiskStateStore
from dewako.strategies import base
from dewako.strategies.signal_engine import SignalEngine


@dataclass
class BotConfig:
    bot_id: str
    strategy: str
    version: str
    symbol: str
    timeframe: str
    environment: str  # PAPER or LIVE
    exchange: str
    risk_scope: str = ""


@dataclass
class TickResult:
    outcome: str  # NO_SIGNAL | HALTED | RISK_REJECTED | EXECUTED | DUPLICATE | BLOCKED | ERROR ...
    detail: str = ""
    signal_id: str = ""
    order_id: Optional[int] = None


class BotRunner:
    def __init__(self, engine: ExecutionEngine, risk: RiskEngine, risk_state: RiskStateStore,
                 state_provider: Callable[[str, str], Awaitable[MarketState]], audit: Optional[AuditLog] = None):
        self.engine, self.risk, self.risk_state = engine, risk, risk_state
        self.state_provider = state_provider
        self.audit = audit or engine.audit
        self._sig_engines: dict[str, SignalEngine] = {}

    async def tick(self, bot: BotConfig) -> TickResult:
        cid = uuid.uuid4().hex
        if bot.environment == "LIVE" and self.engine.safety.kill_switch_active():
            return TickResult("HALTED", "kill switch active or safety state unknown")
        try:
            cls = base.get(bot.strategy, bot.version)
            if bot.environment == "LIVE" and cls.experimental:
                return TickResult("BLOCKED", "experimental strategies cannot run live")
            state = await self.state_provider(bot.symbol, bot.timeframe)
        except Exception as e:
            return TickResult("ERROR", f"{type(e).__name__}")  # no data / bad data -> no trade
        se = self._sig_engines.setdefault(bot.bot_id, SignalEngine(cls()))
        pos = next(iter(self.engine.positions.active(bot.environment, bot.bot_id)), None)
        res = se.evaluate(state, {"side": pos["side"], "quantity": pos["quantity"], "entry_price": pos["average_price"]} if pos else None)
        if res.signal is None:
            if res.reason.startswith(("rejected", "strategy error")):
                self.audit.record("SIGNAL_REJECTED", "REJECTED", cid, resource=f"bot:{bot.bot_id}", metadata={"reason": res.reason})
            return TickResult("NO_SIGNAL", res.reason)
        sig = res.signal
        self._persist_signal(bot, sig, "NEW", None, cid)
        scope = bot.risk_scope or bot.environment
        if res.kind == "MANAGE" and pos:
            decision, intent = self.risk.approve_exit(sig, pos["quantity"], pos["side"]), "EXIT"
        elif pos:
            return TickResult("NO_SIGNAL", "position open; entries are not stacked", sig.signal_id)
        else:
            try:
                ctx = self.risk_state.context(scope, bot.bot_id, bot.environment)
            except KeyError:  # no persisted risk state: refuse rather than invent numbers
                self._persist_signal(bot, sig, "REJECTED", "risk state not initialised", cid)
                return TickResult("BLOCKED", "risk state not initialised for this scope", sig.signal_id)
            decision, intent = self.risk.evaluate(sig, ctx), "ENTRY"
        if not decision.approved:
            self._persist_signal(bot, sig, "REJECTED", "; ".join(decision.reasons), cid)
            with self.engine.db.tx() as c:
                self.engine.record_decision(c, decision, bot.bot_id)
            self.audit.record("RISK_REJECTED", "REJECTED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": decision.reasons})
            return TickResult("RISK_REJECTED", "; ".join(decision.reasons), sig.signal_id)
        r = await self.engine.execute(decision, bot_id=bot.bot_id, environment=bot.environment, exchange=bot.exchange,
                                      intent=intent, correlation_id=cid, risk_scope=scope)
        return TickResult(r.outcome, r.detail, sig.signal_id, r.order_id)

    def _persist_signal(self, bot: BotConfig, sig, status: str, reason: Optional[str], cid: str) -> None:
        with self.engine.db.tx() as c:
            new = c.execute("INSERT INTO signals(signal_id,bot_id,strategy_id,strategy_version,symbol,timeframe,side,price,confidence,timestamp,status,rejection_reason,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO NOTHING",
                            (sig.signal_id, bot.bot_id, sig.strategy, sig.strategy_version, sig.symbol, bot.timeframe, sig.side.value,
                             sig.entry_price, sig.confidence, sig.timestamp.isoformat(), status, reason, now_iso())).rowcount
            if status == "REJECTED":
                c.execute("UPDATE signals SET status='REJECTED', rejection_reason=? WHERE signal_id=? AND status='NEW'", (reason, sig.signal_id))
            if new:
                self.audit.record("SIGNAL_CREATED", "OK", cid, resource=f"signal:{sig.signal_id}", metadata={"strategy": sig.strategy}, conn=c)
