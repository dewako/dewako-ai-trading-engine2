"""Reconciliation: compare DEWAKO's records with what the exchange reports, and record every discrepancy.

READ-ONLY toward the exchange: it never cancels orders (exchange-side protective orders stay), never closes positions,
never trades, and never "fixes" balances. The exchange is authoritative for live account state; a difference DEWAKO cannot
explain blocks new live orders until a person (or a documented automatic rule) resolves each finding.

Per exchange the scope state machine in `state.py` is used: HEALTHY -> RECONCILING -> HEALTHY | RECONCILIATION_REQUIRED | FAILED.

Finding kinds
  order_only_local, order_only_remote, order_quantity_mismatch, order_side_mismatch, order_price_mismatch, order_status_mismatch,
  unresolved_order, duplicate_state, position_missing, position_unexpected, position_quantity_mismatch, position_side_mismatch,
  position_price_mismatch, position_leverage_mismatch, position_unresolved, balance_unexplained, balance_inconsistent,
  fill_missing_locally, fill_local_exceeds_remote, fill_unexpected_external, fill_local_only, fill_duplicate_remote (only for adapters whose
  capabilities().fill_history is True; others are reported as "fills not compared", never as clean)
Condition findings (e.g. order_only_local) clear automatically when a later run no longer sees the condition. HUMAN_ONLY
findings describe an event (a balance that moved for reasons DEWAKO cannot see, e.g. a manual withdrawal) and stay OPEN until an
ADMIN resolves them with a written resolution.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from dewako.exchanges.base import AccountBalance, ExchangeAdapter
from dewako.execution.engine import ExecutionEngine
from dewako.execution.state_machine import OrderState as S
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, now_iso
from dewako.reconciliation.state import ReconState
from dewako.safety.controller import NON_HUMAN, NotAuthorized, SafetyController

QTY_TOL = 1e-8
PRICE_REL_TOL = 0.005           # average-price difference tolerated between our books and the exchange
FEE_REL_TOL = 0.003             # fees DEWAKO does not ledger: tolerated on quote-asset balance changes (of traded notional)
ABS_BAL_TOL = 1e-6
HUMAN_ONLY = {"balance_unexplained", "balance_inconsistent", "fill_missing_locally", "fill_local_exceeds_remote", "fill_unexpected_external",
              "fill_local_only", "fill_duplicate_remote"}


def _key(f: dict) -> str:
    return json.dumps({k: f[k] for k in sorted(f) if k in ("type", "order_id", "client_order_id", "symbol", "asset", "position_id")}, sort_keys=True)


class ReconciliationService:
    def __init__(self, db: Database, safety: SafetyController, engine: ExecutionEngine, audit: Optional[AuditLog] = None,
                 stale_after: timedelta = timedelta(minutes=5)):
        self.db, self.safety, self.engine = db, safety, engine
        self.audit = audit or AuditLog(db)
        self.state = ReconState(db)
        self.stale_after = stale_after

    # ------------------------------------------------------------------ run
    async def run(self, exchange: str, correlation_id: str = "", environment: str = "LIVE") -> dict:
        adapter: ExchangeAdapter = self.engine.adapters[exchange]
        scope = f"exchange:{exchange}"
        started = now_iso()
        self.state.transition(scope, "RECONCILING", "run started", "reconciler", correlation_id)
        try:
            # 1. Settle our own unresolved orders by asking the exchange about each client id (read-only).
            for o in self.engine.orders.in_states({S.SUBMITTING, S.UNKNOWN, S.RECONCILING}, environment):
                if o["exchange"] == exchange:
                    await self.engine.reconcile_order(o["id"])
            ex_open = await adapter.get_open_orders()
            ex_pos = await adapter.get_positions()
            ex_bal = await adapter.get_account_snapshot()
            ex_fills = await adapter.get_fills() if adapter.capabilities().fill_history else None
        except Exception as e:
            return self._finish(exchange, scope, started, "ERROR", [{"type": "exchange_unreachable", "detail": type(e).__name__}], correlation_id)

        findings: list[dict] = []
        self._orders(exchange, environment, ex_open, findings)
        self._positions(exchange, environment, ex_pos, findings)
        if ex_fills is not None:      # an adapter that cannot report fills is NOT compared and NOT assumed clean: see docs/EXCHANGE_ADAPTERS.md
            self._fills(exchange, environment, ex_fills, findings)
        taken = now_iso()
        self._balances(exchange, environment, ex_bal, taken, findings)
        self._settle_orders_scope(correlation_id, environment)
        return self._finish(exchange, scope, started, "MISMATCH" if findings else "OK", findings, correlation_id,
                            snapshot=(exchange, environment, ex_bal, taken))

    # ------------------------------------------------------------------ orders
    def _orders(self, exchange: str, environment: str, ex_open, findings: list) -> None:
        with self.db.read() as c:
            db_active = [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE environment=? AND exchange=? AND state IN ('SUBMITTED','OPEN','PARTIALLY_FILLED','CANCEL_REQUESTED','SUBMITTING','UNKNOWN','RECONCILING')",
                (environment, exchange))]
        now = datetime.now(timezone.utc)
        ex_by_cid: dict = {}
        for o in ex_open:
            if o.client_order_id in ex_by_cid:
                findings.append({"type": "duplicate_state", "client_order_id": o.client_order_id})
            ex_by_cid[o.client_order_id] = o
        db_cids = {o["client_order_id"] for o in db_active}
        for o in db_active:
            if o["state"] in ("SUBMITTING", "UNKNOWN", "RECONCILING"):
                upd = o["updated_at"]
                upd = datetime.fromisoformat(upd) if isinstance(upd, str) else upd
                findings.append({"type": "unresolved_order", "order_id": o["id"], "state": o["state"], "stale": now - upd > self.stale_after})
                continue
            ex = ex_by_cid.get(o["client_order_id"])
            if ex is None:
                findings.append({"type": "order_only_local", "order_id": o["id"], "detail": "we think it is open; the exchange does not list it"})
                continue
            if ex.side.value != o["side"]:
                findings.append({"type": "order_side_mismatch", "order_id": o["id"], "ours": o["side"], "exchange": ex.side.value})
            if abs(ex.quantity - o["quantity"]) > QTY_TOL:
                findings.append({"type": "order_quantity_mismatch", "order_id": o["id"], "field": "quantity", "ours": o["quantity"], "exchange": ex.quantity})
            if abs(ex.filled - o["filled"]) > QTY_TOL:
                findings.append({"type": "order_quantity_mismatch", "order_id": o["id"], "field": "filled", "ours": o["filled"], "exchange": ex.filled})
            if ex.price is not None and o["price"] is not None and o["order_type"] != "MARKET" and abs(ex.price - o["price"]) > 1e-9:
                findings.append({"type": "order_price_mismatch", "order_id": o["id"], "ours": o["price"], "exchange": ex.price})
            theirs = {"OPEN": "OPEN", "NEW": "OPEN", "PARTIAL": "PARTIALLY_FILLED"}.get(ex.status.value)
            if theirs and o["state"] in ("OPEN", "PARTIALLY_FILLED") and theirs != o["state"] and abs(ex.filled - o["filled"]) <= QTY_TOL:
                findings.append({"type": "order_status_mismatch", "order_id": o["id"], "ours": o["state"], "exchange": theirs})
        for cid in ex_by_cid:
            if cid not in db_cids:
                findings.append({"type": "order_only_remote", "client_order_id": cid, "detail": "exchange has an open order DEWAKO does not know"})

    # ------------------------------------------------------------------ fills
    def _fills(self, exchange: str, environment: str, fills: list, findings: list) -> None:
        """Compare exchange fill history with our orders. Only findings are written; no fill is ever created, edited or deleted here."""
        seen: set = set()
        by_cid: dict[str, float] = {}
        for f in fills:
            if f.exchange_trade_id in seen:
                findings.append({"type": "fill_duplicate_remote", "client_order_id": f.client_order_id or "", "symbol": f.symbol,
                                 "detail": f"exchange reported trade {f.exchange_trade_id} more than once (counted once)"})
                continue
            seen.add(f.exchange_trade_id)
            by_cid[f.client_order_id or f"?{f.exchange_order_id}"] = by_cid.get(f.client_order_id or f"?{f.exchange_order_id}", 0.0) + f.quantity
        with self.db.read() as c:
            local = {r["client_order_id"]: dict(r) for r in c.execute(
                "SELECT client_order_id, id, filled, state, symbol FROM orders WHERE environment=? AND exchange=?", (environment, exchange))}
        for cid, qty in sorted(by_cid.items()):
            o = local.get(cid)
            if o is None:
                findings.append({"type": "fill_unexpected_external", "client_order_id": cid, "quantity": qty,
                                 "detail": "the exchange reports fills for an order DEWAKO never created (manual or external trade)"})
            elif qty > o["filled"] + QTY_TOL:
                findings.append({"type": "fill_missing_locally", "order_id": o["id"], "client_order_id": cid, "ours": o["filled"], "exchange": qty, "state": o["state"],
                                 "detail": "fills exist on the exchange that our records do not (late or lost fill); NOT applied automatically"})
            elif qty < o["filled"] - QTY_TOL:
                findings.append({"type": "fill_local_exceeds_remote", "order_id": o["id"], "client_order_id": cid, "ours": o["filled"], "exchange": qty,
                                 "detail": "we recorded more filled quantity than the exchange's fill history supports"})
        for cid, o in local.items():
            if o["filled"] > QTY_TOL and cid not in by_cid:
                findings.append({"type": "fill_local_only", "order_id": o["id"], "client_order_id": cid, "ours": o["filled"],
                                 "detail": "we recorded fills; the exchange's fill history has none for this order"})

    # ------------------------------------------------------------------ positions
    def _positions(self, exchange: str, environment: str, ex_pos: list, findings: list) -> None:
        with self.db.read() as c:
            db_pos = [dict(r) for r in c.execute("SELECT * FROM positions WHERE environment=? AND exchange=? AND status<>'CLOSED'", (environment, exchange))]
        ours = {p["symbol"]: p for p in db_pos}
        theirs: dict = {}
        for p in ex_pos:
            if p.get("quantity"):
                theirs[p["symbol"]] = p
        for p in db_pos:
            if p["status"] in ("UNKNOWN", "RECONCILING"):
                findings.append({"type": "position_unresolved", "position_id": p["id"], "symbol": p["symbol"]})
        for sym in sorted(set(ours) | set(theirs)):
            o, t = ours.get(sym), theirs.get(sym)
            if o and not t:
                findings.append({"type": "position_missing", "symbol": sym, "ours": o["quantity"], "detail": "we hold it; the exchange reports none"})
            elif t and not o:
                findings.append({"type": "position_unexpected", "symbol": sym, "exchange": float(t["quantity"]), "detail": "the exchange holds it; DEWAKO has no open position"})
            else:
                if abs(o["quantity"] - float(t["quantity"])) > QTY_TOL:
                    findings.append({"type": "position_quantity_mismatch", "symbol": sym, "ours": o["quantity"], "exchange": float(t["quantity"])})
                if t.get("side") and str(t["side"]).upper() != o["side"]:
                    findings.append({"type": "position_side_mismatch", "symbol": sym, "ours": o["side"], "exchange": t["side"]})
                if t.get("average_price") and o["average_price"] and abs(float(t["average_price"]) - o["average_price"]) / o["average_price"] > PRICE_REL_TOL:
                    findings.append({"type": "position_price_mismatch", "symbol": sym, "ours": o["average_price"], "exchange": t["average_price"]})
                if t.get("leverage") is not None and abs(float(t["leverage"]) - o["leverage"]) > 1e-9:
                    findings.append({"type": "position_leverage_mismatch", "symbol": sym, "ours": o["leverage"], "exchange": t["leverage"]})
        # Fields the adapter does not report are NOT compared and NOT invented (spot adapters report neither side nor average price).

    # ------------------------------------------------------------------ balances
    def _balances(self, exchange: str, environment: str, ex_bal: list[AccountBalance], taken: str, findings: list) -> None:
        for b in ex_bal:
            if abs(b.available + b.locked - b.total) > ABS_BAL_TOL or min(b.total, b.available, b.locked) < -ABS_BAL_TOL:
                findings.append({"type": "balance_inconsistent", "asset": b.asset, "detail": "available + locked != total (or negative)"})
        with self.db.read() as c:
            prev_rows = c.execute(
                "SELECT s.* FROM account_snapshots s WHERE s.exchange=? AND s.environment=? AND s.source='EXCHANGE' AND s.taken_at="
                "(SELECT MAX(taken_at) FROM account_snapshots WHERE exchange=? AND environment=? AND source='EXCHANGE')",
                (exchange, environment, exchange, environment)).fetchall()
            if not prev_rows:
                return  # first snapshot: it becomes the baseline. There is nothing earlier to compare against.
            prev = {r["asset"]: dict(r) for r in prev_rows}
            since = prev_rows[0]["taken_at"]
            trades = [dict(r) for r in c.execute("SELECT symbol, side, quantity, price FROM trades WHERE environment=? AND created_at > ? AND order_id IN "
                                                 "(SELECT id FROM orders WHERE exchange=?)", (environment, since, exchange))]
        explained: dict[str, float] = {}
        tol: dict[str, float] = {}
        for t in trades:
            base, quote = t["symbol"].split("/")
            sign = 1 if t["side"] == "BUY" else -1
            explained[base] = explained.get(base, 0.0) + sign * t["quantity"]
            explained[quote] = explained.get(quote, 0.0) - sign * t["quantity"] * t["price"]
            tol[quote] = tol.get(quote, 0.0) + t["quantity"] * t["price"] * FEE_REL_TOL
            tol[base] = tol.get(base, 0.0) + t["quantity"] * FEE_REL_TOL      # fees can be charged in the base asset
        now_by = {b.asset: b for b in ex_bal}
        for asset in sorted(set(prev) | set(now_by)):
            before = prev[asset]["total"] if asset in prev else 0.0
            after = now_by[asset].total if asset in now_by else 0.0
            delta = after - before
            if abs(delta - explained.get(asset, 0.0)) > tol.get(asset, 0.0) + ABS_BAL_TOL:
                findings.append({"type": "balance_unexplained", "asset": asset, "before": before, "after": after,
                                 "explained_by_our_fills": explained.get(asset, 0.0), "tolerance": tol.get(asset, 0.0) + ABS_BAL_TOL,
                                 "detail": "balance changed by an amount DEWAKO's own fills do not explain (deposit, withdrawal, fee, manual trade?)"})

    def _settle_orders_scope(self, cid: str, environment: str) -> None:
        """The 'orders' scope is raised when an order outcome was unknown. It clears only when nothing is unresolved any more."""
        cur = self.state.current("orders")
        if cur in (None, "HEALTHY"):
            return
        with self.db.read() as c:
            n = c.execute("SELECT COUNT(*) FROM orders WHERE state IN ('SUBMITTING','UNKNOWN','RECONCILING')").fetchone()[0]
        if n == 0:
            if cur != "RECONCILING":
                self.state.transition("orders", "RECONCILING", "all unresolved orders now settled", "reconciler", cid)
            self.state.transition("orders", "HEALTHY", "no unresolved orders remain", "reconciler", cid)
            self.audit.record("RECONCILIATION_RESOLVED", "OK", cid, resource="scope:orders")

    # ------------------------------------------------------------------ finish: persist run, findings, state
    def _finish(self, exchange: str, scope: str, started: str, result: str, findings: list, cid: str, snapshot=None) -> dict:
        with self.db.tx() as c:
            run_id = c.execute("INSERT INTO reconciliation_runs(exchange,started_at,finished_at,result,findings) VALUES (?,?,?,?,?)",
                               (exchange, started, now_iso(), result, json.dumps(findings, default=str))).lastrowid
            if snapshot:
                ex, env, bal, taken = snapshot
                for b in bal:
                    c.execute("INSERT INTO account_snapshots(exchange,environment,account_type,asset,total,available,locked,margin_balance,available_margin,"
                              "unrealized_pnl,position_margin,source,taken_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (ex, env, b.account_type, b.asset, b.total, b.available, b.locked, b.margin_balance, b.available_margin,
                               b.unrealized_pnl, b.position_margin, "EXCHANGE", taken))
            # record findings; auto-clear condition findings that are gone
            current = {_key(f): f for f in findings}
            open_rows = [dict(r) for r in c.execute("SELECT id, finding_type, detail FROM reconciliation_findings WHERE scope=? AND status='OPEN'", (scope,))]
            known = set()
            for r in open_rows:
                k = json.loads(r["detail"]).get("_key")
                known.add(k)
                if k not in current and r["finding_type"] not in HUMAN_ONLY:
                    c.execute("UPDATE reconciliation_findings SET status='RESOLVED', resolution=?, resolved_by='reconciler', resolved_at=? WHERE id=?",
                              (f"condition no longer present in run {run_id}", now_iso(), r["id"]))
                    self.audit.record("RECONCILIATION_RESOLVED", "OK", cid, resource=f"finding:{r['id']}", metadata={"rule": "condition cleared", "run": run_id}, conn=c)
            for k, f in current.items():
                if k not in known:
                    c.execute("INSERT INTO reconciliation_findings(run_id,scope,finding_type,detail,status,created_at) VALUES (?,?,?,?,?,?)",
                              (run_id, scope, f["type"], json.dumps({**f, "_key": k}, default=str), "OPEN", now_iso()))
            still_open = c.execute("SELECT COUNT(*) FROM reconciliation_findings WHERE scope=? AND status='OPEN'", (scope,)).fetchone()[0]
            if result == "ERROR":
                new, why = "FAILED", "could not read the exchange"
            elif result == "OK" and still_open == 0:
                new, why = "HEALTHY", "clean run"
            else:
                new, why = "RECONCILIATION_REQUIRED", f"{len(findings)} finding(s) this run, {still_open} open in total"
            self.state.transition(scope, new, why, "reconciler", cid, run_id=run_id, conn=c)
            if new != "HEALTHY":
                self.audit.record("RECONCILIATION_FAILED", result, cid, resource=scope, metadata={"findings": [f["type"] for f in findings], "run": run_id}, conn=c)
        if new != "HEALTHY":
            self.safety.block_live(f"reconciliation {new}")
            self.safety.alert(f"Reconciliation {new} for {exchange}: {why}", cid)
        return {"result": result, "state": new, "findings": findings, "run_id": run_id}

    # ------------------------------------------------------------------ explicit resolution by a person
    def open_findings(self, scope: Optional[str] = None) -> list[dict]:
        with self.db.read() as c:
            q, a = "SELECT * FROM reconciliation_findings WHERE status='OPEN'", []
            if scope:
                q += " AND scope=?"; a.append(scope)
            return [dict(r) for r in c.execute(q + " ORDER BY id", a)]

    def resolve_finding(self, finding_id: int, actor: dict, resolution: str, correlation_id: str = "") -> None:
        """Only an ADMIN can resolve a finding, and must say why. DEWAKO's books are NOT edited; this records a decision.
        When no OPEN finding is left the scope becomes RESOLVED, and a later clean run is still required to become HEALTHY."""
        if actor.get("role") != "ADMIN" or str(actor.get("username", "")).lower() in NON_HUMAN:
            self.audit.record("RECONCILIATION_RESOLVED", "DENIED", correlation_id, actor=actor, resource=f"finding:{finding_id}")
            raise NotAuthorized("only a human ADMIN may resolve reconciliation findings")
        if len(resolution.strip()) < 8:
            raise ValueError("a written resolution (at least 8 characters) is required")
        with self.db.tx() as c:
            r = c.execute("SELECT scope, status FROM reconciliation_findings WHERE id=?", (finding_id,)).fetchone()
            if r is None:
                raise KeyError(finding_id)
            if r["status"] != "OPEN":
                raise ValueError("finding is already resolved")
            c.execute("UPDATE reconciliation_findings SET status='RESOLVED', resolution=?, resolved_by=?, resolved_at=? WHERE id=?",
                      (resolution[:500], actor["username"], now_iso(), finding_id))
            self.audit.record("RECONCILIATION_RESOLVED", "OK", correlation_id, actor=actor, resource=f"finding:{finding_id}", metadata={"resolution": resolution[:200]}, conn=c)
            left = c.execute("SELECT COUNT(*) FROM reconciliation_findings WHERE scope=? AND status='OPEN'", (r["scope"],)).fetchone()[0]
            if left == 0 and self.state.current(r["scope"]) == "RECONCILIATION_REQUIRED":
                self.state.transition(r["scope"], "RESOLVED", "every finding resolved; a clean run is still required", actor["username"], correlation_id, conn=c)
