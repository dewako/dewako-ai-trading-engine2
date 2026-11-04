"""Durable per-exit tracking (Stage 2 WP3).

One row per SELL/EXIT order: position, reason, requested / filled / remaining quantity, order id, status and reconciliation status.
It is DERIVED from the order row inside the same transaction as every order creation and transition (hooked in OrderStore), so it can
never disagree with the order and survives a restart because it is in the database. It never sends or changes an order.

A position is REDUCING while a tracked exit of it has filled part of its quantity and is still active; it goes back to OPEN when
that exit ends with quantity left, and is CLOSED only by PositionStore.reduce when nothing is left.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from dewako.persistence.db import now_iso

ACTIVE = ("PENDING", "PARTIAL", "UNKNOWN")
_REASON = re.compile(r"^exit:\d+:([A-Z_]+):\d+$")
_STATUS = {"CREATED": "PENDING", "SUBMITTING": "PENDING", "SUBMITTED": "PENDING", "OPEN": "PENDING", "CANCEL_REQUESTED": "PENDING",
           "PARTIALLY_FILLED": "PARTIAL", "UNKNOWN": "UNKNOWN", "RECONCILING": "UNKNOWN", "FILLED": "COMPLETE",
           "CANCELLED": "CANCELLED", "EXPIRED": "CANCELLED", "REJECTED": "FAILED", "FAILED": "FAILED"}


def sync(c: Any, order_id: int) -> None:
    """Create or refresh the tracking row of an EXIT order (no-op for any other order). Runs in the caller's transaction."""
    o = c.execute("SELECT o.*, i.intent AS intent, i.signal_id AS signal_id FROM orders o JOIN execution_intents i ON i.id=o.intent_id WHERE o.id=?",
                  (order_id,)).fetchone()
    if o is None or o["side"] != "SELL" or o["intent"] != "EXIT":
        return
    status = _STATUS.get(o["state"], "UNKNOWN")
    recon = "REQUIRED" if o["state"] in ("UNKNOWN", "RECONCILING") else "CLEAR"
    filled = float(o["filled"] or 0.0)
    remaining = max(float(o["quantity"]) - filled, 0.0)
    ts = now_iso()
    row = c.execute("SELECT id, filled_quantity FROM position_exits WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        pos = c.execute("SELECT id FROM positions WHERE bot_id=? AND exchange=? AND environment=? AND symbol=? AND status<>'CLOSED'",
                        (o["bot_id"], o["exchange"], o["environment"], o["symbol"])).fetchone()
        m = _REASON.match(o["signal_id"] or "")
        c.execute("INSERT INTO position_exits(order_id,position_id,bot_id,exchange,environment,symbol,reason,requested_quantity,filled_quantity,"
                  "remaining_quantity,status,reconciliation_status,created_at,updated_at,last_progress_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (order_id, pos["id"] if pos else None, o["bot_id"], o["exchange"], o["environment"], o["symbol"], m.group(1) if m else "UNSPECIFIED",
                   o["quantity"], filled, remaining, status, recon, ts, ts, ts))
        position_id = pos["id"] if pos else None
    else:
        progressed = filled > float(row["filled_quantity"]) + 1e-12
        c.execute("UPDATE position_exits SET filled_quantity=?, remaining_quantity=?, status=?, reconciliation_status=?, updated_at=?"
                  + (", last_progress_at=?" if progressed else "") + " WHERE order_id=?",
                  (filled, remaining, status, recon, ts, *((ts,) if progressed else ()), order_id))
        position_id = c.execute("SELECT position_id FROM position_exits WHERE order_id=?", (order_id,)).fetchone()["position_id"]
    if position_id is not None:
        _follow_position(c, position_id, o["id"], status, filled)


def has_active_exit(c: Any, position_id: int, *, partial_only: bool = False) -> bool:
    marks = ("'PARTIAL'",) if partial_only else ("'PENDING'", "'PARTIAL'", "'UNKNOWN'")
    return c.execute(f"SELECT 1 FROM position_exits WHERE position_id=? AND status IN ({','.join(marks)})", (position_id,)).fetchone() is not None


def _follow_position(c: Any, position_id: int, order_id: int, status: str, filled: float) -> None:
    """OPEN -> REDUCING when an exit has partly filled; REDUCING -> OPEN when no exit with a partial fill is active any more.
    The fill itself (quantity, CLOSED) is applied by PositionStore.reduce in the same transaction."""
    p = c.execute("SELECT status FROM positions WHERE id=?", (position_id,)).fetchone()
    if p is None:
        return
    if status == "PARTIAL" and filled > 0 and p["status"] == "OPEN":
        c.execute("UPDATE positions SET status='REDUCING', updated_at=? WHERE id=?", (now_iso(), position_id))
        c.execute("INSERT INTO position_events(position_id,event_type,previous_status,new_status,quantity,price,order_id,correlation_id,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (position_id, "POSITION_STATUS", "OPEN", "REDUCING", None, None, order_id, "", now_iso()))
    elif status in ("CANCELLED", "FAILED") and p["status"] == "REDUCING" and not has_active_exit(c, position_id, partial_only=True):
        c.execute("UPDATE positions SET status='OPEN', updated_at=? WHERE id=?", (now_iso(), position_id))
        c.execute("INSERT INTO position_events(position_id,event_type,previous_status,new_status,quantity,price,order_id,correlation_id,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (position_id, "POSITION_STATUS", "REDUCING", "OPEN", None, None, order_id, "", now_iso()))


def active_exits(c: Any, position_id: int) -> list[dict]:
    return [dict(r) for r in c.execute("SELECT * FROM position_exits WHERE position_id=? AND status IN ('PENDING','PARTIAL','UNKNOWN') ORDER BY id", (position_id,))]
