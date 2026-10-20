"""Persistent orders: creation with an idempotency constraint, explicit transitions, and an event log.

Every transition runs in one transaction that (1) checks the current state against the previous state the caller
saw, (2) validates the transition, (3) updates the order and (4) appends the event. Nothing is changed in memory first.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from dewako.execution.state_machine import IllegalTransition, OrderState, check
from dewako.persistence.audit import scrub
from dewako.persistence.db import Database, now_iso


class StaleState(Exception):
    """Someone else changed the order between the read and the write."""


class RetryNotAllowed(Exception):
    pass


@dataclass
class CreatedOrder:
    order_id: int
    intent_id: int
    client_order_id: str
    created: bool          # False means this logical order already existed: take no action


def client_order_id(bot_id: str, signal_id: str, intent: str) -> str:
    """Deterministic and short enough for exchange limits (36 chars). The same logical order always gets the same id,
    so a resend after a confirmed-absent check is still idempotent on the exchange side."""
    return "DW" + hashlib.sha256(f"{bot_id}|{signal_id}|{intent}".encode()).hexdigest()[:30]


class OrderStore:
    def __init__(self, db: Database):
        self.db = db

    # ---- creation
    def create(self, *, bot_id: str, signal_id: str, intent: str, environment: str, exchange: str, symbol: str, side: str,
               order_type: str, quantity: float, price: Optional[float], correlation_id: str, market_type: str = "SPOT",
               risk_decision_id: str = "", actor: str = "execution", conn: Optional[Any] = None) -> CreatedOrder:
        """Atomically create the execution intent and its order. A second call for the same (bot, signal, intent) creates
        nothing and returns the existing order with created=False. The UNIQUE constraint decides, not Python."""
        def run(c: Any) -> CreatedOrder:
            ts = now_iso()
            # ON CONFLICT DO NOTHING (not catch-and-continue): PostgreSQL aborts a transaction after an error, and a second
            # concurrent writer must wait for the first to commit and then see its row. The UNIQUE constraint decides.
            cur = c.execute("INSERT INTO execution_intents(bot_id,signal_id,intent,environment,correlation_id,risk_decision_id,created_at)"
                            " VALUES (?,?,?,?,?,?,?) ON CONFLICT (bot_id, signal_id, intent) DO NOTHING",
                            (bot_id, signal_id, intent, environment, correlation_id, risk_decision_id, ts))
            if cur.rowcount == 0:
                row = c.execute("SELECT o.id AS oid, i.id AS iid, o.client_order_id AS cid FROM execution_intents i "
                                "JOIN orders o ON o.intent_id=i.id WHERE i.bot_id=? AND i.signal_id=? AND i.intent=?",
                                (bot_id, signal_id, intent)).fetchone()
                if row is None:  # the other writer has not committed an order for this intent: refuse rather than guess
                    raise RuntimeError("execution intent exists without an order; refusing to create a second one")
                return CreatedOrder(row["oid"], row["iid"], row["cid"], False)
            iid = cur.lastrowid
            cid = client_order_id(bot_id, signal_id, intent)
            cur = c.execute("INSERT INTO orders(intent_id,client_order_id,exchange,environment,market_type,bot_id,symbol,side,order_type,"
                            "quantity,price,state,correlation_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (iid, cid, exchange, environment, market_type, bot_id, symbol, side, order_type, quantity, price,
                             OrderState.CREATED.value, correlation_id, ts, ts))
            oid = cur.lastrowid
            c.execute("INSERT INTO order_events(order_id,event_type,previous_state,new_state,actor,correlation_id,created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (oid, "ORDER_CREATED", None, OrderState.CREATED.value, actor, correlation_id, ts))
            return CreatedOrder(oid, iid, cid, True)

        if conn is not None:
            return run(conn)
        with self.db.tx() as c:
            return run(c)

    # ---- transitions
    def transition(self, order_id: int, expected: OrderState, new: OrderState, event_type: str, *, actor: str = "execution",
                   correlation_id: str = "", exchange_order_id: Optional[str] = None, exchange_response: Any = None,
                   error: Optional[str] = None, filled: Optional[float] = None, avg_fill_price: Optional[float] = None,
                   conn: Optional[Any] = None) -> None:
        def run(c: Any) -> None:
            row = c.execute("SELECT state, correlation_id FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(order_id)
            cur = OrderState(row["state"])
            if cur is not expected:
                raise StaleState(f"order {order_id} is {cur.value}, caller expected {expected.value}")
            check(cur, new)
            if cur is OrderState.FAILED:
                last = c.execute("SELECT event_type FROM order_events WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
                if last is None or last["event_type"] != "CONFIRMED_ABSENT":
                    raise RetryNotAllowed("a retry is only allowed after the exchange confirmed it never accepted the order")
            ts = now_iso()
            sets, args = ["state=?", "updated_at=?"], [new.value, ts]
            if exchange_order_id:
                sets.append("exchange_order_id=?"); args.append(exchange_order_id)
            if filled is not None:
                sets.append("filled=?"); args.append(filled)
            if avg_fill_price is not None:
                sets.append("avg_fill_price=?"); args.append(avg_fill_price)
            if new is OrderState.SUBMITTING and cur is OrderState.FAILED:
                sets.append("attempt=attempt+1")
            n = c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=? AND state=?", (*args, order_id, cur.value)).rowcount
            if n != 1:
                raise StaleState(f"order {order_id} changed concurrently")
            from dewako.execution.reservations import on_order_transition
            on_order_transition(c, order_id, new.value)           # reservation follows the order, in the same transaction
            resp = json.dumps(scrub(exchange_response), default=str) if exchange_response is not None else None
            c.execute("INSERT INTO order_events(order_id,event_type,previous_state,new_state,exchange_order_id,exchange_response,error,actor,"
                      "correlation_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (order_id, event_type, cur.value, new.value, exchange_order_id, resp, error and error[:500], actor,
                       correlation_id or row["correlation_id"], ts))

        if conn is not None:
            run(conn)
        else:
            with self.db.tx() as c:
                run(c)

    # ---- reads
    def get(self, order_id: int) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if r is None:
            raise KeyError(order_id)
        return dict(r)

    def by_client_id(self, cid: str) -> Optional[dict]:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM orders WHERE client_order_id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def events(self, order_id: int) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))]

    def in_states(self, states: set[OrderState], environment: Optional[str] = None) -> list[dict]:
        marks = ",".join("?" * len(states))
        q, args = f"SELECT * FROM orders WHERE state IN ({marks})", [s.value for s in states]
        if environment:
            q += " AND environment=?"; args.append(environment)
        with self.db.read() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]
