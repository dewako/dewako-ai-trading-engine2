"""Stage 2 WP6: audit-storage failures are never silent and never change what a trading operation does.

* an audit write that cannot be persisted is HELD with the original exception (class, message), logged at CRITICAL, and reported as DEGRADED;
* the held events are written later, in order, marked as late writes, exactly once, followed by a durable recovery marker;
* while degraded, new risk-increasing orders are blocked and exits are not;
* an audit failure AFTER the exchange has acknowledged an order does not stop the fill being applied, and one at an unknown order outcome does
  not stop the reconciliation flag; an audit failure BEFORE a stop-loss is sent does not stop the stop-loss.

The injected failure models an audit-store-only problem: writes that open their own transaction fail, writes inside the caller's transaction
(which commit or roll back with the change) are untouched.
Paper trading only. Nothing here contacts an exchange.
"""
import contextlib
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import tmp_db  # noqa: E402
from test_stage1_1_p1 import _enter, _started  # noqa: E402
from test_stage1_p0_application import LAST, run, setUpModule, tearDownModule, tick  # noqa: E402,F401
from test_stage1_p0_exits import buy, make, monitor, orders, position  # noqa: E402

from dewako.persistence.audit import AuditLog  # noqa: E402


@contextlib.contextmanager
def failing(*actions):
    """Audit writes that open their own transaction raise (all of them, or only these actions)."""
    original = AuditLog.record

    def record(self, action, *args, **kw):
        if (not actions or action in actions) and kw.get("conn") is None:
            raise RuntimeError("audit storage unavailable")
        return original(self, action, *args, **kw)
    AuditLog.record = record
    try:
        yield
    finally:
        AuditLog.record = original


