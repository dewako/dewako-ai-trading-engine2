"""Server-side bot tick: MarketState -> strategy -> signal -> Risk Engine -> Execution Engine.

No browser is involved. Every stage is persisted, so a tick that dies half way is safe to run again: the same bar yields
the same signal id, and the execution intent's UNIQUE constraint stops a second order.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

from dewako.core.types import MarketState, utcnow
from dewako.execution.engine import ExecutionEngine
from dewako.market_data.freshness import CandleFreshnessError, validate_candles
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import now_iso
from dewako.risk.engine import RiskEngine
from dewako.risk.state import RiskStateStore
from dewako.strategies import base
from dewako.strategies.signal_engine import SignalEngine

log = logging.getLogger("dewako.bots")


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
    bar: str = ""     # open time of the last candle the tick evaluated ("" if no market data was obtained)


class BotRunner:
    def __init__(self, engine: ExecutionEngine, risk: RiskEngine, risk_state: RiskStateStore,
                 state_provider: Callable[[str, str], Awaitable[MarketState]], audit: Optional[AuditLog] = None,
                 clock: Callable[[], datetime] = utcnow):
        self.engine, self.risk, self.risk_state = engine, risk, risk_state
        self.state_provider = state_provider
        self.audit = audit or engine.audit
        self.clock = clock
        self._sig_engines: dict[str, SignalEngine] = {}

    async def tick(self, bot: BotConfig) -> TickResult:
        self._bar = ""
        res = await self._tick(bot)
        res.bar = res.bar or self._bar
        return res

    async def _tick(self, bot: BotConfig) -> TickResult:
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
        try:
            last_t = getattr(state.candles[-1], "t", None) if state.candles else None
            self._bar = last_t.isoformat() if hasattr(last_t, "isoformat") else ""
            if state.symbol != bot.symbol:
                raise CandleFreshnessError("market-state symbol does not match the bot symbol")
            validate_candles(state.candles, bot.timeframe, self.clock())
        except Exception as e:
            reason = f"market data could not be validated ({type(e).__name__})"
            self.audit.record("MARKET_DATA_REJECTED", "BLOCKED", cid, resource=f"bot:{bot.bot_id}", metadata={"reason": reason})
            return TickResult("BLOCKED", reason)
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
                ctx = self.risk_state.context(scope, bot.bot_id, bot.environment, symbol=bot.symbol)
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


class TickMonitor:
    """Makes bot ticks observable. The worker discards nothing silently any more:
    * one INFO log line when a bot's outcome or evaluated bar changes (so every new candle leaves exactly one line);
    * a WARNING line for every change into a failing outcome (ERROR, BLOCKED), and once when a bot reaches `fail_after`
      consecutive failing ticks; `failing()` then names it, and the bot worker raises so the system shows DEGRADED.
    It only reports: it never changes a decision, a limit or an order."""
    FAILING = ("ERROR", "BLOCKED")

    def __init__(self, fail_after: int = 3, logger: logging.Logger = log):
        self.fail_after, self.log = fail_after, logger
        self._last: dict[str, tuple] = {}
        self._fails: dict[str, int] = {}

    def observe(self, bot_id: str, res: TickResult) -> None:
        key = (res.bar, res.outcome, res.detail)
        failing = res.outcome in self.FAILING
        n = self._fails[bot_id] = (self._fails.get(bot_id, 0) + 1) if failing else 0
        if key != self._last.get(bot_id):
            self.log.log(logging.WARNING if failing else logging.INFO, "bot %s bar %s: %s%s", bot_id, res.bar or "-", res.outcome,
                         f" ({res.detail})" if res.detail else "")
        if failing and n == self.fail_after:
            self.log.warning("bot %s: %d consecutive failing ticks (last: %s %s); system health is DEGRADED until a tick succeeds",
                             bot_id, n, res.outcome, res.detail)
        self._last[bot_id] = key

    def failing(self) -> list[str]:
        return sorted(b for b, n in self._fails.items() if n >= self.fail_after)

    def forget_except(self, bot_ids) -> None:
        """A disabled or deleted bot no longer counts as failing."""
        keep = set(bot_ids)
        for d in (self._last, self._fails):
            for b in [b for b in d if b not in keep]:
                del d[b]
