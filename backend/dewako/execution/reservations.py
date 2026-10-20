"""Durable risk reservations.

A risk-increasing order must RESERVE capacity in the same transaction that creates its intent and order. The reservation is
checked against the durable state of the whole account scope (committed positions + other ACTIVE reservations), under a lock
that serialises concurrent workers: a `SELECT ... FOR UPDATE` on the scope row in PostgreSQL, the single-writer lock
(BEGIN IMMEDIATE) in SQLite. Two requests therefore cannot each pass the check against the same free capacity.

Lifecycle (driven only by validated order transitions, inside the same transaction as the transition):
  ACTIVE   from order creation. Stays ACTIVE while the order is SUBMITTING / SUBMITTED / OPEN / PARTIALLY_FILLED / UNKNOWN / RECONCILING
           (unknown outcomes are treated conservatively: the capacity stays reserved).
  CONSUMED when the order is FILLED (the position now carries the exposure).
  RELEASED when the order ends REJECTED / FAILED / CANCELLED / EXPIRED (a partial fill, if any, is carried by the position).
A rollback of the creating transaction removes the reservation with the order; committed reservations survive restarts.
Conservative by design: a partially filled order is counted both by its position and by its still-ACTIVE reservation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from dewako.persistence.db import now_iso

TERMINAL_RELEASE = ("REJECTED", "FAILED", "CANCELLED", "EXPIRED")


@dataclass
class ReserveResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def scope_of(environment: str, exchange: str) -> str:
    return f"{environment}:{exchange}"


def lock_scope(db: Any, c: Any, scope: str) -> None:
    c.execute("INSERT INTO risk_scopes(scope,created_at) VALUES (?,?) ON CONFLICT (scope) DO NOTHING", (scope, now_iso()))
    if db.is_pg:
        c.execute("SELECT scope FROM risk_scopes WHERE scope=? FOR UPDATE", (scope,))     # blocks other workers until this transaction ends
    # SQLite: db.tx() began with BEGIN IMMEDIATE, so this transaction already holds the only write lock.


def committed_state(c: Any, environment: str, exchange: str, scope: str) -> dict:
    pos = c.execute("SELECT COALESCE(SUM(quantity * COALESCE(average_price, entry_price, 0)),0) AS cost, COUNT(*) AS n FROM positions "
                    "WHERE environment=? AND exchange=? AND status<>'CLOSED'", (environment, exchange)).fetchone()
    res = c.execute("SELECT COALESCE(SUM(notional),0) AS notional, COALESCE(SUM(opens_position),0) AS opens FROM risk_reservations "
                    "WHERE scope=? AND status='ACTIVE'", (scope,)).fetchone()
    return {"position_cost": float(pos["cost"]), "open_positions": int(pos["n"]),
            "reserved_notional": float(res["notional"]), "reserved_new_positions": int(res["opens"])}


def reserve(c: Any, *, order_id: int, scope: str, environment: str, exchange: str, bot_id: str, symbol: str, side: str,
            quantity: float, price: float) -> None:
    """Insert the ACTIVE reservation (the checks have already passed under the scope lock)."""
    pos = c.execute("SELECT 1 FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                    (bot_id, exchange, environment, symbol)).fetchone()
    ts = now_iso()
    c.execute("INSERT INTO risk_reservations(order_id,scope,bot_id,symbol,side,quantity,notional,opens_position,status,created_at,updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (order_id, scope, bot_id, symbol, side, quantity, quantity * price, 0 if pos else 1, "ACTIVE", ts, ts))


def on_order_transition(c: Any, order_id: int, new_state: str) -> None:
    """Called by OrderStore.transition inside the same transaction."""
    if new_state == "FILLED":
        status = "CONSUMED"
    elif new_state in TERMINAL_RELEASE:
        status = "RELEASED"
    else:
        return
    c.execute("UPDATE risk_reservations SET status=?, updated_at=? WHERE order_id=? AND status='ACTIVE'", (status, now_iso(), order_id))


def check_limits(limits: Any, *, equity: float, quote_balance: float, notional: float, st: dict, new_position: bool,
                 exposure_increasing: bool) -> list[str]:
    """Hard caps evaluated under the lock. These are the server-side backstop; the RiskEngine decision is a separate, earlier check."""
    if not exposure_increasing:
        return []
    why = []
    if equity <= 0:
        return ["equity is zero or unknown; risk-increasing orders are blocked"]
    if notional > equity * limits.max_position_pct / 100.0 + 1e-9:
        why.append(f"order notional {notional:.2f} exceeds {limits.max_position_pct}% of equity")
    total = st["position_cost"] + st["reserved_notional"] + notional
    if total > equity * limits.max_exposure_pct / 100.0 + 1e-9:
        why.append(f"total exposure {total:.2f} would exceed {limits.max_exposure_pct}% of equity")
    if new_position and st["open_positions"] + st["reserved_new_positions"] + 1 > limits.max_open_positions:
        why.append(f"open positions would exceed {limits.max_open_positions}")
    if notional > quote_balance - st["reserved_notional"] + 1e-9:
        why.append(f"available balance {quote_balance - st['reserved_notional']:.2f} (after reservations) is below the order notional {notional:.2f}")
    return why