def audit_rows(db, action=None):
    with db.read() as c:
        q, a = "SELECT * FROM audit_log", ()
        if action:
            q, a = q + " WHERE action=?", (action,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def meta(row):
    return json.loads(row["metadata"])


class HeldNotLost(unittest.TestCase):
    def test_a_write_that_succeeds_is_durable_and_nothing_is_held(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        self.assertTrue(al.record_safely("X_EVENT", "OK", "c1", metadata={"k": 1}))
        self.assertEqual(al.status()["persistence"], "HEALTHY")
        self.assertEqual(len(audit_rows(db, "X_EVENT")), 1)

    def test_a_failed_write_is_held_logged_critical_and_keeps_the_original_exception(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with failing("X_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL") as logs:
            self.assertFalse(al.record_safely("X_EVENT", "OK", "c1", resource="order:7", metadata={"k": 1}))
        self.assertIn("X_EVENT", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        st = al.status()
        self.assertEqual((st["persistence"], st["pending"], st["lost"], st["failed_total"]), ("DEGRADED", 1, 0, 1))
        self.assertEqual((st["last_error"]["error_class"], st["last_error"]["error_message"], st["last_error"]["action"]),
                         ("RuntimeError", "audit storage unavailable", "X_EVENT"))
        self.assertEqual(audit_rows(db, "X_EVENT"), [])                                  # it is NOT in the log, and is not claimed to be

    def test_held_events_are_written_in_order_exactly_once_as_late_writes_with_a_recovery_marker(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with failing(), self.assertLogs("dewako.audit", level="CRITICAL"):
            for n in (1, 2, 3):
                al.record_safely("X_EVENT", "OK", f"c{n}", metadata={"n": n})
        self.assertEqual(al.flush_pending(), 3)
        rows = audit_rows(db, "X_EVENT")
        self.assertEqual([meta(r)["n"] for r in rows], [1, 2, 3])                        # original order
        for r in rows:
            m = meta(r)
            self.assertTrue(m["late_write"])
            self.assertEqual(m["original_failure"]["error_class"], "RuntimeError")      # the original failure travels with the record
            self.assertIn("original_time", m)
        marker = audit_rows(db, "AUDIT_PERSISTENCE_RECOVERED")
        self.assertEqual(len(marker), 1)
        self.assertEqual((marker[0]["result"], meta(marker[0])["flushed"], meta(marker[0])["lost"]), ("OK", 3, 0))
        self.assertEqual(al.status()["persistence"], "HEALTHY")
        self.assertEqual(al.flush_pending(), 0)                                          # nothing left to write twice
        self.assertEqual(len(audit_rows(db, "X_EVENT")), 3)
        self.assertTrue(al.verify_chain()["ok"])

    def test_the_next_successful_safe_write_also_flushes_what_was_held(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with failing("X_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL"):
            al.record_safely("X_EVENT", "OK", "c1", metadata={"n": 1})
        self.assertTrue(al.record_safely("Y_EVENT", "OK", "c2"))
        self.assertEqual([len(audit_rows(db, "X_EVENT")), al.status()["persistence"]], [1, "HEALTHY"])

    def test_an_event_that_was_in_fact_written_is_not_written_twice(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with failing("X_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL"):
            al.record_safely("X_EVENT", "OK", "c1", metadata={"n": 1})
        event_id = al.failures.pending[0]["event_id"]
        al.record("X_EVENT", "OK", "c1", metadata={"n": 1, "audit_event_id": event_id})   # the write had committed; the caller only saw an error
        al.flush_pending()
        self.assertEqual(len(audit_rows(db, "X_EVENT")), 1)
        self.assertEqual(al.status()["persistence"], "HEALTHY")

    def test_a_flush_that_fails_again_keeps_the_rest_and_stays_degraded(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with failing(), self.assertLogs("dewako.audit", level="CRITICAL"):
            al.record_safely("A_EVENT", "OK", "c1")
            al.record_safely("B_EVENT", "OK", "c2")
        with failing("B_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL"):
            self.assertEqual(al.flush_pending(), 1)
        self.assertEqual((len(audit_rows(db, "A_EVENT")), audit_rows(db, "B_EVENT")), (1, []))
        self.assertEqual((al.status()["persistence"], al.status()["pending"]), ("DEGRADED", 1))
        self.assertEqual(audit_rows(db, "AUDIT_PERSISTENCE_RECOVERED"), [])
        al.flush_pending()
        self.assertEqual(al.status()["persistence"], "HEALTHY")

    def test_overflow_loses_the_oldest_says_so_and_stays_degraded_until_a_human_acknowledges(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        al.failures.max_pending = 3
        with failing(), self.assertLogs("dewako.audit", level="CRITICAL"):
            for n in range(5):
                al.record_safely("X_EVENT", "OK", f"c{n}", metadata={"n": n})
        self.assertEqual((al.status()["pending"], al.status()["lost"]), (3, 2))
        al.flush_pending()
        self.assertEqual([meta(r)["n"] for r in audit_rows(db, "X_EVENT")], [2, 3, 4])             # the two oldest are gone
        marker = audit_rows(db, "AUDIT_PERSISTENCE_RECOVERED")[0]
        self.assertEqual((marker["result"], meta(marker)["lost"]), ("DEGRADED", 2))                # the gap is on durable record
        self.assertEqual(al.status()["persistence"], "DEGRADED")                                   # events were lost: not "healthy"
        for bad in ({}, {"username": ""}):
            with self.assertRaises(ValueError):
                al.acknowledge_lost(bad, "ok")
        with self.assertRaises(ValueError):
            al.acknowledge_lost({"username": "root", "role": "ADMIN"}, "  ")
        self.assertEqual(al.acknowledge_lost({"user_id": 1, "username": "root", "role": "ADMIN"}, "reviewed"), 2)
        self.assertEqual(al.status()["persistence"], "HEALTHY")
        self.assertEqual(meta(audit_rows(db, "AUDIT_LOSS_ACKNOWLEDGED")[0])["lost"], 2)

    def test_every_audit_log_on_the_same_database_shares_one_failure_state(self):
        db, _ = tmp_db()
        a, b = AuditLog(db), AuditLog(db)
        with failing(), self.assertLogs("dewako.audit", level="CRITICAL"):
            a.record_safely("X_EVENT", "OK", "c1")
        self.assertTrue(b.degraded())

    def test_a_write_inside_the_callers_transaction_rolls_back_with_it(self):
        db, _ = tmp_db()
        al = AuditLog(db)
        with self.assertRaises(RuntimeError):
            with db.tx() as c:
                al.record("X_EVENT", "OK", "c1", conn=c)
                raise RuntimeError("the change failed")
        self.assertEqual(audit_rows(db, "X_EVENT"), [])


class TradingOperationsAreUnaffected(unittest.TestCase):
    def test_an_audit_failure_after_the_exchange_ack_does_not_stop_the_fill_being_applied(self):
        db, _, pa, eng = make()
        with failing("ORDER_SUBMITTED"), self.assertLogs("dewako.audit", level="CRITICAL"):
            self.assertEqual(buy(eng).outcome, "EXECUTED")
        self.assertEqual(position(db)["status"], "OPEN")                                  # the fill was applied
        self.assertEqual(len(orders(db, "BUY")), 1)
        self.assertEqual([e["action"] for e in eng.audit.failures.pending], ["ORDER_SUBMITTED"])
        eng.audit.flush_pending()
        self.assertEqual(len(audit_rows(db, "ORDER_SUBMITTED")), 1)

    def test_an_audit_failure_at_an_unknown_outcome_does_not_skip_the_reconciliation_flag(self):
        from dewako.safety.controller import ReconState
        db, _, pa, eng = make()

        async def timeout(req):
            raise TimeoutError("exchange timed out")
        pa.create_order = timeout
        with failing("ORDER_UNKNOWN"), self.assertLogs("dewako.audit", level="CRITICAL"):
            buy(eng)
        self.assertNotEqual(ReconState(db).current("orders"), "HEALTHY")                  # reconciliation was still required
        self.assertEqual([e["action"] for e in eng.audit.failures.pending], ["ORDER_UNKNOWN"])
        self.assertTrue(all(o["state"] != "SUBMITTING" for o in orders(db)))             # the order did not stay half-sent

    def test_an_audit_failure_before_a_wide_spread_stop_does_not_stop_the_stop_loss(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        pa.slippage = 0.04                                                                # 8 % spread: sent at once, with an emergency audit record
        pa.set_price("BTC/USDT", 94.0)
        with failing("EMERGENCY_EXIT_WIDE_SPREAD"), self.assertLogs("dewako.audit", level="CRITICAL"):
            rep = run(monitor(eng).run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual([e["action"] for e in eng.audit.failures.pending], ["EMERGENCY_EXIT_WIDE_SPREAD"])

    def test_an_audit_failure_while_a_take_profit_waits_is_not_an_unverified_position(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        pa.slippage = 0.01
        pa.set_price("BTC/USDT", 120.0)
        with failing("EXIT_WAITING_WIDE_SPREAD"), self.assertLogs("dewako.audit", level="CRITICAL"):
            rep = run(monitor(eng).run_once())
        self.assertEqual((len(rep.waiting), rep.unverified), (1, []))
        self.assertEqual(position(db)["status"], "OPEN")


class DegradedBehaviour(unittest.TestCase):
    def test_while_audit_is_degraded_entries_are_blocked_but_exits_are_not_and_recovery_reopens_entries(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng, sid="e1", bot="b1").outcome, "EXECUTED")
        with failing("X_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL"):
            eng.audit.record_safely("X_EVENT", "OK", "c1")                                # audit persistence is now degraded
        self.assertTrue(eng.audit.degraded())
        r = buy(eng, sid="e2", bot="b2")
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("audit persistence is degraded", r.detail)
        self.assertEqual(len(orders(db, "BUY")), 1)
        pa.set_price("BTC/USDT", 94.0)                                                    # the open position's stop is hit: exits are never blocked
        rep = run(monitor(eng).run_once())
        self.assertEqual([e[2] for e in rep.exits], ["EXECUTED"])
        self.assertEqual(position(db, "b1")["status"], "CLOSED")
        eng.audit.flush_pending()
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e3", bot="b3").outcome, "EXECUTED")


class ThroughTheRunningApplication(unittest.TestCase):
    def test_readiness_health_and_the_health_worker_follow_audit_persistence(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            try:
                for w in app.supervisor.workers:
                    if w.critical:
                        await tick(app, w.name)
                self.assertEqual(app.readiness(), (True, "READY"))
                self.assertEqual(app.health()["audit_persistence"]["persistence"], "HEALTHY")
                with failing("X_EVENT"), self.assertLogs("dewako.audit", level="CRITICAL"):
                    app.rt.audit.record_safely("X_EVENT", "OK", "c1")
                self.assertEqual(app.readiness(), (False, "SYSTEM_DEGRADED"))             # events that are not persisted are not "audited"
                self.assertEqual(app.health()["audit_persistence"]["pending"], 1)
                await tick(app, "health_monitor")                                          # storage is back: the health worker writes them
                self.assertEqual(app.readiness(), (True, "READY"))
                self.assertEqual(len(audit_rows(db, "X_EVENT")), 1)
            finally:
                await app.shutdown()
        run(go())

    def test_a_failing_audit_store_at_startup_is_held_and_reported_not_swallowed(self):
        db, target = tmp_db()

        async def go():
            with failing("ENGINE_STARTUP"), self.assertLogs("dewako.audit", level="CRITICAL"):
                app, status = await _started(target)
            try:
                self.assertEqual(status, "READY")                                         # startup itself is unaffected
                self.assertIn("ENGINE_STARTUP", [e["action"] for e in app.rt.audit.failures.pending])
                self.assertEqual(app.health()["audit_persistence"]["persistence"], "DEGRADED")
            finally:
                await app.shutdown()
        run(go())

    def test_an_ai_request_audit_failure_is_held_not_swallowed(self):
        from dewako.ai.manager import AIProviderManager as AIManager
        db, _ = tmp_db()
        al = AuditLog(db)
        fake = types.SimpleNamespace(audit=al, config=types.SimpleNamespace(mode="test"))
        res = types.SimpleNamespace(status="OK", provider="p", used_fallback=False, attempts=1, suspicious_input=False)
        with failing("AI_REQUEST"), self.assertLogs("dewako.audit", level="CRITICAL"):
            AIManager._audit(fake, res, "schema", "cid")
        self.assertEqual([e["action"] for e in al.failures.pending], ["AI_REQUEST"])


if __name__ == "__main__":
    unittest.main()
