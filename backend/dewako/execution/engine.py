"""Execution engine: the ONLY place that sends orders to an exchange adapter.

Flow for one approved decision
  1. gate (LIVE needs every safety condition) -> 2. signal + intent + order row in ONE transaction (UNIQUE constraint
  decides duplicates) -> 3. persist SUBMITTING *before* calling the exchange -> 4. call the adapter ->
  5. persist the outcome. Any ambiguity becomes UNKNOWN -> RECONCILING and is resolved by asking the exchange for the
  client order id. A retry is only possible after the exchange has positively said it never accepted the order.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import dataclasses
import uuid
from dataclasses import dataclass
from typing import Optional

from dewako.core.types import Order, OrderRequest, OrderStatus, OrderType, RiskDecision, Side, utcnow
from dewako.exchanges.base import (ExchangeAdapter, ExchangeError, InsufficientBalance, InvalidOrder, NotConnected,
                                   NotImplementedForExchange, OrderNotFound, OrderOutcomeUnknown)
from dewako.exchanges.contract import ORDER_ENVIRONMENTS_ENABLED, redact, refusals_for_submission
from dewako.execution.order_store import OrderStore, StaleState
from dewako.execution.positions import PositionError, PositionStore
from dewako.execution.validation import (QuoteGuardConfig, QuoteGuardError, conservative_risk_price,
                                         validate_executable_quote,
                                         validate_order)
from dewako.execution.state_machine import OrderState as S
from dewako.execution import reservations as rsv
from dewako.risk.engine import RiskEngine, RiskLimits
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, now_iso
from dewako.risk.state import RiskStateStore
from dewako.safety.controller import SafetyController

log = logging.getLogger("dewako.execution")

_MAP = {OrderStatus.NEW: S.OPEN, OrderStatus.OPEN: S.OPEN, OrderStatus.PARTIAL: S.PARTIALLY_FILLED,
        OrderStatus.FILLED: S.FILLED, OrderStatus.CANCELLED: S.CANCELLED, OrderStatus.REJECTED: S.REJECTED,
        OrderStatus.UNKNOWN: S.UNKNOWN}


class ExecutionRefused(Exception):
    """Refused before anything was sent. Carries the reasons."""
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class _SendBlocked(Exception):
    """A last-moment refusal with a safe, user-readable reason."""


def _safe_error(error: object) -> str:
    return redact(str(error))[:500]


@dataclass
class ExecResult:
    outcome: str            # EXECUTED | DUPLICATE | REJECTED | UNRESOLVED | FAILED | BLOCKED
    order_id: Optional[int] = None
    state: Optional[str] = None
    detail: str = ""


def _valid(o: object, order: dict) -> Optional[str]:
    """Why an adapter response cannot be trusted, or None."""
    if not isinstance(o, Order):
        return "response is not an order"
    if o.client_order_id != order["client_order_id"]:
        return "response is for a different client order id"
    if o.symbol != order["symbol"] or o.side.value != order["side"]:
        return "response symbol or side does not match"
    if (isinstance(o.quantity, bool) or not isinstance(o.quantity, (int, float))
            or not math.isfinite(o.quantity) or o.quantity <= 0
            or not math.isclose(o.quantity, order["quantity"], rel_tol=1e-9, abs_tol=1e-12)):
        return "response quantity does not match the submitted quantity"
    if (isinstance(o.filled, bool) or not isinstance(o.filled, (int, float))
            or not math.isfinite(o.filled) or not 0 <= o.filled <= order["quantity"]):
        return "filled quantity is impossible"
    if o.filled < order["filled"]:
        return "filled quantity regressed from the previously confirmed amount"
    if (o.filled > 0 and (o.price is None or isinstance(o.price, bool) or not isinstance(o.price, (int, float))
                          or not math.isfinite(o.price) or o.price <= 0)):
        return "filled order has no valid positive finite price"
    if o.status is OrderStatus.FILLED and not math.isclose(o.filled, order["quantity"], rel_tol=1e-9, abs_tol=1e-12):
        return "filled order quantity does not match the submitted quantity"
    return None


class ExecutionEngine:
    def __init__(self, db: Database, safety: SafetyController, adapters: dict[str, ExchangeAdapter], *,
                 audit: Optional[AuditLog] = None, risk_state: Optional[RiskStateStore] = None,
                 exchange_ids: Optional[dict[str, int]] = None, limits: Optional[RiskLimits] = None,
                 submit_authorized=None, quote_guard: Optional[QuoteGuardConfig] = None,
                 clock=utcnow, risk_engine: Optional[RiskEngine] = None):
        """`adapters` maps exchange name -> adapter. Live exchange ids come from `exchange_ids` (exchanges table).
        `limits` are the hard caps enforced under the reservation lock (default: the platform's default RiskLimits, never 'unlimited').
        `submit_authorized` is a callable returning (ok, reason); the application sets it so a worker that has been stopped or has
        lost its authorization cannot send an order that was created before that happened."""
        self.limits = limits or RiskLimits()
        self.risk_engine = risk_engine or RiskEngine(self.limits)      # final, executable-price sizing approval for entries (no I/O)
        self.submit_authorized = submit_authorized
        self.db, self.safety = db, safety
        self.audit = audit or AuditLog(db)
        self.orders, self.positions = OrderStore(db), PositionStore(db)
        self.risk_state = risk_state or RiskStateStore(db)
        self.adapters = adapters
        self.exchange_ids = exchange_ids or {}
        self.quote_guard = quote_guard
        self.clock = clock
        self._unapplied: list = []
        self.entry_guard = None          # optional callable -> (ok, reason): the application wires the exit monitor's health here
        safety.live_adapters_ok = self._live_capable             # activation asks the engine whether its own live lock would let any order through

    # ------------------------------------------------------------------ entry point
    async def execute(self, decision: RiskDecision, *, bot_id: str, environment: str, exchange: str,
                      intent: str = "ENTRY", correlation_id: str = "", market_type: str = "SPOT",
                      risk_scope: Optional[str] = None, emergency_max_spread_pct: Optional[float] = None,
                      spread_exempt: bool = False) -> ExecResult:
        """`emergency_max_spread_pct` is honoured ONLY for a SELL EXIT: it raises that order's spread limit (never lowers it) so a triggered
        stop/target can still leave through a wide spread. Everything else about the quote (fresh, positive, uncrossed) is still enforced.
        The caller owns the policy; without it a wide spread blocks the exit exactly as before.
        `spread_exempt` (also SELL EXIT only) removes the spread limit for that one order: a triggered STOP-LOSS must not wait for a tighter
        spread. The quote must still be present, positive, uncrossed and fresh; nothing else about the quote guard is relaxed."""
        cid = correlation_id or uuid.uuid4().hex
        sig = decision.signal
        reasons = self._refusals(decision, environment, exchange, market_type)
        if sig.side is Side.SELL and intent != "EXIT":
            reasons.append("spot SELL is only allowed as an EXIT of an existing long position (short selling is not implemented)")
        if sig.side is Side.BUY and intent == "ENTRY":
            reasons += self._entry_protection_refusals(sig)
        if environment == "LIVE" and not reasons:
            gate = self.safety.live_gate(exchange_id=self.exchange_ids.get(exchange, -1), strategy_id=sig.strategy,
                                         strategy_version=sig.strategy_version, symbol=sig.symbol)
            reasons += gate.reasons
        if reasons:
            self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": reasons, "bot": bot_id})
            return ExecResult("BLOCKED", detail="; ".join(reasons))
        adapter = self.adapters[exchange]
        if self.quote_guard is None:
            reasons = ["quote guard configuration is missing; refusing order"]
            self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": reasons})
            return ExecResult("BLOCKED", detail="; ".join(reasons))

        guard = self.quote_guard
        if spread_exempt and sig.side is Side.SELL and intent == "EXIT":
            guard = dataclasses.replace(self.quote_guard, enforce_spread=False)
        elif emergency_max_spread_pct is not None and sig.side is Side.SELL and intent == "EXIT":
            try:
                guard = dataclasses.replace(self.quote_guard, max_spread_pct=max(self.quote_guard.max_spread_pct, float(emergency_max_spread_pct)))
            except (TypeError, ValueError):
                reasons = ["emergency exit spread limit is invalid; refusing order"]
                self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": reasons})
                return ExecResult("BLOCKED", detail="; ".join(reasons))
        sized_from = None
        # Exchange rules. Anything we cannot verify blocks the order (nothing is created, nothing is sent).
        try:
            markets = await adapter.get_markets()
            balances = await adapter.get_balance()
            quote = await adapter.get_ticker(sig.symbol)
            check = validate_executable_quote(quote, side=sig.side.value, reference_price=sig.entry_price, now=self.clock(),
                                              config=guard)
            # One conservative executable-price basis for every pre-trade figure below (min notional, balance, limits, reservation).
            risk_price = conservative_risk_price(sig.side.value, sig.entry_price, check)
            order_quantity = decision.quantity
            if sig.side is Side.BUY and intent == "ENTRY":
                # The Risk Engine sized this from the reference price. The order can fill at the executable ask, so the loss at the stop is
                # measured from that price; the quantity is scaled down (never up) so it cannot exceed the approved risk.
                sized = self.risk_engine.size_for_executable_entry(decision, executable_price=risk_price)
                if not sized.approved:
                    raise QuoteGuardError("; ".join(sized.reasons))
                order_quantity = sized.quantity
                if order_quantity < decision.quantity:
                    sized_from = decision.quantity
            v = validate_order(markets, balances, symbol=sig.symbol, side=sig.side.value,
                               quantity=order_quantity, price=risk_price)
            problems = list(v.problems)
            if not problems and sig.side is Side.BUY and intent == "ENTRY":
                # last gate before an order exists: after exchange rounding the quantity and its loss at the stop must still be within
                # what the Risk Engine approved
                final = self.risk_engine.final_entry_problem(decision, quantity=v.quantity, executable_price=risk_price)
                if final:
                    problems.append(final)
        except QuoteGuardError as e:
            problems = [str(e)]
        except NotImplementedForExchange as e:
            problems = [str(e)]
        except Exception as e:
            problems = [f"exchange rules could not be read ({type(e).__name__})"]
        if problems:
            with self.db.tx() as c:
                self._upsert_signal(c, decision, bot_id, "REJECTED")
                c.execute("UPDATE signals SET rejection_reason=? WHERE signal_id=? AND status='REJECTED'", ("; ".join(problems)[:500], sig.signal_id))
                self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": problems}, conn=c)
            return ExecResult("BLOCKED", detail="; ".join(problems))
        decision = decision.model_copy(update={"quantity": v.quantity})

        risk_increasing = sig.side is Side.BUY and intent == "ENTRY"       # spot, long-only: a BUY entry adds exposure
        scope = rsv.scope_of(environment, exchange)
        blocked: list[str] = []
        created = None
        with self.db.tx() as c:
            self._upsert_signal(c, decision, bot_id, "EXECUTING")
            did = self.record_decision(c, decision, bot_id)
            seen = c.execute("SELECT id FROM execution_intents WHERE bot_id=? AND signal_id=? AND intent=?", (bot_id, sig.signal_id, intent)).fetchone()
            if seen is None:      # first time this decision reaches the engine (a replay is not a new decision)
                self.audit.record("RISK_APPROVED", "OK", cid, resource=f"signal:{sig.signal_id}", conn=c,
                                  metadata={"quantity": decision.quantity, "symbol": sig.symbol, "strategy": sig.strategy, "version": sig.strategy_version,
                                            "environment": environment,
                                            **({"reduced_for_executable_entry_risk_from": sized_from} if sized_from else {})})
            if risk_increasing and seen is None:
                # 1. take the scope lock, 2. read durable reservations + positions, 3. read the exchange balance (this order is
                # deliberate: a fill landing in between can only make the check stricter, never looser), 4. check, 5. reserve.
                rsv.lock_scope(self.db, c, scope)
                st = rsv.committed_state(c, environment, exchange, scope)
                quote = sig.symbol.split("/")[1]
                try:
                    quote_balance = float((await adapter.get_balance()).get(quote, 0.0))
                except Exception as e:
                    quote_balance, blocked = 0.0, [f"balance could not be read ({type(e).__name__}); risk-increasing order blocked"]
                if not blocked:
                    held = c.execute("SELECT 1 FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                                     (bot_id, exchange, environment, sig.symbol)).fetchone()
                    blocked = rsv.check_limits(self.limits, equity=quote_balance + st["position_cost"], quote_balance=quote_balance,
                                               notional=decision.quantity * risk_price, st=st, new_position=held is None,
                                               exposure_increasing=True)
            if sig.side is Side.SELL and seen is None and not blocked:
                # A SELL may only reduce an existing long position, counting exits that are already pending. Checked under the scope lock
                # so two concurrent exits cannot together sell more than is held.
                rsv.lock_scope(self.db, c, scope)
                problem = self._exit_size_problem(c, bot_id, exchange, environment, sig.symbol, decision.quantity, sig.signal_id, intent)
                if problem:
                    blocked = [problem]
            if blocked:
                c.execute("UPDATE signals SET status='REJECTED', rejection_reason=? WHERE signal_id=? AND status='EXECUTING'", ("; ".join(blocked)[:500], sig.signal_id))
                self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"signal:{sig.signal_id}", metadata={"reasons": blocked, "scope": scope}, conn=c)
            else:
                created = self.orders.create(risk_decision_id=did, bot_id=bot_id, signal_id=sig.signal_id, intent=intent, environment=environment,
                                             exchange=exchange, symbol=sig.symbol, side=sig.side.value, order_type="MARKET",
                                             quantity=decision.quantity, price=sig.entry_price, correlation_id=cid,
                                             market_type=market_type, conn=c)
                if created.created:
                    if risk_increasing:
                        rsv.reserve(c, order_id=created.order_id, scope=scope, environment=environment, exchange=exchange, bot_id=bot_id,
                                    symbol=sig.symbol, side=sig.side.value, quantity=decision.quantity, price=risk_price)
                    self.audit.record("ORDER_CREATED", "OK", cid, resource=f"order:{created.order_id}",
                                      metadata={"signal": sig.signal_id, "bot": bot_id, "env": environment}, conn=c)
        if blocked:
            return ExecResult("BLOCKED", detail="; ".join(blocked))
        if not created.created:
            return ExecResult("DUPLICATE", created.order_id, self.orders.get(created.order_id)["state"], "this signal already has an order")
        if guard is self.quote_guard:
            return await self._send(created.order_id, adapter, risk_scope or environment)
        return await self._send(created.order_id, adapter, risk_scope or environment, quote_config=guard)      # only the bounded emergency exit

    def _entry_protection_refusals(self, sig) -> list[str]:
        why = []
        px = sig.entry_price
        if sig.stop_price is None or not (px and 0 < sig.stop_price < px):
            why.append("entry needs a stop below the entry price; an unprotected position cannot be created")
        elif sig.take_profit_price is not None and sig.take_profit_price <= px:
            why.append("take-profit must be above the entry price for a long")
        degraded = getattr(self.audit, "degraded", None)
        if degraded is not None and degraded():
            # Audit persistence is failing: new risk is not taken while its record cannot be guaranteed. Exits are never blocked by this.
            why.append("audit persistence is degraded; new risk-increasing orders are blocked until the audit log is written again")
        if self.entry_guard is not None:
            try:
                ok, reason = self.entry_guard()
            except Exception as e:
                ok, reason = False, f"protection status could not be verified ({type(e).__name__})"
            if not ok:
                why.append(f"exit protection is not verified: {reason}")
        with self.db.read() as c:
            open_inc = c.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND kind IN "
                                 "('EXIT_PROTECTION_UNVERIFIED','EXIT_BLOCKED_WIDE_SPREAD')").fetchone()[0]
        if open_inc:
            why.append("exit protection is unverified for an open position (incident open); new entries are blocked")
        return why

    _OPEN_ORDER_STATES = ("CREATED", "SUBMITTING", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN", "RECONCILING")

    def _exit_size_problem(self, c, bot_id: str, exchange: str, environment: str, symbol: str, qty: float, signal_id: str, intent: str) -> Optional[str]:
        pos = c.execute("SELECT quantity FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                        (bot_id, exchange, environment, symbol)).fetchone()
        if pos is None or pos["quantity"] <= 0:
            return f"no open position in {symbol} to exit; SELL refused"
        marks = ",".join("?" * len(self._OPEN_ORDER_STATES))
        pending = c.execute(f"SELECT COALESCE(SUM(o.quantity - o.filled), 0) FROM orders o JOIN execution_intents i ON i.id=o.intent_id "
                            f"WHERE o.bot_id=? AND o.exchange=? AND o.environment=? AND o.symbol=? AND o.side='SELL' AND o.state IN ({marks}) "
                            f"AND NOT (i.signal_id=? AND i.intent=?)",
                            (bot_id, exchange, environment, symbol, *self._OPEN_ORDER_STATES, signal_id, intent)).fetchone()[0]
        if qty > pos["quantity"] - pending + 1e-12:
            return f"exit quantity {qty:g} exceeds the open position {pos['quantity']:g} less {pending:g} already being sold; SELL refused"
        return None

    def _live_capable(self) -> tuple[bool, str]:
        """Same rule as the live lock in `_refusals`: an adapter must be live-verified (or a test double) to receive a LIVE order."""
        live = [a for a in self.adapters.values() if a.environment == "LIVE"]
        if not live:
            return False, "no LIVE adapter is configured"
        ok = [a for a in live if a.capabilities().live_execution_verified or a.capabilities().test_double]
        if not ok:
            return False, "no configured adapter is verified for live execution (" + ", ".join(sorted(a.name for a in live)) + ")"
        return True, ""

    def _refusals(self, d: RiskDecision, environment: str, exchange: str, market_type: str) -> list[str]:
        why = []
        if not d.approved or d.quantity <= 0:
            why.append("only approved risk decisions can be executed")
        if not d.signal.signal_id:
            why.append("signal has no signal_id (it did not come from the SignalEngine)")
        if environment == "TESTNET":
            why.append("TESTNET execution is not enabled in this build (it needs a schema migration and a verified testnet adapter)")
        elif environment not in ORDER_ENVIRONMENTS_ENABLED:
            why.append("environment must be PAPER or LIVE")
        if market_type != "SPOT":
            why.append(f"NOT_IMPLEMENTED: {market_type} execution")
        ad = self.adapters.get(exchange)
        if ad is None:
            why.append(f"no adapter configured for {exchange}")
        elif ad.environment != environment:
            why.append(f"adapter environment {ad.environment} does not match requested {environment}")
        else:
            why += refusals_for_submission(ad, environment)       # declared capabilities (Phase 4.1); the live lock below is separate
        if ad is not None and ad.environment == environment and environment == "LIVE":
            # LIVE LOCK: independent of the live switch, environment variables, API calls or confirmation phrases. An adapter that has not been
            # verified for live execution (no shipped adapter has) cannot receive an order. Test doubles cannot reach a real exchange.
            caps = ad.capabilities()
            if not (caps.live_execution_verified or caps.test_double):
                why.append(f"live lock: the {ad.name} adapter is not verified for live execution")
        return why

    @staticmethod
    def record_decision(c, d: RiskDecision, bot_id: str) -> str:
        """Persist the Risk Engine's decision (approved or not) so the chain signal -> decision -> intent -> order is traceable."""
        did = hashlib.sha256(f"{d.signal.signal_id}|{bot_id}|{int(d.approved)}|{d.quantity}".encode()).hexdigest()[:20]
        c.execute("INSERT INTO risk_decisions(risk_decision_id,signal_id,bot_id,approved,quantity,reasons,decided_at) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT (risk_decision_id) DO NOTHING", (did, d.signal.signal_id, bot_id, 1 if d.approved else 0, d.quantity,
                                                                 json.dumps(d.reasons), now_iso()))
        return did

    @staticmethod
    def _upsert_signal(c, d: RiskDecision, bot_id: str, status: str) -> None:
        s = d.signal
        c.execute("INSERT INTO signals(signal_id,bot_id,strategy_id,strategy_version,symbol,side,price,confidence,timestamp,status,created_at,"
                  "stop_price,take_profit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO NOTHING",
                  (s.signal_id, bot_id, s.strategy, s.strategy_version, s.symbol, s.side.value, s.entry_price, s.confidence,
                   s.timestamp.isoformat(), status, now_iso(), s.stop_price, s.take_profit_price))
        # a signal row written earlier by the bot runner has no levels yet: fill them in without overwriting anything set
        c.execute("UPDATE signals SET stop_price=COALESCE(stop_price,?), take_profit_price=COALESCE(take_profit_price,?) WHERE signal_id=?",
                  (s.stop_price, s.take_profit_price, s.signal_id))

    # ------------------------------------------------------------------ sending
    async def _send(self, order_id: int, adapter: ExchangeAdapter, risk_scope: str, quote_config: Optional[QuoteGuardConfig] = None) -> ExecResult:
        o = self.orders.get(order_id)
        cid = o["correlation_id"]
        cur = S(o["state"])
        refusal = self._send_refusal(o)
        if refusal:      # re-checked at the last moment: the kill switch, the live gate or this worker's authorization may have changed
            if cur is S.CREATED:
                self.orders.transition(order_id, cur, S.FAILED, "BLOCKED_BEFORE_SEND", correlation_id=cid, error=refusal)
                self._signal_status(o, "FAILED")
            self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"order:{order_id}", metadata={"reasons": [refusal], "stage": "pre-send"})
            return ExecResult("BLOCKED", order_id, self.orders.get(order_id)["state"], refusal)
        event = "QUOTE_REVALIDATION_FAILED"
        try:
            fresh = validate_executable_quote(await adapter.get_ticker(o["symbol"]), side=o["side"], reference_price=o["price"],
                                              now=self.clock(), config=quote_config or self.quote_guard)
            reason = self._reserved_price_exceeded(o, fresh.executable_price)
            if reason:
                raise _SendBlocked(reason)
            if o["side"] == "SELL":          # a SELL reserves nothing, so its last-moment protection is the exchange rules at the current bid
                event = "RULES_REVALIDATION_FAILED"
                reason = await self._sell_rules_problem(o, adapter, fresh)
                if reason:
                    raise _SendBlocked(reason)
        except Exception as e:
            reason = str(e) if isinstance(e, _SendBlocked) else f"executable quote could not be revalidated ({type(e).__name__})"
            if cur is S.CREATED:
                self.orders.transition(order_id, cur, S.FAILED, event, correlation_id=cid, error=reason)
                self._signal_status(o, "FAILED")
            self.audit.record("ORDER_BLOCKED", "BLOCKED", cid, resource=f"order:{order_id}",
                              metadata={"reasons": [reason], "stage": "pre-send"})
            return ExecResult("BLOCKED", order_id, self.orders.get(order_id)["state"], reason)
        # Persist the intent to send BEFORE sending. A crash after this line leaves SUBMITTING, which recovery treats as unknown.
        self.orders.transition(order_id, cur, S.SUBMITTING, "SUBMIT_ATTEMPT", correlation_id=cid)
        req = OrderRequest(symbol=o["symbol"], side=Side(o["side"]), type=OrderType.MARKET, quantity=o["quantity"],
                           client_order_id=o["client_order_id"], bot_id=o["bot_id"])
        try:
            res = await adapter.create_order(req)
        except (InvalidOrder, InsufficientBalance) as e:  # definitive refusal by the exchange
            reason = _safe_error(e)
            self.orders.transition(order_id, S.SUBMITTING, S.REJECTED, "EXCHANGE_REJECTED", correlation_id=cid, error=reason)
            self._signal_status(o, "FAILED")
            self.audit.record_safely("ORDER_REJECTED", "REJECTED", cid, resource=f"order:{order_id}", metadata={"error": reason})
            return ExecResult("REJECTED", order_id, S.REJECTED.value, reason)
        except NotImplementedForExchange as e:  # the adapter says the capability does not exist: nothing was sent
            reason = _safe_error(e)
            self.orders.transition(order_id, S.SUBMITTING, S.FAILED, "NOT_SENT", correlation_id=cid, error=reason)
            self._signal_status(o, "FAILED")
            return ExecResult("FAILED", order_id, S.FAILED.value, reason)
        except Exception as e:  # timeout, 5xx, reset, a generic NotConnected (a drop mid-request is indistinguishable from "never sent"), anything unclassified: the order MAY exist
            return await self._unknown(order_id, adapter, f"{type(e).__name__}: {_safe_error(e)}", risk_scope)
        bad = _valid(res, o)
        if bad:
            return await self._unknown(order_id, adapter, f"malformed response: {bad}", risk_scope)
        self.orders.transition(order_id, S.SUBMITTING, S.SUBMITTED, "EXCHANGE_ACK", correlation_id=cid,
                               exchange_order_id=res.exchange_order_id)
        self.audit.record_safely("ORDER_SUBMITTED", "OK", cid, resource=f"order:{order_id}", metadata={"exchange_order_id": res.exchange_order_id})
        return self._apply_exchange_order(order_id, S.SUBMITTED, res, risk_scope, adapter)

    async def _sell_rules_problem(self, o: dict, adapter: ExchangeAdapter, fresh) -> Optional[str]:
        """Re-run the exchange rules for a SELL at the CURRENT executable bid (conservative: the lower of the approved reference and the bid)
        with the final validated quantity. Any problem, or any failure to read the rules, blocks the send (fail closed)."""
        try:
            px = conservative_risk_price("SELL", o["price"], fresh)
            v = validate_order(await adapter.get_markets(), await adapter.get_balance(), symbol=o["symbol"], side="SELL",
                               quantity=o["quantity"], price=px)
        except Exception as e:
            return f"exchange rules could not be revalidated before sending ({type(e).__name__})"
        if v.problems:
            return "exchange rules no longer met at the current executable bid: " + "; ".join(v.problems)
        if abs(v.quantity - float(o["quantity"])) > 1e-12:
            return "order quantity no longer matches the exchange lot size at send time"
        return None

    def _reserved_price_exceeded(self, o: dict, executable_price: float) -> Optional[str]:
        """A risk-increasing BUY reserved risk at a specific unit price. If the executable ask has since risen above it, the order would
        cost more than was checked against the limits and the balance, so it is not sent (the reservation is released with the FAILED state)."""
        if o["side"] != "BUY":
            return None
        with self.db.read() as c:
            r = c.execute("SELECT notional, quantity FROM risk_reservations WHERE order_id=? AND status='ACTIVE'", (o["id"],)).fetchone()
        if r is None or not r["quantity"]:
            return None
        reserved_unit = r["notional"] / r["quantity"]
        if executable_price > reserved_unit * (1 + 1e-9):
            return f"executable ask {executable_price:.8g} rose above the price reserved for risk ({reserved_unit:.8g}); order not sent"
        return None

    def _send_refusal(self, o: dict) -> Optional[str]:
        """Last-moment checks before anything is sent. Returns a reason, or None."""
        if o["side"] == "BUY" and self.safety.kill_switch_active():
            return "kill switch active"
        if self.submit_authorized is not None and o["environment"] == "LIVE":
            try:
                ok, why = self.submit_authorized()
            except Exception as e:
                return f"worker authorization could not be verified ({type(e).__name__})"
            if not ok:
                return f"this worker may not submit orders: {why}"
        if o["environment"] == "LIVE":
            with self.db.read() as c:
                sv = c.execute("SELECT s.strategy_id, s.strategy_version FROM signals s JOIN execution_intents i ON i.signal_id=s.signal_id "
                               "WHERE i.id=?", (o["intent_id"],)).fetchone()
            if sv is None:
                return "signal for this order not found"
            g = self.safety.live_gate(exchange_id=self.exchange_ids.get(o["exchange"], -1), strategy_id=sv["strategy_id"],
                                      strategy_version=sv["strategy_version"], symbol=o["symbol"])
            if not g.allowed and o["side"] == "BUY":
                return "live gate closed: " + "; ".join(g.reasons)
        return None

    def open_incident(self, kind: str, detail: str, *, severity: str = "HIGH", scope: str = "", order_id: Optional[int] = None,
                      correlation_id: str = "") -> None:
        """A durable record that a human must investigate. Open incidents close the live gate until resolved."""
        with self.db.tx() as c:
            dup = c.execute("SELECT id FROM incidents WHERE status='OPEN' AND kind=? AND scope=? AND COALESCE(order_id,0)=?",
                            (kind, scope, order_id or 0)).fetchone()
            if dup is None:
                c.execute("INSERT INTO incidents(kind,severity,scope,order_id,detail,status,created_at) VALUES (?,?,?,?,?,?,?)",
                          (kind, severity, scope, order_id, detail[:500], "OPEN", now_iso()))
                self.audit.record("INCIDENT_OPENED", "OPEN", correlation_id, resource=f"order:{order_id}" if order_id else scope,
                                  metadata={"kind": kind}, conn=c)
        self.safety.alert(f"Incident {kind}: {detail}", correlation_id)

    async def _unknown(self, order_id: int, adapter: ExchangeAdapter, why: str, risk_scope: str) -> ExecResult:
        o = self.orders.get(order_id)
        cid = o["correlation_id"]
        self.orders.transition(order_id, S(o["state"]), S.UNKNOWN, "OUTCOME_UNKNOWN", correlation_id=cid, error=why)
        self.audit.record_safely("ORDER_UNKNOWN", "UNKNOWN", cid, resource=f"order:{order_id}", metadata={"why": why})
        self.safety.set_reconciliation_required(True, f"order {order_id} outcome unknown", by="execution", correlation_id=cid)
        return await self.reconcile_order(order_id, adapter, risk_scope)

    # ------------------------------------------------------------------ results
    def _apply_exchange_order(self, order_id: int, from_state: S, res: Order, risk_scope: str, adapter: ExchangeAdapter) -> ExecResult:
        o = self.orders.get(order_id)
        cid = o["correlation_id"]
        target = _MAP[res.status]
        if target is S.UNKNOWN:
            return ExecResult("UNRESOLVED", order_id, from_state.value, "exchange reports an unknown status")
        pnl_info = None
        with self.db.tx() as c:
            if target is S.PARTIALLY_FILLED and res.filled <= 0:
                target = S.OPEN
            delta = max(res.filled - o["filled"], 0.0)
            self.orders.transition(order_id, from_state, target, f"EXCHANGE_{res.status.value}", correlation_id=cid,
                                   exchange_order_id=res.exchange_order_id, filled=res.filled, avg_fill_price=res.price,
                                   exchange_response=res.model_dump(mode="json"), conn=c)
            if delta > 0 and res.price:
                pnl_info = self._apply_fill(c, o, delta, res.price, res.exchange_order_id or "", res.filled, adapter)
            if target in (S.FILLED,):
                c.execute("UPDATE signals SET status='EXECUTED' WHERE signal_id=(SELECT i.signal_id FROM execution_intents i WHERE i.id=?)", (o["intent_id"],))
                self.audit.record("ORDER_FILLED", "OK", cid, resource=f"order:{order_id}", metadata={"filled": res.filled}, conn=c)
            elif target in (S.REJECTED, S.CANCELLED, S.EXPIRED):
                c.execute("UPDATE signals SET status='FAILED' WHERE signal_id=(SELECT i.signal_id FROM execution_intents i WHERE i.id=?)", (o["intent_id"],))
                if target is S.CANCELLED:
                    self.audit.record("ORDER_CANCELLED", "OK", cid, resource=f"order:{order_id}", conn=c)
        self._flush_unapplied()
        if pnl_info is not None and pnl_info[0] is not None:
            self._record_risk(o, risk_scope, pnl_info[0], pnl_info[1])
        out = {S.FILLED: "EXECUTED", S.REJECTED: "REJECTED"}.get(target, "EXECUTED" if target is S.PARTIALLY_FILLED else "UNRESOLVED")
        if target is S.OPEN:
            out = "EXECUTED"
        return ExecResult(out, order_id, target.value)

    def _apply_fill(self, c, o: dict, delta: float, price: float, ex_oid: str, total_filled: float, adapter: ExchangeAdapter):
        """Trade row + position + paper cash, in the caller's transaction. Returns (realized_pnl or None, equity)."""
        pnl = None
        pid = None
        if o["side"] == "BUY":
            lv = c.execute("SELECT s.stop_price, s.take_profit_price FROM signals s JOIN execution_intents i ON i.signal_id=s.signal_id "
                           "WHERE i.id=?", (o["intent_id"],)).fetchone()
            try:
                pid = self.positions.open_or_add(c, bot_id=o["bot_id"], exchange=o["exchange"], environment=o["environment"], symbol=o["symbol"],
                                                 side="BUY", quantity=delta, price=price, market_type=o["market_type"], order_id=o["id"],
                                                 correlation_id=o["correlation_id"], stop_loss=lv["stop_price"] if lv else None,
                                                 take_profit=lv["take_profit_price"] if lv else None)
            except PositionError as e:
                # A real fill that cannot be turned into a PROTECTED position (for example no stop was recorded for the signal). No unprotected
                # position is invented; the fill is kept as a trade row and a CRITICAL incident plus reconciliation require a human decision.
                pid = None
                self._unapplied.append((o["id"], o["exchange"], o["environment"], f"{e}; fill {delta:g} @ {price:g}", o["correlation_id"]))
        else:
            # LIVE closes are only accepted from an exchange-reported fill, which is what reached this point.
            try:
                pid, pnl, _ = self.positions.reduce(c, bot_id=o["bot_id"], exchange=o["exchange"], environment=o["environment"], symbol=o["symbol"],
                                                    quantity=delta, price=price, exchange_confirmed=True, order_id=o["id"],
                                                    correlation_id=o["correlation_id"])
            except PositionError as e:
                # The exchange reports a fill that the local books cannot apply. The fill is real, so it is recorded (trade row without a
                # position); the divergence is surfaced after this transaction commits (incident + reconciliation), never raised.
                pid, pnl = None, None
                self._unapplied.append((o["id"], o["exchange"], o["environment"], f"{e}; fill {delta:g} @ {price:g}", o["correlation_id"]))
        c.execute("INSERT INTO trades(order_id,environment,symbol,side,quantity,price,exchange_trade_id,correlation_id,created_at,position_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (o["id"], o["environment"], o["symbol"], o["side"], delta, price, f"{ex_oid}:{total_filled:.10f}", o["correlation_id"], now_iso(), pid))
        equity = None
        if o["environment"] == "PAPER" and hasattr(adapter, "cash"):
            c.execute("INSERT INTO paper_accounts(bot_id,cash,updated_at) VALUES (?,?,?) ON CONFLICT(bot_id) DO UPDATE SET cash=excluded.cash, updated_at=excluded.updated_at",
                      (o["bot_id"], adapter.cash, now_iso()))
            equity = adapter.cash
        return pnl, equity

    def _flush_unapplied(self) -> None:
        pending, self._unapplied = self._unapplied, []
        for oid, exch, env, why, cid in pending:
            self.safety.set_reconciliation_required(True, f"order {oid}: fill could not be applied to local positions", by="execution", correlation_id=cid)
            self.open_incident("FILL_NOT_APPLICABLE", f"order {oid}: {why}", severity="CRITICAL", scope=rsv.scope_of(env, exch), order_id=oid, correlation_id=cid)

    def _record_risk(self, o: dict, scope: str, pnl: float, equity: Optional[float]) -> None:
        try:
            if equity is None:
                raise KeyError("no equity source for this scope")
            self.risk_state.record_trade_result(scope, pnl, equity)
        except KeyError:
            self.safety.set_risk_healthy(False, by="execution")  # a realised result we could not record: block until a human looks
            log.error("risk state could not be updated after a closing fill; trading blocked")

    def _signal_status(self, o: dict, status: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE signals SET status=? WHERE signal_id=(SELECT signal_id FROM execution_intents WHERE id=?)", (status, o["intent_id"]))

    # ------------------------------------------------------------------ reconciliation of one order
    async def reconcile_order(self, order_id: int, adapter: Optional[ExchangeAdapter] = None, risk_scope: Optional[str] = None) -> ExecResult:
        o = self.orders.get(order_id)
        adapter = adapter or self.adapters[o["exchange"]]
        scope = risk_scope or o["environment"]
        cid = o["correlation_id"]
        st = S(o["state"])
        if st is S.SUBMITTING:  # process died mid-send: the order may exist
            self.orders.transition(order_id, st, S.UNKNOWN, "RECOVERED_AFTER_CRASH", actor="recovery", correlation_id=cid)
            st = S.UNKNOWN
        if st is S.UNKNOWN:
            self.orders.transition(order_id, st, S.RECONCILING, "RECONCILE_START", actor="recovery", correlation_id=cid)
            st = S.RECONCILING
        if st is not S.RECONCILING:
            return ExecResult("UNRESOLVED", order_id, st.value, "order is not in an unresolved state")
        try:
            res = await adapter.get_order(o["symbol"], order_id=o["exchange_order_id"], client_order_id=o["client_order_id"])
        except OrderNotFound:
            self.orders.transition(order_id, st, S.FAILED, "CONFIRMED_ABSENT", actor="recovery", correlation_id=cid,
                                   error="exchange confirmed it has no order with this client id")
            self._signal_status(o, "FAILED")
            self._maybe_clear_reconciliation(cid)
            return ExecResult("FAILED", order_id, S.FAILED.value, "exchange confirmed the order was never accepted; a retry is now safe")
        except Exception as e:
            self.orders.transition(order_id, st, S.RECONCILING, "RECONCILE_INCONCLUSIVE", actor="recovery", correlation_id=cid,
                                   error=f"{type(e).__name__}: {_safe_error(e)}")
            self._escalate_if_stuck(order_id, o)
            return ExecResult("UNRESOLVED", order_id, S.RECONCILING.value, "could not confirm with the exchange yet")
        bad = _valid(res, o)
        if bad or _MAP.get(res.status) is S.UNKNOWN:
            self.orders.transition(order_id, st, S.RECONCILING, "RECONCILE_INCONCLUSIVE", actor="recovery", correlation_id=cid, error=bad or "status unknown")
            return ExecResult("UNRESOLVED", order_id, S.RECONCILING.value, bad or "exchange status unknown")
        r = self._apply_exchange_order(order_id, S.RECONCILING, res, scope, adapter)
        self._maybe_clear_reconciliation(cid)
        return r

    def _escalate_if_stuck(self, order_id: int, o: dict) -> None:
        with self.db.read() as c:
            n = c.execute("SELECT COUNT(*) FROM order_events WHERE order_id=? AND event_type='RECONCILE_INCONCLUSIVE'", (order_id,)).fetchone()[0]
        if n >= self.INCONCLUSIVE_INCIDENT_AFTER:
            self.open_incident("ORDER_UNRESOLVED", f"order {order_id} still cannot be confirmed after {n} attempts; investigate on the exchange",
                               severity="CRITICAL", scope=rsv.scope_of(o["environment"], o["exchange"]), order_id=order_id, correlation_id=o["correlation_id"])

    def _maybe_clear_reconciliation(self, cid: str) -> None:
        """Order-level reconciliation never clears the global flag by itself while other orders remain unresolved."""
        if not self.orders.in_states({S.SUBMITTING, S.UNKNOWN, S.RECONCILING}):
            pass  # the reconciliation service (full account comparison) is what clears RECONCILIATION_REQUIRED

    async def retry(self, order_id: int, actor: str = "operator", risk_scope: Optional[str] = None) -> ExecResult:
        """Resend an order the exchange confirmed it never accepted. Same client id, so it stays idempotent."""
        o = self.orders.get(order_id)
        if o["environment"] == "LIVE":
            g = self.safety.live_gate(exchange_id=self.exchange_ids.get(o["exchange"], -1), strategy_id="", strategy_version="")
            # strategy check is by the signal; look it up
            with self.db.read() as c:
                s = c.execute("SELECT s.strategy_id, s.strategy_version FROM signals s JOIN execution_intents i ON i.signal_id=s.signal_id WHERE i.id=?", (o["intent_id"],)).fetchone()
            g = self.safety.live_gate(exchange_id=self.exchange_ids.get(o["exchange"], -1), strategy_id=s["strategy_id"], strategy_version=s["strategy_version"], symbol=o["symbol"])
            if not g.allowed:
                return ExecResult("BLOCKED", order_id, o["state"], "; ".join(g.reasons))
        return await self._send(order_id, self.adapters[o["exchange"]], risk_scope or o["environment"])

    # ------------------------------------------------------------------ cancel, recovery, kill switch
    async def cancel(self, order_id: int, actor: str = "operator") -> ExecResult:
        o = self.orders.get(order_id)
        st = S(o["state"])
        adapter = self.adapters[o["exchange"]]
        cid = o["correlation_id"]
        self.orders.transition(order_id, st, S.CANCEL_REQUESTED, "CANCEL_REQUESTED", actor=actor, correlation_id=cid)
        try:
            res = await adapter.cancel_order(o["symbol"], o["exchange_order_id"] or o["client_order_id"])
        except Exception as e:
            self.orders.transition(order_id, S.CANCEL_REQUESTED, S.UNKNOWN, "CANCEL_OUTCOME_UNKNOWN", correlation_id=cid,
                                   error=f"{type(e).__name__}: {_safe_error(e)}")
            return await self.reconcile_order(order_id, adapter)
        if _valid(res, o) or res.status is not OrderStatus.CANCELLED:
            self.orders.transition(order_id, S.CANCEL_REQUESTED, S.UNKNOWN, "CANCEL_RESPONSE_UNTRUSTED", correlation_id=cid)
            return await self.reconcile_order(order_id, adapter)
        return self._apply_exchange_order(order_id, S.CANCEL_REQUESTED, res, o["environment"], adapter)

    INCONCLUSIVE_INCIDENT_AFTER = 3

    def abandon_unsent(self, environment: Optional[str] = None, grace_seconds: float = 60.0) -> list[int]:
        """Orders still CREATED were never sent (SUBMITTING is persisted before any send), so the process died between committing the
        intent and sending it. They are not resent from here: a stale market order may no longer be wanted. They end FAILED with
        reason ABANDONED_AFTER_CRASH, which releases their reservation; a fresh signal can try again. `grace_seconds` protects an order
        that another live worker is about to send."""
        from datetime import datetime, timezone, timedelta
        cutoff = utcnow() - timedelta(seconds=grace_seconds)
        done = []
        for o in self.orders.in_states({S.CREATED}, environment):
            created = o["updated_at"]
            created = datetime.fromisoformat(created) if isinstance(created, str) else created
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created > cutoff:
                continue
            try:
                self.orders.transition(o["id"], S.CREATED, S.FAILED, "ABANDONED_AFTER_CRASH", actor="recovery", correlation_id=o["correlation_id"],
                                       error="order was created but never sent; process died before submission")
                self._signal_status(o, "FAILED")
                done.append(o["id"])
            except StaleState:
                pass                       # another worker moved it first
        return done

    async def recover(self, environment: Optional[str] = None, created_grace_seconds: float = 60.0) -> list[ExecResult]:
        """Run at startup and periodically: resolve everything left unresolved by a crash or timeout. Never resends."""
        out = []
        self.abandon_unsent(environment, created_grace_seconds)
        for o in self.orders.in_states({S.SUBMITTING, S.UNKNOWN, S.RECONCILING}, environment):
            out.append(await self.reconcile_order(o["id"]))
        out.extend(await self.sync_active_orders(environment))
        return out

    async def sync_active_orders(self, environment: Optional[str] = None) -> list[ExecResult]:
        """Orders the exchange may have moved on while we were down or while a fill transaction rolled back
        (SUBMITTED / OPEN / PARTIALLY_FILLED / CANCEL_REQUESTED). The exchange is the source of truth for fills.
        An exchange error leaves the order untouched (try again later); an order the exchange does not know goes to UNKNOWN
        and raises reconciliation. Nothing is ever resent."""
        out = []
        for o in self.orders.in_states({S.SUBMITTED, S.OPEN, S.PARTIALLY_FILLED, S.CANCEL_REQUESTED}, environment):
            adapter = self.adapters.get(o["exchange"])
            if adapter is None:
                continue
            cid, st = o["correlation_id"], S(o["state"])
            try:
                res = await adapter.get_order(o["symbol"], order_id=o["exchange_order_id"], client_order_id=o["client_order_id"])
            except OrderNotFound:
                self.orders.transition(o["id"], st, S.UNKNOWN, "ACTIVE_ORDER_MISSING_ON_EXCHANGE", actor="recovery", correlation_id=cid,
                                       error="exchange has no record of an order we believed was active")
                self.safety.set_reconciliation_required(True, f"order {o['id']} missing on exchange", by="recovery", correlation_id=cid)
                self.open_incident("ACTIVE_ORDER_MISSING_ON_EXCHANGE", f"order {o['id']} was {st.value} locally; the exchange has no such order",
                                   severity="CRITICAL", scope=rsv.scope_of(o["environment"], o["exchange"]), order_id=o["id"], correlation_id=cid)
                out.append(ExecResult("UNRESOLVED", o["id"], S.UNKNOWN.value, "active order missing on the exchange"))
                continue
            except Exception as e:
                log.warning("order %s could not be synced: %s", o["id"], type(e).__name__)
                out.append(ExecResult("UNRESOLVED", o["id"], st.value, "could not reach the exchange; left unchanged"))
                continue
            if _valid(res, o) or _MAP.get(res.status) in (None, S.UNKNOWN):
                self.orders.transition(o["id"], st, S.UNKNOWN, "SYNC_UNTRUSTED_RESPONSE", actor="recovery", correlation_id=cid, error=_valid(res, o) or "status unknown")
                self.safety.set_reconciliation_required(True, f"order {o['id']} sync response untrusted", by="recovery", correlation_id=cid)
                self.open_incident("SYNC_RESPONSE_UNTRUSTED", f"order {o['id']}: {_valid(res, o) or 'status unknown'}", scope=rsv.scope_of(o["environment"], o["exchange"]),
                                   order_id=o["id"], correlation_id=cid)
                continue
            target = _MAP[res.status]
            if target is S.PARTIALLY_FILLED and res.filled <= 0:
                target = S.OPEN
            if target is st and abs(res.filled - o["filled"]) < 1e-12:
                continue                                                  # nothing new
            if target is S.OPEN and st is S.PARTIALLY_FILLED:
                continue
            out.append(self._apply_exchange_order(o["id"], st, res, o["environment"], adapter))
        return out

    async def on_kill_switch(self, actor: dict, reason: str, correlation_id: str = "") -> list[ExecResult]:
        """Activate the kill switch, then cancel eligible UNFILLED live orders. Positions are never closed here and
        exchange-side protective orders are not touched."""
        self.safety.activate_kill_switch(actor, reason, correlation_id)
        res = []
        for o in self.orders.in_states({S.SUBMITTED, S.OPEN}, "LIVE"):
            if o["filled"] == 0:
                res.append(await self.cancel(o["id"], actor=actor.get("username", "system")))
        return res
