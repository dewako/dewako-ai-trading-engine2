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
    def __init__(self, db: Database, correlation_groups: Optional[dict] = None):
        self.db = db
        self.correlation_groups = correlation_groups or {}       # symbol -> group name; empty means one group for everything

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

    def context(self, scope: str, bot_id: str, environment: str, now: Optional[datetime] = None, symbol: Optional[str] = None) -> RiskContext:
        """Open-position figures cover the whole environment (every bot), because the limits are account-level.
        `correlated_risk_pct` is the capital currently at risk (entry to stop) across open positions in the same correlation group as `symbol`;
        every symbol is one group unless `correlation_groups` says otherwise (conservative: assume everything moves together)."""
        now = now or utcnow()
        with self.db.tx() as c:
            self._roll(c, scope, now)
            r = c.execute("SELECT * FROM risk_state WHERE scope=?", (scope,)).fetchone()
            pos = [dict(p) for p in c.execute("SELECT symbol, quantity, average_price, entry_price, stop_loss FROM positions "
                                              "WHERE environment=? AND status<>'CLOSED'", (environment,))]
        eq = r["current_equity"]
        daily = (eq - r["day_start_equity"]) / r["day_start_equity"] * 100
        dd = (r["peak_equity"] - eq) / r["peak_equity"] * 100 if r["peak_equity"] else 0.0
        notional = sum(p["quantity"] * (p["average_price"] or p["entry_price"] or 0.0) for p in pos)
        group = self.correlation_groups.get(symbol) if symbol else None
        at_risk = 0.0
        for p in pos:
            if group is not None and self.correlation_groups.get(p["symbol"]) != group:
                continue
            px = p["average_price"] or p["entry_price"] or 0.0
            risk_per_unit = (px - p["stop_loss"]) if p["stop_loss"] is not None else px      # no stop recorded: count the whole notional
            at_risk += p["quantity"] * max(risk_per_unit, 0.0)
        return RiskContext(equity=eq, daily_pnl_pct=daily, drawdown_pct=max(dd, 0.0), open_positions=len(pos),
                           exposure_pct=notional / eq * 100 if eq else 100.0, correlated_risk_pct=at_risk / eq * 100 if eq else 100.0,
                           consecutive_losses=r["consecutive_losses"],
                           last_loss_at=datetime.fromisoformat(r["last_loss_at"]) if r["last_loss_at"] else None, now=now)
