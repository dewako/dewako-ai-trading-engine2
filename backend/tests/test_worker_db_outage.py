"""Regression: a short database outage must not kill workers for good (paper run, 27 Nov 2026 21:50).

What happened: every worker raised sqlite3.OperationalError once; each error handler then wrote its health row to the SAME database, that
write raised too, the exception escaped the iteration guard and the loop guard, and every worker task ended silently. The web server kept
answering /health/live for 18 hours. These tests reproduce that and pin the fix: a failed health write is logged, never fatal; the worker
keeps retrying and recovers when the database is back; the log names the database error; a task that still dies is logged, not silent."""
import asyncio
import logging
import sqlite3

from dewako.runtime.workers import Worker, WorkerSupervisor
from dewako.safety.controller import SafetyController
from hardening_helpers import tmp_db


class Outage:
    """Makes the supervisor's own health writes fail while `on` is True (the database is 'locked')."""
    def __init__(self, sup):
        self.on = False
        self.real = sup._write

        def write(*a, **kw):
            if self.on:
                raise sqlite3.OperationalError("database is locked")
            return self.real(*a, **kw)
        sup._write = write


def _status(db, name):
    with db.read() as c:
        return dict(c.execute("SELECT * FROM worker_health WHERE name=?", (name,)).fetchone())


def test_worker_survives_a_database_outage_that_also_breaks_its_health_writes(caplog):
    db, _ = tmp_db()
    sup = WorkerSupervisor(db, SafetyController(db), grace=1.0, restart_backoff=0.01)
    calls = {"n": 0}
    outage = Outage(sup)

    async def job():
        calls["n"] += 1
        if outage.on:
            raise sqlite3.OperationalError("database is locked")

    sup.add("bot_runner", job, 0.01, critical=True)

    async def go():
        sup.start()
        outage.on = True
        await asyncio.sleep(0.2)            # the outage: the job AND every health write fail
        during = calls["n"]
        outage.on = False
        await asyncio.sleep(0.2)            # the database is back
        after = calls["n"]
        alive = not sup._tasks["bot_runner"].done()
        await sup.stop()
        return during, after, alive

    with caplog.at_level(logging.ERROR, logger="dewako.workers"):
        during, after, alive = asyncio.run(go())
    assert alive, "the worker task must still be running after the outage"
    assert during >= 3, "the worker must keep retrying during the outage, not stop after the first failure"
    assert after > during, "the worker must run again once the database is back"
    assert _status(db, "bot_runner")["status"] == "STOPPED"       # stop() reached it: it was alive to the end
    assert "database is locked" in caplog.text, "the log must name the database error, not only the class"


def test_worker_recovers_to_healthy_after_the_outage():
    db, _ = tmp_db()
    sup = WorkerSupervisor(db, SafetyController(db), grace=1.0, restart_backoff=0.01)
    outage = Outage(sup)

    async def job():
        if outage.on:
            raise sqlite3.OperationalError("disk I/O error")

    sup.add("market_data", job, 0.01, critical=True)

    async def go():
        sup.start()
        outage.on = True
        await asyncio.sleep(0.1)
        outage.on = False
        await asyncio.sleep(0.2)
        st = sup.status()["market_data"]["status"]
        await sup.stop()
        return st
    assert asyncio.run(go()) == "HEALTHY"


def test_loop_guard_survives_failing_health_writes():
    """The loop itself dies (a bug) while the database is down: the guard's own DEGRADED write must not end the task."""
    db, _ = tmp_db()
    sup = WorkerSupervisor(db, SafetyController(db), grace=1.0, restart_backoff=0.01)
    outage = Outage(sup)
    sup.add("w", lambda: asyncio.sleep(0), 0.01, critical=True, max_restarts=1000)
    loops = {"n": 0}

    async def dying(_w):
        loops["n"] += 1
        if outage.on:
            raise RuntimeError("loop bug")

    sup.run_once = dying

    async def go():
        sup.start()
        outage.on = True
        await asyncio.sleep(0.2)
        n = loops["n"]
        outage.on = False
        await asyncio.sleep(0.1)
        alive = not sup._tasks["w"].done()
        await sup.stop()
        return n, alive
    n, alive = asyncio.run(go())
    assert n >= 2 and alive


def test_non_database_errors_still_log_the_class_name_only(caplog):
    """Messages of other exceptions may carry secrets (URLs with keys, payloads): they stay out of the log, as before."""
    db, _ = tmp_db()
    sup = WorkerSupervisor(db, SafetyController(db))

    async def job():
        raise RuntimeError("https://api.example/?apiKey=SECRET123")

    sup.add("x", job, 1)
    with caplog.at_level(logging.ERROR, logger="dewako.workers"):
        asyncio.run(sup.run_once(sup.workers[0]))
    assert "RuntimeError" in caplog.text and "SECRET123" not in caplog.text
    assert _status(db, "x")["last_error"] == "RuntimeError"


def test_a_worker_task_that_still_dies_is_logged_not_silent(caplog):
    db, _ = tmp_db()
    sup = WorkerSupervisor(db, SafetyController(db), grace=1.0)
    sup.add("w", lambda: asyncio.sleep(0), 0.01)

    async def guard_bug(_w):
        raise ValueError("bug outside every handler")

    sup._guard = guard_bug

    async def go():
        sup.start()
        await asyncio.sleep(0.05)
        await sup.stop()
    with caplog.at_level(logging.CRITICAL, logger="dewako.workers"):
        asyncio.run(go())
    assert "worker w task ended unexpectedly" in caplog.text
