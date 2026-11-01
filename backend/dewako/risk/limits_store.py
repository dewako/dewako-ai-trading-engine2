"""Durable, versioned, audited RiskLimits.

Before Stage 1 the running loop rebuilt `RiskLimits()` (the defaults) on every iteration, so an operator could not change a limit and a limit
could not survive a restart. Now the current limits are the newest row of `risk_limits`; if no row exists the defaults apply and the source is
reported as DEFAULT. Changing a limit appends a new version (never edits history) and writes an audit record.

Two views of the limits:
- hard caps: exactly the stored limits. The Execution Engine's reservation backstop enforces these under the scope lock.
- sizing limits: the Risk Engine sizes positions slightly inside the hard caps (SIZING_HEADROOM) because the backstop prices an order at the
  executable ask, which is above the signal's reference price. Without headroom a position sized to the cap is always refused by the backstop.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from dewako.persistence.db import now_iso
from dewako.risk.engine import RiskLimits

SIZING_HEADROOM = 0.95


class RiskLimitsStore:
    def __init__(self, db, audit=None):
        self.db, self.audit = db, audit

    def current(self) -> tuple[RiskLimits, dict]:
        """Returns (limits, meta). meta: {"source": "STORED"|"DEFAULT", "version": int|None, "changed_by": ..., "created_at": ...}."""
        with self.db.read() as c:
            r = c.execute("SELECT * FROM risk_limits ORDER BY version DESC LIMIT 1").fetchone()
        if r is None:
            return RiskLimits(), {"source": "DEFAULT", "version": None, "changed_by": None, "created_at": None}
        return RiskLimits(**json.loads(r["limits"])), {"source": "STORED", "version": r["version"], "changed_by": r["changed_by"], "created_at": str(r["created_at"])}

    def hard_caps(self) -> RiskLimits:
        return self.current()[0]

    def sizing_limits(self) -> RiskLimits:
        lim = self.hard_caps()
        return lim.model_copy(update={"max_position_pct": lim.max_position_pct * SIZING_HEADROOM,
                                      "max_exposure_pct": lim.max_exposure_pct * SIZING_HEADROOM})

    def set(self, limits: dict | RiskLimits, *, actor: dict, reason: str = "") -> dict:
        """Validate and append a new version. Raises pydantic.ValidationError for an invalid value; nothing is written then.
        `require_stop_loss` can never be switched off."""
        new = limits if isinstance(limits, RiskLimits) else RiskLimits(**limits)
        if not new.require_stop_loss:
            raise ValueError("require_stop_loss cannot be disabled")
        with self.db.tx() as c:
            c.execute("INSERT INTO risk_limits(limits, changed_by, reason, created_at) VALUES (?,?,?,?)",
                      (json.dumps(new.model_dump()), actor.get("username", "system"), reason[:300], now_iso()))
            self.audit and self.audit.record("RISK_LIMITS_CHANGED", "OK", uuid.uuid4().hex, actor=actor, resource="risk_limits",
                                             metadata={"limits": new.model_dump(), "reason": reason[:300]}, conn=c)
        return self.current()[1]

    def history(self) -> list[dict]:
        with self.db.read() as c:
            return [{"version": r["version"], "changed_by": r["changed_by"], "reason": r["reason"], "created_at": str(r["created_at"]),
                     "limits": json.loads(r["limits"])} for r in c.execute("SELECT * FROM risk_limits ORDER BY version")]
