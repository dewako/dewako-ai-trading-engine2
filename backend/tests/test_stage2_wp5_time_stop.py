"""Stage 2 WP5: the time stop is explicit, durable and never changes silently across a restart.

* disabled by default and reported NOT_ENFORCED with a reason;
* the effective setting is stored in the database together with its audit record;
* a restart with a different configuration does NOT change the time stop while any position is open (the stored value stays, the
  refusal is audited and reported); with nothing open the change is adopted and audited.

Paper trading only. Nothing here contacts an exchange.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import tmp_db  # noqa: E402
from test_stage1_1_p1 import _enter, _started  # noqa: E402
from test_stage1_p0_application import LAST, rows, run, setUpModule, tearDownModule, tick  # noqa: E402,F401

from dewako.risk.controls import control_status  # noqa: E402

KEY = "time_stop_config"


def stored(app):
    r = rows(app, f"SELECT value FROM safety_state WHERE key='{KEY}'")
    return json.loads(r[0]["value"]) if r else None


def actions(app, name):
    return rows(app, f"SELECT * FROM audit_log WHERE action='{name}' ORDER BY id")


class DisabledByDefault(unittest.TestCase):
    def test_no_configuration_means_disabled_stored_audited_and_reported_not_enforced(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            try:
                self.assertIsNone(app.exit_monitor.max_hold_seconds)
                self.assertEqual(stored(app), {"max_hold_seconds": None})
                self.assertEqual(len(actions(app, "TIME_STOP_CONFIG_STORED")), 1)
                ctl = control_status(equity_feed_ok=True, time_stop=app.time_stop)["time_stop"]
                self.assertEqual(ctl["status"], "NOT_ENFORCED")
                self.assertIn("disabled", ctl["reason"])
            finally:
                await app.shutdown()
        run(go())

    def test_an_enabled_time_stop_is_reported_enforced_with_its_limit(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target, max_hold_seconds=900)
            try:
                self.assertEqual(stored(app), {"max_hold_seconds": 900.0})
                ctl = control_status(equity_feed_ok=True, time_stop=app.time_stop)["time_stop"]
                self.assertEqual((ctl["status"], ctl["max_hold_seconds"]), ("ENFORCED", 900))
            finally:
                await app.shutdown()
        run(go())

    def test_without_the_argument_the_report_says_not_enforced(self):
        self.assertEqual(control_status(equity_feed_ok=True)["time_stop"]["status"], "NOT_ENFORCED")


class RestartDoesNotChangeOpenPositionBehaviour(unittest.TestCase):
    def test_enabling_the_time_stop_on_restart_with_an_open_position_is_refused(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)                                  # first run: disabled, one open position
            await _enter(app)
            self.assertEqual(len(rows(app, "SELECT * FROM positions WHERE status='OPEN'")), 1)
            await app.shutdown()
            app2, _ = await _started(target, max_hold_seconds=60)            # restart with the time stop switched on
            try:
                self.assertIsNone(app2.exit_monitor.max_hold_seconds)       # the stored (disabled) value stays in force
                self.assertTrue(app2.time_stop["refused"])
                self.assertEqual(len(actions(app2, "TIME_STOP_CONFIG_REFUSED")), 1)
                ctl = control_status(equity_feed_ok=True, time_stop=app2.time_stop)["time_stop"]
                self.assertEqual(ctl["pending_change_refused"]["configured"], 60)
                app2._clock_box["now"] = datetime.now(timezone.utc) + timedelta(hours=5)
                app2.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app2.clock())
                await tick(app2, "exit_monitor")
                self.assertEqual(rows(app2, "SELECT status FROM positions")[0]["status"], "OPEN")    # not closed by an unannounced time stop
            finally:
                await app2.shutdown()
        run(go())

    def test_disabling_the_time_stop_on_restart_with_an_open_position_is_refused_too(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target, max_hold_seconds=3600)
            await _enter(app)
            await app.shutdown()
            app2, _ = await _started(target)                                  # env no longer sets it
            try:
                self.assertEqual(app2.exit_monitor.max_hold_seconds, 3600)
                self.assertTrue(app2.time_stop["refused"])
                app2._clock_box["now"] = datetime.now(timezone.utc) + timedelta(hours=2)
                app2.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app2.clock())
                await tick(app2, "exit_monitor")
                self.assertEqual(rows(app2, "SELECT status FROM positions")[0]["status"], "CLOSED")  # the protection the position was opened under
            finally:
                await app2.shutdown()
        run(go())

    def test_an_unchanged_configuration_restarts_cleanly_with_no_extra_audit_record(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target, max_hold_seconds=3600)
            await app.shutdown()
            app2, _ = await _started(target, max_hold_seconds=3600)
            try:
                self.assertFalse(app2.time_stop["refused"])
                self.assertEqual(app2.exit_monitor.max_hold_seconds, 3600)
                self.assertEqual(len(actions(app2, "TIME_STOP_CONFIG_STORED")), 1)
                self.assertEqual(actions(app2, "TIME_STOP_CONFIG_CHANGED") + actions(app2, "TIME_STOP_CONFIG_REFUSED"), [])
            finally:
                await app2.shutdown()
        run(go())

    def test_with_nothing_open_a_changed_configuration_is_adopted_and_audited(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            await app.shutdown()
            app2, _ = await _started(target, max_hold_seconds=120)
            try:
                self.assertEqual(app2.exit_monitor.max_hold_seconds, 120)
                self.assertFalse(app2.time_stop["refused"])
                self.assertEqual(stored(app2), {"max_hold_seconds": 120.0})
                ch = actions(app2, "TIME_STOP_CONFIG_CHANGED")
                self.assertEqual(len(ch), 1)
                meta = json.loads(ch[0]["metadata"])
                self.assertEqual((meta["previous"], meta["new"], meta["open_positions"]), (None, 120, 0))
            finally:
                await app2.shutdown()
        run(go())

    def test_a_closed_position_does_not_block_a_change(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target, max_hold_seconds=3600)
            await _enter(app)
            app._clock_box["now"] = datetime.now(timezone.utc) + timedelta(hours=2)
            app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
            await tick(app, "exit_monitor")
            self.assertEqual(rows(app, "SELECT status FROM positions")[0]["status"], "CLOSED")
            await app.shutdown()
            app2, _ = await _started(target)
            try:
                self.assertIsNone(app2.exit_monitor.max_hold_seconds)
                self.assertFalse(app2.time_stop["refused"])
            finally:
                await app2.shutdown()
        run(go())


if __name__ == "__main__":
    unittest.main()
