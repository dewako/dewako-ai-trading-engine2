"""Persistent positions with an explicit state machine.

A LIVE position is only CLOSED by `confirm_close`, which the execution layer calls after the exchange reports the
closing order FILLED. Nothing else can set CLOSED.
"""
from __future__ import annotations

from typing import Any
from typing import Optional

from dewako.persistence.db import Database, now_iso

TRANSITIONS = {
    "OPENING": {"OPEN", "CLOSED", "UNKNOWN", "RECONCILING"},
    "OPEN": {"REDUCING", "CLOSING", "UNKNOWN", "RECONCILING", "OPEN"},
    "REDUCING": {"OPEN", "CLOSING", "CLOSED", "UNKNOWN", "RECONCILING"},
    "CLOSING": {"CLOSED", "OPEN", "UNKNOWN", "RECONCILING"},
    "UNKNOWN": {"RECONCILING"},
    "RECONCILING": {"OPEN", "CLOSED", "UNKNOWN"},
    "CLOSED": set(),
}


class PositionError(Exception):
    pass


class PositionStore:
    def __init__(self, db: Database):
        self.db = db

    def open_or_add(self, c: Any, *, bot_id: str, exchange: str, environment: str, symbol: str, side: str,
                    quantity: float, price: float, market_type: str = "SPOT", stop_loss: Optional[float] = None,
                    take_profit: Optional[float] = None, leverage: float = 1.0, order_id: Optional[int] = None,
                    correlation_id: str = "") -> int:
        """Called inside the order-fill transaction. Adds to the active position or opens one."""
        if market_type != "SPOT":
            raise PositionError(f"{market_type} positions are NOT_IMPLEMENTED")
        ts = now_iso()
        row = c.execute("SELECT * FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                        (bot_id, exchange, environment, symbol)).fetchone()
        if row is None:
            pid = c.execute("INSERT INTO positions(bot_id,exchange,environment,market_type,symbol,side,quantity,entry_price,average_price,"
                             "leverage,stop_loss,take_profit,status,opened_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (bot_id, exchange, environment, market_type, symbol, side, quantity, price, price, leverage, stop_loss,
                              take_profit, "OPEN", ts, ts)).lastrowid
            self._event(c, pid, "POSITION_OPENED", None, "OPEN", quantity, price, order_id, correlation_id)
            return pid
        if row["side"] != side:
            raise PositionError("opposite-side fill must go through reduce/close, not open_or_add")
        if row["status"] not in ("OPEN", "OPENING"):
            raise PositionError(f"position is {row['status']}; cannot add")
        q = row["quantity"] + quantity
        avg = (row["quantity"] * (row["average_price"] or price) + quantity * price) / q
        c.execute("UPDATE positions SET quantity=?, average_price=?, updated_at=? WHERE id=?", (q, avg, ts, row["id"]))
        self._event(c, row["id"], "POSITION_INCREASED", row["status"], row["status"], quantity, price, order_id, correlation_id)
        return row["id"]

    def reduce(self, c: Any, *, bot_id: str, exchange: str, environment: str, symbol: str, quantity: float,
               price: float, exchange_confirmed: bool, order_id: Optional[int] = None, correlation_id: str = "") -> tuple[Optional[int], float, bool]:
        """Apply a confirmed closing fill. Returns (position_id, realized_pnl, closed).
        `exchange_confirmed` must be True for LIVE (the order is FILLED on the exchange); otherwise nothing is closed."""
        row = c.execute("SELECT * FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                        (bot_id, exchange, environment, symbol)).fetchone()
        if row is None:
            raise PositionError("no active position to reduce")
        if not exchange_confirmed:
            raise PositionError("a position may only be reduced or closed from an exchange-confirmed fill")
        if quantity > row["quantity"] + 1e-12:
            raise PositionError("closing quantity exceeds the position (short/flip not implemented for spot)")
        pnl = (price - row["average_price"]) * quantity * (1 if row["side"] == "BUY" else -1)
        left = row["quantity"] - quantity
        ts = now_iso()
        if left <= 1e-12:
            c.execute("UPDATE positions SET quantity=0, status='CLOSED', realized_pnl=realized_pnl+?, closed_at=?, updated_at=? WHERE id=?",
                      (pnl, ts, ts, row["id"]))
            self._event(c, row["id"], "POSITION_CLOSED", row["status"], "CLOSED", quantity, price, order_id, correlation_id)
            return row["id"], pnl, True
        c.execute("UPDATE positions SET quantity=?, status='OPEN', realized_pnl=realized_pnl+?, updated_at=? WHERE id=?",
                  (left, pnl, ts, row["id"]))
        self._event(c, row["id"], "POSITION_REDUCED", row["status"], "OPEN", quantity, price, order_id, correlation_id)
        return row["id"], pnl, False

    @staticmethod
    def _event(c: Any, position_id: int, event: str, prev: Optional[str], new: str, qty: Optional[float], price: Optional[float],
               order_id: Optional[int], correlation_id: str) -> None:
        c.execute("INSERT INTO position_events(position_id,event_type,previous_status,new_status,quantity,price,order_id,correlation_id,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (position_id, event, prev, new, qty, price, order_id, correlation_id or "", now_iso()))

    def set_status(self, position_id: int, new: str, c: Optional[Any] = None) -> None:
        if new == "CLOSED":
            raise PositionError("CLOSED can only be set by an exchange-confirmed fill")
        def run(cc):
            r = cc.execute("SELECT status FROM positions WHERE id=?", (position_id,)).fetchone()
            if r is None:
                raise KeyError(position_id)
            if new not in TRANSITIONS[r["status"]]:
                raise PositionError(f"{r['status']} -> {new} is not allowed")
            cc.execute("UPDATE positions SET status=?, updated_at=? WHERE id=?", (new, now_iso(), position_id))
            self._event(cc, position_id, "POSITION_STATUS", r["status"], new, None, None, None, "")
        if c is not None:
            run(c)
        else:
            with self.db.tx() as cc:
                run(cc)

    def mark_incomplete_for_reconciliation(self) -> int:
        """Startup recovery: positions caught mid-change (OPENING/REDUCING/CLOSING/UNKNOWN) cannot be trusted. They become
        RECONCILING; the reconciliation run decides what they really are. Nothing is closed or opened here."""
        n = 0
        with self.db.tx() as c:
            for r in c.execute("SELECT id, status FROM positions WHERE status IN ('OPENING','REDUCING','CLOSING','UNKNOWN')").fetchall():
                if r["status"] != "UNKNOWN":
                    c.execute("UPDATE positions SET status='UNKNOWN', updated_at=? WHERE id=?", (now_iso(), r["id"]))
                    self._event(c, r["id"], "RECOVERY_UNCERTAIN", r["status"], "UNKNOWN", None, None, None, "")
                c.execute("UPDATE positions SET status='RECONCILING', updated_at=? WHERE id=?", (now_iso(), r["id"]))
                self._event(c, r["id"], "RECOVERY_RECONCILING", "UNKNOWN", "RECONCILING", None, None, None, "")
                n += 1
        return n

    def active(self, environment: str, bot_id: Optional[str] = None) -> list[dict]:
        q, a = "SELECT * FROM positions WHERE status<>'CLOSED' AND environment=?", [environment]
        if bot_id:
            q += " AND bot_id=?"; a.append(bot_id)
        with self.db.read() as c:
            return [dict(r) for r in c.execute(q, a)]

    def has_position(self, bot_id: str, environment: str) -> bool:
        return bool(self.active(environment, bot_id))
