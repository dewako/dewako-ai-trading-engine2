"""Durable worker lease with a fencing epoch.

Only the process holding the 'execution' lease may submit orders. The lease row lives in the database, so every process sees the same
truth. A process that stops renewing (crash, hang, partition) loses the lease after `ttl`; another process may then take it and the
epoch increases. The old process is checked against the database immediately before every send (`check()`), so a paused-then-resumed
worker cannot send under a stale lease. Clock assumption: the processes share a reasonably synchronised clock (expiry compares
timestamps written by different processes); keep NTP running and `ttl` well above the expected skew.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from dewako.persistence.db import Database


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class WorkerLease:
    def __init__(self, db: Database, name: str = "execution", owner: Optional[str] = None, ttl: float = 30.0):
        self.db, self.name, self.ttl = db, name, ttl
        self.owner = owner or uuid.uuid4().hex
        self.epoch: Optional[int] = None
        self._deadline = 0.0            # monotonic local deadline: a worker that cannot reach the database stops by itself

    def acquire(self) -> bool:
        """Take the lease if it is free, expired, or already ours. Atomic in the database."""
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self.ttl)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO worker_leases(name,owner,epoch,expires_at,acquired_at,renewed_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (name) DO UPDATE SET owner=excluded.owner, "
                "epoch=CASE WHEN worker_leases.owner=excluded.owner THEN worker_leases.epoch ELSE worker_leases.epoch+1 END, "
                "expires_at=excluded.expires_at, "
                "acquired_at=CASE WHEN worker_leases.owner=excluded.owner THEN worker_leases.acquired_at ELSE excluded.acquired_at END, "
                "renewed_at=excluded.renewed_at "
                "WHERE worker_leases.owner=excluded.owner OR worker_leases.expires_at < ?",
                (self.name, self.owner, 1, _iso(exp), _iso(now), _iso(now), _iso(now)))
            won = cur.rowcount == 1
            row = c.execute("SELECT owner, epoch FROM worker_leases WHERE name=?", (self.name,)).fetchone() if won else None
        if won and row and row["owner"] == self.owner:
            self.epoch = int(row["epoch"])
            self._deadline = time.monotonic() + self.ttl
            return True
        return False

    renew = acquire        # renewing is acquiring again while still the owner

    def check(self) -> tuple[bool, str]:
        """Called before every send. Verifies against the DATABASE, not against local belief."""
        if self.epoch is None:
            return False, "execution lease was never acquired"
        if time.monotonic() > self._deadline:
            return False, "execution lease expired locally (not renewed in time)"
        with self.db.read() as c:
            r = c.execute("SELECT owner, epoch, expires_at FROM worker_leases WHERE name=?", (self.name,)).fetchone()
        if r is None or r["owner"] != self.owner or int(r["epoch"]) != self.epoch:
            return False, "execution lease is held by another worker"
        exp = r["expires_at"]
        exp = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
        if exp < datetime.now(timezone.utc):
            return False, "execution lease expired"
        return True, ""

    def release(self) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE worker_leases SET expires_at=? WHERE name=? AND owner=?",
                      (_iso(datetime.now(timezone.utc) - timedelta(seconds=1)), self.name, self.owner))
        self.epoch = None
