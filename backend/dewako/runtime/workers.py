"""Background workers with durable health, restart policy and graceful shutdown. No browser is involved.

Health per worker (table worker_health): status STARTING|HEALTHY|DEGRADED|FAILED|STOPPED, last_heartbeat, last_success,
last_error (exception class name only, never a message), restart_count, critical.

Policy
- an iteration that raises marks the worker DEGRADED; `fail_after` consecutive failures mark it FAILED (it keeps retrying);
- if the loop task itself dies it is restarted with a back-off up to `max_restarts`, then stays FAILED;
- a heartbeat older than 3 x interval + 5 s is STALE and counts as unhealthy;
- any unhealthy CRITICAL worker sets the system DEGRADED and makes the live gate refuse orders (`healthy()`);
- shutdown never cancels an iteration that is running (it waits, up to `grace` seconds, so an order state transition is not cut in half).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from dewako.persistence.db import Database, now_iso

log = logging.getLogger("dewako.workers")


@dataclass
class Worker:
    name: str
    fn: Callable[[], Awaitable[None]]
    interval: float
    critical: bool = True
    fail_after: int = 3
    max_restarts: int = 5


class WorkerSupervisor:
    def __init__(self, db: Database, safety, grace: float = 10.0, restart_backoff: float = 0.5):
        self.db, self.safety, self.grace, self.restart_backoff = db, safety, grace, restart_backoff
        self.workers: list[Worker] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop: Optional[asyncio.Event] = None
        self._consecutive: dict[str, int] = {}
        self._restarts: dict[str, int] = {}
        self._inflight: dict[str, bool] = {}

    # ---- registration / durable state
    def add(self, name: str, fn: Callable[[], Awaitable[None]], interval: float, critical: bool = True, **kw) -> None:
        self.workers.append(Worker(name, fn, interval, critical, **kw))
        self._write(name, "STARTING", critical=critical)

    def _write(self, name: str, status: str, *, critical: Optional[bool] = None, success: bool = False, error: Optional[str] = None,
               heartbeat: bool = False, restarts: Optional[int] = None) -> None:
        ts = now_iso()
        with self.db.tx() as c:
            row = c.execute("SELECT critical, restart_count FROM worker_health WHERE name=?", (name,)).fetchone()
            if row is None:
                c.execute("INSERT INTO worker_health(name,critical,status,last_heartbeat,last_success,last_error,restart_count,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                          "ON CONFLICT (name) DO NOTHING", (name, 1 if (critical is None or critical) else 0, status, ts if heartbeat else None,
                                                             ts if success else None, error, restarts or 0, ts))
                return
            sets, args = ["status=?", "updated_at=?"], [status, ts]
            if heartbeat:
                sets.append("last_heartbeat=?"); args.append(ts)
            if success:
                sets.append("last_success=?"); args.append(ts)
            if error is not None:
                sets.append("last_error=?"); args.append(error)
            if restarts is not None:
                sets.append("restart_count=?"); args.append(restarts)
            if critical is not None:
                sets.append("critical=?"); args.append(1 if critical else 0)
            c.execute(f"UPDATE worker_health SET {', '.join(sets)} WHERE name=?", (*args, name))

    # ---- one iteration (also used directly by tests)
    async def run_once(self, w: Worker) -> bool:
        self._inflight[w.name] = True
        try:
            self._write(w.name, "HEALTHY" if self._consecutive.get(w.name, 0) == 0 else "DEGRADED", heartbeat=True)
            await w.fn()
            self._consecutive[w.name] = 0
            self._write(w.name, "HEALTHY", success=True, heartbeat=True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a failing worker never takes the others down, and never looks healthy
            n = self._consecutive[w.name] = self._consecutive.get(w.name, 0) + 1
            status = "FAILED" if n >= w.fail_after else "DEGRADED"
            log.error("worker %s failed (%s), consecutive failures %d", w.name, type(e).__name__, n)
            self._write(w.name, status, error=type(e).__name__, heartbeat=True)
            if w.critical:
                self.safety.set_degraded(True, by=f"worker:{w.name}")
            return False
        finally:
            self._inflight[w.name] = False

    # ---- lifecycle
    async def _loop(self, w: Worker) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            await self.run_once(w)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=w.interval)
            except asyncio.TimeoutError:
                pass

    async def _guard(self, w: Worker) -> None:
        """Restart the loop if it dies unexpectedly (a bug in the loop itself), up to max_restarts."""
        while self._stop is not None and not self._stop.is_set():
            try:
                await self._loop(w)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                n = self._restarts[w.name] = self._restarts.get(w.name, 0) + 1
                if n > w.max_restarts:
                    self._write(w.name, "FAILED", error=f"loop died: {type(e).__name__}", restarts=n)
                    if w.critical:
                        self.safety.set_degraded(True, by=f"worker:{w.name}")
                    return
                self._write(w.name, "DEGRADED", error=f"loop restarted: {type(e).__name__}", restarts=n)
                await asyncio.sleep(self.restart_backoff * n)

    def start(self) -> None:
        self._stop = asyncio.Event()
        self._tasks = {w.name: asyncio.create_task(self._guard(w)) for w in self.workers}

    async def stop(self) -> dict[str, str]:
        """Graceful: ask loops to finish, wait for running iterations, only then cancel stragglers. Returns per-worker outcome."""
        out: dict[str, str] = {}
        if self._stop is None:
            return out
        self._stop.set()
        done, pending = await asyncio.wait(list(self._tasks.values()), timeout=self.grace) if self._tasks else (set(), set())
        for name, t in self._tasks.items():
            if t in pending:
                t.cancel()
                out[name] = "CANCELLED_AFTER_GRACE"
            else:
                out[name] = "STOPPED"
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for w in self.workers:
            self._write(w.name, "STOPPED")
        self._tasks = {}
        return out

    # ---- health
    def status(self) -> dict[str, dict]:
        now = datetime.now(timezone.utc)
        with self.db.read() as c:
            rows = {r["name"]: dict(r) for r in c.execute("SELECT * FROM worker_health")}
        out = {}
        for w in self.workers:
            r = rows.get(w.name)
            if r is None:
                out[w.name] = {"status": "STARTING", "critical": w.critical}
                continue
            st = r["status"]
            hb = r["last_heartbeat"]
            hb = datetime.fromisoformat(hb) if isinstance(hb, str) else hb
            if st in ("HEALTHY", "DEGRADED") and (hb is None or now - hb > timedelta(seconds=w.interval * 3 + 5)):
                st = "FAILED"   # heartbeat went silent: treat as dead even though nothing wrote FAILED
            out[w.name] = {"status": st, "critical": bool(r["critical"]), "last_heartbeat": str(r["last_heartbeat"]) if r["last_heartbeat"] else None,
                           "last_success": str(r["last_success"]) if r["last_success"] else None, "last_error": r["last_error"],
                           "restart_count": r["restart_count"]}
        return out

    def healthy(self) -> tuple[bool, str]:
        """(ok, reason) for the live gate: every critical worker must be HEALTHY. No workers registered means NOT healthy."""
        st = self.status()
        if not st:
            return False, "no workers are registered"
        bad = [f"{n}={v['status']}" for n, v in st.items() if v["critical"] and v["status"] != "HEALTHY"]
        return (not bad), ", ".join(bad)

    def recompute_degraded(self) -> None:
        """Clear the DEGRADED flag only when every critical worker is healthy again."""
        ok, _ = self.healthy()
        self.safety.set_degraded(not ok, by="supervisor")
