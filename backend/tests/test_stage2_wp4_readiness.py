"""Stage 2 WP4: readiness is earned, not assumed.

* a worker is STARTING (not healthy) until an iteration has actually COMPLETED in this process, even though it has already written a
  heartbeat; a healthy-looking row left by a previous process never counts;
* after failures a worker stays DEGRADED until an iteration succeeds (recovery needs a real success);
* a silent heartbeat (older than 3 x interval + 5 s) makes a critical worker FAILED and the system unready, and the next success recovers it;
* a failing NON-critical worker does not make the system unready; a failing critical one does.

Paper trading only. Nothing here contacts an exchange.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import tmp_db  # noqa: E402

from dewako.runtime.workers import WorkerSupervisor  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402


def make(db):
    return WorkerSupervisor(db, SafetyController(db))


async def ok():
    return None


async def bad():
    raise RuntimeError("boom")


def age_heartbeat(db, name, seconds):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with db.tx() as c:
        c.execute("UPDATE worker_health SET last_heartbeat=? WHERE name=?", (ts, name))


class StartingUntilFirstSuccess(unittest.TestCase):
    def test_a_worker_inside_its_first_iteration_is_starting_not_healthy(self):
        db, _ = tmp_db()
        sup = make(db)
        seen = {}

        async def go():
            gate = asyncio.Event()

            async def slow():
                seen["mid"] = sup.status()["w"]["status"]
                seen["healthy_mid"] = sup.healthy()[0]
                await gate.wait()
            sup.add("w", slow, 60)
            t = asyncio.create_task(sup.run_once(sup.workers[0]))
            await asyncio.sleep(0.05)
            gate.set()
            await t
        asyncio.run(go())
        self.assertEqual(seen["mid"], "STARTING")                 # heartbeat was written, but nothing has completed yet
        self.assertFalse(seen["healthy_mid"])
        self.assertEqual(sup.status()["w"]["status"], "HEALTHY")   # the completed iteration is what earns HEALTHY
        self.assertTrue(sup.healthy()[0])

    def test_a_healthy_row_from_a_previous_process_does_not_count_after_a_restart(self):
        db, _ = tmp_db()
        first = make(db)
        first.add("w", ok, 60)
        asyncio.run(first.run_once(first.workers[0]))
        self.assertTrue(first.healthy()[0])
        second = make(db)                                          # the process restarted on the same database
        second.add("w", ok, 60)
        self.assertEqual(second.status()["w"]["status"], "STARTING")
        self.assertFalse(second.healthy()[0])
        asyncio.run(second.run_once(second.workers[0]))
        self.assertTrue(second.healthy()[0])


class CrashAndRecovery(unittest.TestCase):
    def test_a_critical_failure_is_unhealthy_and_only_a_successful_iteration_recovers_it(self):
        db, _ = tmp_db()
        sup = make(db)
        mode = {"fail": True}

        async def flaky():
            if mode["fail"]:
                raise RuntimeError("boom")
        sup.add("w", flaky, 60, critical=True)
        w = sup.workers[0]
        self.assertFalse(asyncio.run(sup.run_once(w)))
        self.assertFalse(sup.healthy()[0])
        self.assertFalse(asyncio.run(sup.run_once(w)))              # still failing: still not healthy, and the next attempt did not heal it
        self.assertFalse(sup.healthy()[0])
        self.assertIn(sup.status()["w"]["status"], ("DEGRADED", "FAILED"))
        mode["fail"] = False
        self.assertTrue(asyncio.run(sup.run_once(w)))
        self.assertTrue(sup.healthy()[0])

    def test_a_failing_non_critical_worker_does_not_make_the_system_unready(self):
        db, _ = tmp_db()
        sup = make(db)
        sup.add("core", ok, 60, critical=True)
        sup.add("extra", bad, 60, critical=False)
        for w in sup.workers:
            asyncio.run(sup.run_once(w))
        self.assertEqual(sup.status()["extra"]["status"], "DEGRADED")
        self.assertTrue(sup.healthy()[0])

    def test_no_registered_workers_is_never_healthy(self):
        db, _ = tmp_db()
        self.assertFalse(make(db).healthy()[0])


class HeartbeatExpiry(unittest.TestCase):
    def test_a_silent_critical_worker_becomes_failed_and_recovers_on_its_next_success(self):
        db, _ = tmp_db()
        sup = make(db)
        sup.add("w", ok, 10, critical=True)
        w = sup.workers[0]
        asyncio.run(sup.run_once(w))
        self.assertTrue(sup.healthy()[0])
        age_heartbeat(db, "w", 10 * 3 + 5 + 30)                      # silent for longer than 3 x interval + 5 s
        self.assertEqual(sup.status()["w"]["status"], "FAILED")
        self.assertFalse(sup.healthy()[0])
        asyncio.run(sup.run_once(w))
        self.assertEqual(sup.status()["w"]["status"], "HEALTHY")
        self.assertTrue(sup.healthy()[0])

    def test_a_heartbeat_inside_the_window_is_still_healthy(self):
        db, _ = tmp_db()
        sup = make(db)
        sup.add("w", ok, 10, critical=True)
        asyncio.run(sup.run_once(sup.workers[0]))
        age_heartbeat(db, "w", 10 * 3 - 5)
        self.assertTrue(sup.healthy()[0])

    def test_a_non_critical_silent_worker_does_not_make_the_system_unready(self):
        db, _ = tmp_db()
        sup = make(db)
        sup.add("core", ok, 10, critical=True)
        sup.add("extra", ok, 10, critical=False)
        for w in sup.workers:
            asyncio.run(sup.run_once(w))
        age_heartbeat(db, "extra", 1000)
        self.assertEqual(sup.status()["extra"]["status"], "FAILED")
        self.assertTrue(sup.healthy()[0])


if __name__ == "__main__":
    unittest.main()
