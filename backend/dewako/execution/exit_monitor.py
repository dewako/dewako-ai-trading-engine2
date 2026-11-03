"""Exit monitor: enforces each position's persisted stop-loss, take-profit and maximum holding time.

A stop price stored on a position protects nothing by itself. This worker compares every open position with a validated, fresh,
executable quote on every cycle and routes any exit through the Risk Engine (`approve_exit`) and the Execution Engine
(`execute(intent="EXIT")`). It never sends an order itself.

If protection cannot be verified for any position (no adapter, no valid fresh quote, no stop on the position, a position in an uncertain
status, or an exit that could not be placed) the monitor opens an EXIT_PROTECTION_UNVERIFIED incident. While that incident is open the
Execution Engine refuses new entries. The incident is closed again only after `clean_cycles_to_clear` consecutive cycles in which every
position was verified. The worker wrapper raises when anything is unverified so the supervisor reports the worker unhealthy.

Wide spreads: a triggered exit whose quote fails ONLY the spread limit is never silently dropped and never silently forced. The trigger is
evaluated on the bid. If `emergency_exit_max_spread_pct` is configured and the spread is within it, the exit is sent with that bound (the
quote must still be fresh, positive and uncrossed). If it is not configured, or the spread is wider than it, NO order is sent, the position
stays OPEN, an EXIT_BLOCKED_WIDE_SPREAD incident (CRITICAL) blocks new entries, the attempt count is recorded, and the exit is retried every
cycle. The monitor never reports a position closed that is not.

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
import math

from dewako.execution.validation import QuoteGuardError, WideSpreadError, validate_executable_quote
from dewako.persistence.db import now_iso

log = logging.getLogger("dewako.exit_monitor")

INCIDENT_KIND = "EXIT_PROTECTION_UNVERIFIED"
WIDE_SPREAD_KIND = "EXIT_BLOCKED_WIDE_SPREAD"
_PENDING_STATES = ("CREATED", "SUBMITTING", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN", "RECONCILING")


@dataclass
class MonitorReport:
    checked: int = 0
    exits: list = field(default_factory=list)          # (position_id, reason, outcome)
    unverified: list = field(default_factory=list)     # (position_id, why)
    pending: list = field(default_factory=list)        # position ids with an exit already in flight
    wide: list = field(default_factory=list)           # (position_id, reason, spread_pct, attempts): triggered but blocked by a wide spread
    waiting: list = field(default_factory=list)        # (position_id, reason, spread_pct, cycles): take-profit / time stop waiting for a fair spread


class ExitMonitor:
    def __init__(self, engine, risk, *, max_hold_seconds: Optional[float] = None, clock: Callable[[], datetime] = utcnow,
                 clean_cycles_to_clear: int = 2, emergency_exit_max_spread_pct: Optional[float] = None,
                 stop_suspect_spread_pct: Optional[float] = 10.0, stop_suspect_confirm_cycles: int = 3):
        self.engine, self.risk = engine, risk
        for name, value, upper in (("max_hold_seconds", max_hold_seconds, None), ("emergency_exit_max_spread_pct", emergency_exit_max_spread_pct, 100.0)):
            if value is None:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
                    or (upper is not None and value > upper)):
                raise ValueError(f"{name} must be a finite positive number" + (f" not above {upper:g}" if upper else "") + " or unset")
        if stop_suspect_spread_pct is not None and (isinstance(stop_suspect_spread_pct, bool) or not isinstance(stop_suspect_spread_pct, (int, float))
                                                     or not math.isfinite(stop_suspect_spread_pct) or not 0 < stop_suspect_spread_pct <= 100):
            raise ValueError("stop_suspect_spread_pct must be a finite number above 0 and not above 100, or None")
        if isinstance(stop_suspect_confirm_cycles, bool) or not isinstance(stop_suspect_confirm_cycles, int) or stop_suspect_confirm_cycles < 1:
            raise ValueError("stop_suspect_confirm_cycles must be an integer of at least 1")
        self.stop_suspect_spread_pct = stop_suspect_spread_pct
        self.stop_suspect_confirm_cycles = stop_suspect_confirm_cycles
        self._waiting: dict[int, int] = {}
        self.max_hold_seconds = max_hold_seconds
        self.emergency_exit_max_spread_pct = emergency_exit_max_spread_pct
        self._wide_attempts: dict[int, int] = {}
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
        wide_ids = {w[0] for w in rep.wide}
        for pid, why in rep.unverified:
            p = next(x for x in positions if x["id"] == pid)
            wide = pid in wide_ids
            self.engine.open_incident(WIDE_SPREAD_KIND if wide else INCIDENT_KIND, f"position {pid} {p['symbol']}: {why}",
                                      severity="CRITICAL" if wide else "HIGH",
                                      scope=rsv.scope_of(p["environment"], p["exchange"]), correlation_id=f"exitmon:{pid}")
        live = {p["id"] for p in positions}
        for pid in [k for k in self._wide_attempts if k not in live or (k not in wide_ids)]:
            self._wide_attempts.pop(pid, None)            # the streak ends when the trigger clears, the exit goes out, or the position closes
        waiting_ids = {w[0] for w in rep.waiting}
        for pid in [k for k in self._waiting if k not in live or k not in waiting_ids]:
            self._waiting.pop(pid, None)                  # the wait ends when the trigger clears, the exit goes out, or the position closes
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
        wide: Optional[WideSpreadError] = None
        try:
            quote = await adapter.get_ticker(p["symbol"])
            bid = quote.get("bid") if isinstance(quote, dict) else None
            check = validate_executable_quote(quote, side="SELL", reference_price=bid, now=self.clock(), config=self.engine.quote_guard)
            price = check.executable_price
        except WideSpreadError as e:      # otherwise-valid quote: the triggers can still be evaluated on its bid
            wide, price = e, e.bid
        except (QuoteGuardError, TypeError, ValueError) as e:
            rep.unverified.append((pid, f"no valid fresh quote ({e})"))
            return
        except Exception as e:
            rep.unverified.append((pid, f"quote unavailable ({type(e).__name__})"))
            return
        reason = None
        if price <= p["stop_loss"]:
            reason = "STOP"
        elif p["take_profit"] is not None and price >= p["take_profit"]:
            reason = "TAKE_PROFIT"
        elif self.max_hold_seconds is not None and self._held_seconds(p) >= self.max_hold_seconds:
            reason = "MAX_HOLD"
        if reason is None:
            if wide is not None:
                rep.unverified.append((pid, f"no valid fresh quote ({wide})"))
            return
        if self._exit_in_flight(p):
            rep.pending.append(pid)
            return
        cap = None
        immediate = False
        if wide is not None and reason == "STOP":
            # Stop-loss: goes out at market at once, through any spread a real market can show, to limit the downside. ONE exception: a
            # spread above `stop_suspect_spread_pct` is far more likely a bad quote than a market, and selling on it can dump the position at a
            # glitch price. Such a quote must persist for `stop_suspect_confirm_cycles` consecutive cycles before the stop is sent; until then
            # protection is reported as not enforceable (CRITICAL incident, entries blocked) so the hold is never silent.
            limit = self.stop_suspect_spread_pct
            if limit is not None and wide.spread_pct > limit:
                n = self._wide_attempts[pid] = self._wide_attempts.get(pid, 0) + 1
                if n < self.stop_suspect_confirm_cycles:
                    rep.wide.append((pid, reason, wide.spread_pct, n))
                    rep.unverified.append((pid, f"STOP triggered at bid {price:g} but the spread {wide.spread_pct:.4g}% exceeds the suspect-quote "
                                                f"limit {limit:g}%: confirming it persists ({n} of {self.stop_suspect_confirm_cycles} cycles) before selling"))
                    if n == 1:
                        self.engine.audit.record_safely("EXIT_STOP_QUOTE_SUSPECT", "BLOCKED", "", resource=f"position:{pid}",
                                                 metadata={"reason": reason, "bid": price, "spread_pct": wide.spread_pct, "suspect_limit": limit,
                                                           "confirm_cycles": self.stop_suspect_confirm_cycles})
                    return
                policy = "STOP_LOSS_CONFIRMED_WIDE"
            else:
                policy = "STOP_LOSS_IMMEDIATE"
            immediate = True
            self.engine.audit.record_safely("EMERGENCY_EXIT_WIDE_SPREAD", "OK", "", resource=f"position:{pid}",
                                     metadata={"reason": reason, "bid": price, "spread_pct": wide.spread_pct, "policy": policy,
                                               "emergency_limit": None})
        elif wide is not None:
            # Take-profit and time stop are not emergencies: they use the normal limit, then the explicit tolerance, and otherwise WAIT for a
            # fair spread. Waiting is audited and reported but is not an unverified-protection event: the position's stop-loss is still
            # enforced every cycle, so the worker stays healthy, no incident is opened and new entries are not blocked.
            cap = self.emergency_exit_max_spread_pct
            if cap is None or wide.spread_pct > cap:
                n = self._waiting[pid] = self._waiting.get(pid, 0) + 1
                rep.waiting.append((pid, reason, wide.spread_pct, n))
                if n == 1:
                    self.engine.audit.record_safely("EXIT_WAITING_WIDE_SPREAD", "WAITING", "", resource=f"position:{pid}",
                                             metadata={"reason": reason, "bid": price, "spread_pct": wide.spread_pct, "tolerance": cap})
                return
            self.engine.audit.record_safely("EMERGENCY_EXIT_WIDE_SPREAD", "OK", "", resource=f"position:{pid}",
                                     metadata={"reason": reason, "bid": price, "spread_pct": wide.spread_pct, "emergency_limit": cap})
        sig = Signal(symbol=p["symbol"], side=Side.SELL, confidence=1.0, strategy="ExitMonitor", strategy_version="1",
                     signal_id=self._signal_id(pid, reason), reason=[f"{reason} trigger at {price:g}"], entry_price=price,
                     timestamp=self.clock())
        decision = self.risk.approve_exit(sig, p["quantity"], p["side"])
        if not decision.approved:
            rep.unverified.append((pid, "exit refused by risk engine: " + "; ".join(decision.reasons)))
            return
        res = await self.engine.execute(decision, bot_id=p["bot_id"], environment=p["environment"], exchange=p["exchange"],
                                        intent="EXIT", correlation_id=uuid.uuid4().hex, risk_scope=None,
                                        emergency_max_spread_pct=cap, spread_exempt=immediate)
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
            rows = c.execute("SELECT id FROM incidents WHERE status='OPEN' AND kind IN (?,?)", (INCIDENT_KIND, WIDE_SPREAD_KIND)).fetchall()
            for r in rows:
                c.execute("UPDATE incidents SET status='RESOLVED', resolved_by=?, resolved_at=?, resolution=? WHERE id=? AND status='OPEN'",
                          ("exit_monitor", now_iso(), f"{self.clean_cycles_to_clear} consecutive cycles with every position verified", r["id"]))
                self.engine.audit.record("INCIDENT_RESOLVED", "OK", "", resource=f"incident:{r['id']}", metadata={"by": "exit_monitor"}, conn=c)
