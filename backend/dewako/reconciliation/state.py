"""Explicit reconciliation state machine, one row per scope (e.g. 'exchange:binance', 'orders').

HEALTHY -> RECONCILING -> HEALTHY | RECONCILIATION_REQUIRED | FAILED
RECONCILIATION_REQUIRED -> RECONCILING | RESOLVED         (RESOLVED = every finding explicitly resolved; a clean run is still needed)
RESOLVED -> RECONCILING | HEALTHY
FAILED -> RECONCILING
Every transition is persisted with reason, actor and correlation id. Anything other than HEALTHY blocks new live orders.
"""
from __future__ import annotations

from typing import Any, Optional

from dewako.persistence.db import Database, now_iso

STATES = ("HEALTHY", "RECONCILING", "RECONCILIATION_REQUIRED", "RESOLVED", "FAILED")
ALLOWED: dict[Optional[str], set[str]] = {
    None: set(STATES),
    "HEALTHY": {"RECONCILING", "RECONCILIATION_REQUIRED"},
    "RECONCILING": {"HEALTHY", "RECONCILIATION_REQUIRED", "FAILED", "RECONCILING"},
    "RECONCILIATION_REQUIRED": {"RECONCILING", "RESOLVED", "RECONCILIATION_REQUIRED"},
    "RESOLVED": {"RECONCILING", "HEALTHY", "RECONCILIATION_REQUIRED"},
    "FAILED": {"RECONCILING", "RECONCILIATION_REQUIRED", "FAILED"},
}


class IllegalReconciliationTransition(Exception):
    pass


class ReconState:
    def __init__(self, db: Database):
        self.db = db

    def current(self, scope: str) -> Optional[str]:
        with self.db.read() as c:
            r = c.execute("SELECT state FROM reconciliation_state WHERE scope=?", (scope,)).fetchone()
        return r["state"] if r else None

    def transition(self, scope: str, new: str, reason: str, actor: str = "reconciler", correlation_id: str = "",
                   run_id: Optional[int] = None, conn: Optional[Any] = None) -> Optional[str]:
        """Move `scope` to `new`. Returns the previous state. Illegal moves raise."""
        def run(c) -> Optional[str]:
            r = c.execute("SELECT state FROM reconciliation_state WHERE scope=?", (scope,)).fetchone()
            prev = r["state"] if r else None
            if new not in ALLOWED[prev]:
                raise IllegalReconciliationTransition(f"{scope}: {prev} -> {new} is not allowed")
            ts = now_iso()
            if r is None:
                c.execute("INSERT INTO reconciliation_state(scope,state,reason,last_run_id,updated_at) VALUES (?,?,?,?,?) ON CONFLICT (scope) DO NOTHING",
                          (scope, new, reason[:500], run_id, ts))
            else:
                n = c.execute("UPDATE reconciliation_state SET state=?, reason=?, last_run_id=COALESCE(?, last_run_id), updated_at=? WHERE scope=? AND state=?",
                              (new, reason[:500], run_id, ts, scope, prev)).rowcount
                if n != 1:
                    raise IllegalReconciliationTransition(f"{scope} changed concurrently")
            c.execute("INSERT INTO reconciliation_events(scope,previous_state,new_state,reason,actor,correlation_id,created_at) VALUES (?,?,?,?,?,?,?)",
                      (scope, prev, new, reason[:500], actor, correlation_id or "", ts))
            return prev
        if conn is not None:
            return run(conn)
        with self.db.tx() as c:
            return run(c)

    def blocking(self) -> list[tuple[str, str]]:
        """Scopes that are not HEALTHY. Empty means nothing is blocking."""
        with self.db.read() as c:
            return [(r["scope"], r["state"]) for r in c.execute("SELECT scope, state FROM reconciliation_state WHERE state <> 'HEALTHY' ORDER BY scope")]

    def events(self, scope: Optional[str] = None) -> list[dict]:
        with self.db.read() as c:
            q, a = "SELECT * FROM reconciliation_events", []
            if scope:
                q += " WHERE scope=?"; a.append(scope)
            return [dict(r) for r in c.execute(q + " ORDER BY id", a)]
