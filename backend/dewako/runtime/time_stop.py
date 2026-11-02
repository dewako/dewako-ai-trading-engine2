"""Durable time-stop configuration (Stage 2 WP5).

The time stop is DISABLED by default and is never activated implicitly. The setting the exit monitor runs with is stored in the database
(`safety_state`, key `time_stop_config`), so a restart does not silently change how already-open positions are handled:

* first start: the configured value (env, or none = disabled) is stored and used;
* same value on restart: unchanged;
* a different value while any position is not CLOSED: the STORED value stays in force, the change is refused, and the refusal is audited
  (TIME_STOP_CONFIG_REFUSED) and reported by /risk/limits;
* a different value while nothing is open: the new value is adopted, stored and audited (TIME_STOP_CONFIG_CHANGED).

The stored row and its audit record are written in one transaction: neither exists without the other.
"""
from __future__ import annotations

import json
from typing import Any, Optional

KEY = "time_stop_config"


def _parse(raw: str) -> Optional[float]:
    v = json.loads(raw).get("max_hold_seconds")
    return None if v is None else float(v)


def resolve_time_stop(db: Any, audit: Any, configured: Optional[float], *, correlation_id: str = "startup:time_stop") -> dict:
    """Return {"max_hold_seconds": effective or None, "source", "configured", "refused"} and persist/audit as described above."""
    from dewako.persistence.db import now_iso
    with db.tx() as c:
        row = c.execute("SELECT value FROM safety_state WHERE key=?", (KEY,)).fetchone()
        stored = _parse(row["value"]) if row is not None else None
        open_n = c.execute("SELECT COUNT(*) AS n FROM positions WHERE status<>'CLOSED'").fetchone()["n"]
        value = json.dumps({"max_hold_seconds": configured})

        def store(action: str, result: str, why: str) -> None:
            if row is None:
                c.execute("INSERT INTO safety_state(key,value,updated_at,updated_by) VALUES (?,?,?,?)", (KEY, value, now_iso(), "startup"))
            else:
                c.execute("UPDATE safety_state SET value=?, updated_at=?, updated_by=? WHERE key=?", (value, now_iso(), "startup", KEY))
            audit.record(action, result, correlation_id, resource=KEY, conn=c,
                         metadata={"previous": stored if row is not None else "unset", "new": configured, "open_positions": open_n, "why": why})

        if row is None:
            store("TIME_STOP_CONFIG_STORED", "OK", "first start: configuration stored")
            return {"max_hold_seconds": configured, "source": "configured", "configured": configured, "refused": False}
        if stored == configured:
            return {"max_hold_seconds": stored, "source": "stored", "configured": configured, "refused": False}
        if open_n > 0:
            audit.record("TIME_STOP_CONFIG_REFUSED", "BLOCKED", correlation_id, resource=KEY, conn=c,
                         metadata={"stored": stored, "configured": configured, "open_positions": open_n,
                                   "why": "the time-stop setting does not change while positions are open; the stored value stays in force"})
            return {"max_hold_seconds": stored, "source": "stored", "configured": configured, "refused": True}
        store("TIME_STOP_CONFIG_CHANGED", "OK", "no open positions: the new configuration was adopted")
        return {"max_hold_seconds": configured, "source": "configured", "configured": configured, "refused": False}
