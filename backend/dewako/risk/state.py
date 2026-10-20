"""Durable risk counters. Daily loss, drawdown, loss streak and cooldown survive a restart.

The RiskContext handed to the Risk Engine is built from these rows plus the positions table, not from caller-supplied
numbers, so a restart (or a careless caller) cannot reset them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from dewako.core.types import utcnow
from dewako.persistence.db import Database, now_iso
from dewako.risk.engine import RiskContext


class RiskStateStore:
    def __init__(self, db: Database):
        self.db = db

    def init_scope(self, scope: str, equity: float) -> None:
        if equity <= 0:
            raise ValueError("equity must be positive")
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM risk_state WHERE scope=?", (scope,)).fetchone():
                return  # never overwrite counters that already exist
            c.execute("INSERT INTO risk_state(scope,day,day_start_equity,peak_equity,current_equity,updated_at) VALUES (?,?,?,?,?,?)",
                      (scope, utcnow().date().isoformat(), equity, equity, equity, now_iso()))

    def _roll(self, c, scope: str, now: datetime) -> None:
        r = c.execute("SELECT * FROM risk_state WHERE scope=?", (scope,)).fetchone()
        if r is None:
            raise KeyError(f"risk scope {scope!r} was never initialised")
        today = now.astimezone(timezone.utc).date().isoformat()
        if r["day"] != today:  # a new UTC day: daily counters restart from today's opening equity
            c.execute("UPDATE risk_state SET day=?, day_start_equity=current_equity, daily_realized_pnl=0, updated_at=? WHERE scope=?",
                      (today, now_iso(), scope))

    def record_equity(self, scope: str, equity: float, now: Optional[datetime] = None) -> None:
        now = now or utcnow()
        with self.db.tx() as c:
            self._roll(c, scope, now)
            c.execute("UPDATE risk_state SET current_equity=?, peak_equity=CASE WHEN peak_equity > ? THEN peak_equity ELSE ? END, updated_at=? WHERE scope=?",
                      (equity, equity, equity, now_iso(), scope))

    def record_trade_result(self, scope: str, pnl: float, equity: float, now: Optional[datetime] = None, conn=None) -> None:
        now = now or utcnow()

        def run(c):
            self._roll(c, scope, now)
            if pnl < 0:
                c.execute("UPDATE risk_state SET consecutive_losses=consecutive_losses+1, last_loss_at=? WHERE scope=?", (now.isoformat(), scope))
            elif pnl > 0:
                c.execute("UPDATE risk_state SET consecutive_losses=0 WHERE scope=?", (scope,))
            c.execute("UPDATE risk_state SET daily_realized_pnl=daily_realized_pnl+?, current_equity=?, peak_equity=CASE WHEN peak_equity > ? THEN peak_equity ELSE ? END, updated_at=? WHERE scope=?",
                      (pnl, equity, equity, equity, now_iso(), scope))
        if conn is not None:
            run(conn)
        else:
            with self.db.tx() as c:
                run(c)

    def context(self, scope: str, bot_id: str, environment: str, now: Optional[datetime] = None) -> RiskContext:
        now = now or utcnow()
        with self.db.tx() as c:
            self._roll(c, scope, now)
            r = c.execute("SELECT * FROM risk_state WHERE scope=?", (scope,)).fetchone()
            pos = c.execute("SELECT COUNT(*) n, COALESCE(SUM(quantity*COALESCE(average_price,entry_price,0)),0) notional FROM positions "
                            "WHERE bot_id=? AND environment=? AND status<>'CLOSED'", (bot_id, environment)).fetchone()
        eq = r["current_equity"]
        daily = (eq - r["day_start_equity"]) / r["day_start_equity"] * 100
        dd = (r["peak_equity"] - eq) / r["peak_equity"] * 100 if r["peak_equity"] else 0.0
        return RiskContext(equity=eq, daily_pnl_pct=daily, drawdown_pct=max(dd, 0.0), open_positions=pos["n"],
                           exposure_pct=pos["notional"] / eq * 100 if eq else 100.0, consecutive_losses=r["consecutive_losses"],
                           last_loss_at=datetime.fromisoformat(r["last_loss_at"]) if r["last_loss_at"] else None, now=now)
