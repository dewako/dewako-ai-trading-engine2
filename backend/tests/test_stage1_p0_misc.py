"""Stage 1 / P0-5: (a) the per-order live gate must not fail open when no worker supervisor is registered; (b) the first-user bootstrap must
be atomic under concurrency (two simultaneous bootstrap requests must never create two ADMINs).
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import make_live_ready, tmp_db  # noqa: E402

from dewako.auth.service import AuthError, AuthService  # noqa: E402
from dewako.persistence.db import Database  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402


class WorkerHealthIsNotOptionalInTheLiveGate(unittest.TestCase):
    def test_unset_workers_healthy_blocks_the_gate(self):
        db, _ = tmp_db()
        safety = SafetyController(db)
        eid = make_live_ready(db, safety)
        safety.workers_healthy = lambda: (True, "")
        self.assertTrue(safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2", symbol="BTC/USDT").allowed)
        safety.workers_healthy = None                      # no supervisor registered (workers disabled / not started)
        g = safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2", symbol="BTC/USDT")
        self.assertFalse(g.allowed)
        self.assertTrue(any("worker" in r for r in g.reasons), g.reasons)


class BootstrapIsAtomic(unittest.TestCase):
    def test_concurrent_first_user_requests_create_exactly_one_admin(self):
        db, target = tmp_db()
        n = 8
        barrier = threading.Barrier(n)
        results, lock = [], threading.Lock()

        def attempt(i):
            d = Database(target) if not db.is_pg else db
            svc = AuthService(d)
            barrier.wait()
            try:
                uid = svc.create_user(None, f"admin{i}", "correct horse battery staple", "ADMIN")
                out = ("ok", uid)
            except Exception as e:
                out = ("err", type(e).__name__)
            with lock:
                results.append(out)

        ts = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        with db.read() as c:
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(users, 1, results)
        self.assertEqual(sum(1 for r in results if r[0] == "ok"), 1, results)

    def test_second_bootstrap_after_the_first_is_refused(self):
        db, _ = tmp_db()
        svc = AuthService(db)
        svc.create_user(None, "root", "correct horse battery staple", "ADMIN")
        with self.assertRaises((AuthError, Exception)):
            svc.create_user(None, "second", "correct horse battery staple", "ADMIN")


if __name__ == "__main__":
    unittest.main()
