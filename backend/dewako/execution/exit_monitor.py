"""Exit monitor: enforces each position's persisted stop-loss, take-profit and maximum holding time.

A stop price stored on a position protects nothing by itself. This worker compares every open position with a validated, fresh,
executable quote on every cycle and routes any exit through the Risk Engine (`approve_exit`) and the Execution Engine
(`execute(intent="EXIT")`). It never sends an order itself.

If protection cannot be verified for any position (no adapter, no valid fresh quote, no stop on the position, a position in an uncertain
status, or an exit that could not be placed) the monitor opens an EXIT_PROTECTION_UNVERIFIED incident. While that incident is open the
Execution Engine refuses new entries. The incident is closed again only after `clean_cycles_to_clear` consecutive cycles in which every
position was verified. The worker wrapper raises when anything is unverified so the supervisor reports the worker unhealthy.

Gap behaviour: an exit is a market order at the next executable price. A gap through the stop fills at the gapped market, never at the
stop price; the stop is a trigger, not a guaranteed fill.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from dewako.core.types import Side, Signal, utcnow
from dewako.execution import reservations as rsv
from dewako.execution.validation import QuoteGuardError, validate_executable_quote
from dewako.persistence.db import now_iso

log = logging.getLogger("dewako.exit_monitor")

INCIDENT_KIND = "EXIT_PROTECTION_UNVERIFIED"
_PENDING_STATES = ("CREATED", "SUBMITTING", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN", "RECONCILING")


@dataclass
class MonitorReport:
    checked: int = 0
    exits: list = field(default_factory=list)          # (position_id, reason, outcome)
    unverified: list = field(default_factory=list)     # (position_id, why)
    pending: list = field(default_factory=list)        # position ids with an exit already in flight


class ExitMonitor:
    def __init__(self, engine, risk, *, max_hold_seconds: Optional[float] = None, clock: Callable[[], datetime] = utcnow,
                 clean_cycles_to_clear: int = 2):
        self.engine, self.risk = engine, risk
        self.max_hold_seconds = max_hold_seconds
        self.clock = clock
        self.clean_cycles_to_clear = clean_cycles_to_clear
        self._clean = 0
        self.last_report: Optional[MonitorReport] = None

    # ------------------------------------------------------------------ one cycle
    async def run_once(self) -> MonitorReport:
        rep = MonitorReport()
        with self.engine.db.read() as c:
            positions = [dict(r) for r in c.execute("SELECT * FROM positions WHERE status<>'CLOSED' ORDER BY id")]
        for p in positions:
            rep.checked += 1
            try:
                await self._check(p, rep)
            except Exception as e:      # one bad position must never stop the others being protected
                rep.unverified.append((p["id"], f"unexpected {type(e).__name__}"))
        for pid, why in rep.unverified:
            p = next(x for x in positions if x["id"] == pid)
            self.engine.open_incident(INCIDENT_KIND, f"position {pid} {p['symbol']}: {why}", severity="HIGH",
                                      scope=rsv.scope_of(p["environment"], p["exchange"]), correlation_id=f"exitmon:{pid}")
        if rep.unverified:
            self._clean = 0
        else:
            self._clean += 1
            if self._clean >= self.clean_cycles_to_clear:
                self._resolve_incidents()
        self.last_report = rep
        return rep

    async def worker_tick(self) -> None:
        """Supervisor entry point: raises when any position is not verifiably protected, so the worker never looks healthy then."""
        rep = await self.run_once()
        if rep.unverified:
            raise RuntimeError(f"{len(rep.unverified)} position(s) without verified exit protection")

    # ------------------------------------------------------------------ one position
    async def _check(self, p: dict, rep: MonitorReport) -> None:
        pid = p["id"]
        if p["status"] != "OPEN":
            rep.unverified.append((pid, f"position status is {p['status']}; protection cannot be verified until it is reconciled"))
            return
        if p["side"] != "BUY":
            rep.unverified.append((pid, "only long positions are supported"))
            return
        if p["stop_loss"] is None:
            rep.unverified.append((pid, "position has no stop_loss recorded"))
            return
        adapter = self.engine.adapters.get(p["exchange"])
        if adapter is None:
            rep.unverified.append((pid, f"no adapter for {p['exchange']}"))
            return
        try:
            quote = await adapter.get_ticker(p["symbol"])
            bid = quote.get("bid") if isinstance(quote, dict) else None
            check = validate_executable_quote(quote, side="SELL", reference_price=bid, now=self.clock(), config=self.engine.quote_guard)
        except (QuoteGuardError, TypeError, ValueError) as e:
            rep.unverified.append((pid, f"no valid fresh quote ({e})"))
            return
        except Exception as e:
            rep.unverified.append((pid, f"quote unavailable ({type(e).__name__})"))
            return
        price = check.executable_price
        reason = None
        if price <= p["stop_loss"]:
            reason = "STOP"
        elif p["take_profit"] is not None and price >= p["take_profit"]:
            reason = "TAKE_PROFIT"
        elif self.max_hold_seconds is not None and self._held_seconds(p) >= self.max_hold_seconds:
            reason = "MAX_HOLD"
        if reason is None:
            return
        if self._exit_in_flight(p):
            rep.pending.append(pid)
            return
        sig = Signal(symbol=p["symbol"], side=Side.SELL, confidence=1.0, strategy="ExitMonitor", strategy_version="1",
                     signal_id=self._signal_id(pid, reason), reason=[f"{reason} trigger at {price:g}"], entry_price=price,
                     timestamp=self.clock())
        decision = self.risk.approve_exit(sig, p["quantity"], p["side"])
        if not decision.approved:
            rep.unverified.append((pid, "exit refused by risk engine: " + "; ".join(decision.reasons)))
            return
        res = await self.engine.execute(decision, bot_id=p["bot_id"], environment=p["environment"], exchange=p["exchange"],
                                        intent="EXIT", correlation_id=uuid.uuid4().hex, risk_scope=None)
        rep.exits.append((pid, reason, res.outcome))
        if res.outcome in ("BLOCKED", "REJECTED", "FAILED"):
            rep.unverified.append((pid, f"{reason} exit could not be placed: {res.outcome} {res.detail[:120]}"))

    # ------------------------------------------------------------------ helpers
    def _held_seconds(self, p: dict) -> float:
        opened = p["opened_at"]
        opened = datetime.fromisoformat(opened) if isinstance(opened, str) else opened
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (self.clock() - opened).total_seconds()

    def _exit_in_flight(self, p: dict) -> bool:
        marks = ",".join("?" * len(_PENDING_STATES))
        with self.engine.db.read() as c:
            n = c.execute(f"SELECT COUNT(*) FROM orders WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND side='SELL' "
                          f"AND state IN ({marks})", (p["bot_id"], p["exchange"], p["environment"], p["symbol"], *_PENDING_STATES)).fetchone()[0]
        return n > 0

    def _signal_id(self, pid: int, reason: str) -> str:
        """Deterministic per position, reason and attempt: concurrent monitors collide on the intent's UNIQUE constraint (DUPLICATE) and
        a retry after a FAILED/REJECTED exit gets the next attempt number."""
        with self.engine.db.read() as c:
            n = c.execute("SELECT COUNT(*) FROM execution_intents WHERE signal_id LIKE ?", (f"exit:{pid}:{reason}:%",)).fetchone()[0]
        return f"exit:{pid}:{reason}:{n + 1}"

    def _resolve_incidents(self) -> None:
        with self.engine.db.tx() as c:
            rows = c.execute("SELECT id FROM incidents WHERE status='OPEN' AND kind=?", (INCIDENT_KIND,)).fetchall()
            for r in rows:
                c.execute("UPDATE incidents SET status='RESOLVED', resolved_by=?, resolved_at=?, resolution=? WHERE id=? AND status='OPEN'",
                          ("exit_monitor", now_iso(), f"{self.clean_cycles_to_clear} consecutive cycles with every position verified", r["id"]))
                self.engine.audit.record("INCIDENT_RESOLVED", "OK", "", resource=f"incident:{r['id']}", metadata={"by": "exit_monitor"}, conn=c)
