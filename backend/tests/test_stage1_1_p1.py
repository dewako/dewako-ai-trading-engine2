"""Stage 1.1: the four P1 defects found by the independent Stage 1 verification.

P1-1  runtime time stop is wired from configuration (disabled by default, invalid values refuse to start)
P1-2  entry size is bounded by the loss at the stop measured from the EXECUTABLE entry price, not only the reference price
P1-3  a triggered stop/target that meets a wide spread follows an explicit, bounded policy and is never silently dropped or falsely closed
P1-4  readiness is computed from current worker/protection state, not from the startup verdict

Paper trading only. Nothing here contacts an exchange.
"""
import asyncio
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import scenarios as S  # noqa: E402
from hardening_helpers import tmp_db  # noqa: E402
from test_stage1_p0_application import (DATA, LAST, NOW, rows, run, setUpModule, started, tearDownModule, tick)  # noqa: E402,F401
from hardening_helpers import PAPER_ASK  # noqa: E402
from test_stage1_p0_exits import buy, make, monitor, orders, position  # noqa: E402

from dewako.core.types import MarketState  # noqa: E402
from dewako.execution.exit_monitor import INCIDENT_KIND, WIDE_SPREAD_KIND, ExitMonitor  # noqa: E402
from dewako.execution.validation import QuoteGuardError, executable_risk_quantity  # noqa: E402
from dewako.risk.engine import RiskEngine  # noqa: E402
from dewako.runtime.application import Application, Config  # noqa: E402


def incidents(db, kind=None):
    with db.read() as c:
        q = "SELECT * FROM incidents" + (" WHERE kind=?" if kind else "")
        return [dict(r) for r in c.execute(q, (kind,) if kind else ())]


# ------------------------------------------------------------------ P1-2
class ExecutableRiskQuantity(unittest.TestCase):
    def test_a_higher_executable_price_scales_the_quantity_down_in_proportion(self):
        q = executable_risk_quantity(10.0, reference_price=100.0, risk_price=101.0, stop_price=95.0)
        self.assertAlmostEqual(q, 10.0 * 5 / 6)
        self.assertLessEqual(q * (101.0 - 95.0), 10.0 * (100.0 - 95.0) + 1e-9)

    def test_an_executable_price_at_or_below_the_reference_never_increases_the_quantity(self):
        self.assertEqual(executable_risk_quantity(10.0, reference_price=100.0, risk_price=100.0, stop_price=95.0), 10.0)
        self.assertEqual(executable_risk_quantity(10.0, reference_price=100.0, risk_price=99.0, stop_price=95.0), 10.0)

    def test_a_stop_at_or_above_the_executable_price_is_refused(self):
        for risk_price in (95.0, 94.0):
            with self.assertRaises(QuoteGuardError):
                executable_risk_quantity(10.0, reference_price=100.0, risk_price=risk_price, stop_price=95.0)

    def test_missing_or_invalid_figures_are_refused_not_guessed(self):
        base = dict(quantity=10.0, reference_price=100.0, risk_price=100.0, stop_price=95.0)
        for key in base:
            for bad in (None, 0, -1, math.nan, math.inf, True, "5"):
                with self.assertRaises(QuoteGuardError, msg=f"{key}={bad!r}"):
                    executable_risk_quantity(**{**base, key: bad})


class EntrySizedFromExecutablePrice(unittest.TestCase):
    def _set_ask(self, pa, ask):
        pa.set_price("BTC/USDT", ask / (1 + pa.slippage))        # the paper fill price is the ask

    def test_an_adverse_ask_keeps_the_loss_at_the_stop_within_the_approved_risk(self):
        db, _, pa, eng = make()
        self._set_ask(pa, 100.3)                               # 0.3 % above the 100.0 reference; inside the deviation and spread limits
        self.assertEqual(buy(eng, qty=10.0).outcome, "EXECUTED")
        p = position(db)
        approved_loss = 10.0 * (PAPER_ASK - 95.0)              # the exits-file fixture uses the paper ask as its reference price
        self.assertLess(p["quantity"], 10.0)
        self.assertLessEqual(p["quantity"] * (p["average_price"] - 95.0), approved_loss + 1e-6)
        self.assertGreater(p["quantity"], 9.0)                  # reduced in proportion, not discarded

    def test_an_ask_at_or_below_the_reference_keeps_the_approved_quantity(self):
        db, _, pa, eng = make()
        self._set_ask(pa, 99.9)
        self.assertEqual(buy(eng, qty=10.0).outcome, "EXECUTED")
        self.assertAlmostEqual(position(db)["quantity"], 10.0)

    def test_the_reduction_is_audited(self):
        db, _, pa, eng = make()
        self._set_ask(pa, 100.3)
        buy(eng, qty=10.0)
        with db.read() as c:
            meta = [r[0] for r in c.execute("SELECT metadata FROM audit_log WHERE action='RISK_APPROVED'")]
        self.assertTrue(any("reduced_for_executable_entry_risk_from" in (m or "") for m in meta), meta)

    def test_an_entry_beyond_the_deviation_limit_is_still_blocked_and_creates_nothing(self):
        db, _, pa, eng = make()
        self._set_ask(pa, 101.0)                                # 1 % above the reference; the limit is 0.5 %
        self.assertEqual(buy(eng, qty=10.0).outcome, "BLOCKED")
        self.assertEqual(orders(db), [])


# ------------------------------------------------------------------ P1-3
class WideSpreadExits(unittest.TestCase):
    def _open_then_gap(self, price, slippage, **monitor_kw):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        pa.slippage = slippage                                  # spread = 2 x slippage
        pa.set_price("BTC/USDT", price)
        return db, pa, eng, monitor(eng, **monitor_kw)

    def test_a_triggered_stop_in_a_wide_spread_is_not_sent_is_not_reported_closed_and_raises_a_critical_incident(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01)      # 2 % spread, normal limit 0.25 %, bid 93.06 is below the 95 stop
        rep = run(mon.run_once())
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(orders(db, "SELL"), [])
        self.assertEqual(rep.exits, [])
        self.assertEqual([(w[0], w[1]) for w in rep.wide], [(position(db)["id"], "STOP")])
        inc = incidents(db, WIDE_SPREAD_KIND)
        self.assertEqual([(i["severity"], i["status"]) for i in inc], [("CRITICAL", "OPEN")])
        self.assertIn("NOT sent", inc[0]["detail"])
        with self.assertRaises(RuntimeError):
            run(mon.worker_tick())                              # the worker cannot look healthy

    def test_new_entries_are_blocked_while_the_wide_spread_incident_is_open(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01)
        run(mon.run_once())
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0)
        r = buy(eng, sid="e2", bot="b2")
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("unverified", r.detail.lower())

    def test_the_attempt_count_grows_each_cycle_and_the_streak_is_retried_every_cycle(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01)
        attempts = [run(mon.run_once()).wide[0][3] for _ in range(3)]
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(orders(db, "SELL"), [])

    def test_a_triggered_take_profit_is_handled_the_same_way(self):
        db, pa, eng, mon = self._open_then_gap(120.0, 0.01)     # bid 118.8 is above the 110 target
        rep = run(mon.run_once())
        self.assertEqual([w[1] for w in rep.wide], ["TAKE_PROFIT"])
        self.assertEqual(position(db)["status"], "OPEN")

    def test_with_an_explicit_emergency_limit_the_exit_is_sent_and_audited(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01, emergency_exit_max_spread_pct=5.0)
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual(len(orders(db, "SELL")), 1)
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='EMERGENCY_EXIT_WIDE_SPREAD'").fetchone()[0], 1)

    def test_a_spread_wider_than_the_emergency_limit_is_still_not_sent(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01, emergency_exit_max_spread_pct=1.0)     # spread is 2 %
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(len(rep.wide), 1)

    def test_a_stale_quote_is_not_rescued_by_the_emergency_limit(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01, emergency_exit_max_spread_pct=5.0)
        pa.set_price("BTC/USDT", 94.0, as_of=datetime.now(timezone.utc) - timedelta(minutes=10))
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(rep.wide, [])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertTrue(rep.unverified)

    def test_a_wide_spread_without_a_trigger_is_unverified_protection_not_an_exit(self):
        db, pa, eng, mon = self._open_then_gap(100.0, 0.01, emergency_exit_max_spread_pct=5.0)     # bid 99 is between stop and target
        rep = run(mon.run_once())
        self.assertEqual((rep.exits, rep.wide), ([], []))
        self.assertTrue(rep.unverified)
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(len(incidents(db, INCIDENT_KIND)), 1)
        self.assertEqual(incidents(db, WIDE_SPREAD_KIND), [])

    def test_when_the_spread_narrows_the_normal_exit_closes_it_once_and_the_incident_clears(self):
        db, pa, eng, mon = self._open_then_gap(94.0, 0.01)
        run(mon.run_once())
        run(mon.run_once())
        pa.slippage = 0.0005                                    # the market recovers
        pa.set_price("BTC/USDT", 94.0)
        rep = run(mon.run_once())
        self.assertEqual([e[1] for e in rep.exits], ["STOP"])
        self.assertEqual(position(db)["status"], "CLOSED")
        for _ in range(2):
            run(mon.run_once())
        self.assertEqual([i["status"] for i in incidents(db, WIDE_SPREAD_KIND)], ["RESOLVED"])
        self.assertEqual(len(orders(db, "SELL")), 1)            # no duplicate exit
        self.assertEqual(mon._wide_attempts, {})

    def test_the_override_is_ignored_for_an_entry(self):
        db, _, pa, eng = make()
        pa.slippage = 0.01
        d = __import__("test_stage1_p0_exits").bdec("w1")
        r = asyncio.run(eng.execute(d, bot_id="b1", environment="PAPER", exchange="paper", emergency_max_spread_pct=50.0))
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertEqual(orders(db), [])

    def test_the_monitor_refuses_invalid_policy_values(self):
        db, _, pa, eng = make()
        for kw in ({"max_hold_seconds": 0}, {"max_hold_seconds": -5}, {"max_hold_seconds": math.nan}, {"max_hold_seconds": True},
                   {"emergency_exit_max_spread_pct": 0}, {"emergency_exit_max_spread_pct": 101}, {"emergency_exit_max_spread_pct": math.inf}):
            with self.assertRaises(ValueError, msg=str(kw)):
                ExitMonitor(eng, RiskEngine(), **kw)


# ------------------------------------------------------------------ P1-1
class TimeStop(unittest.TestCase):
    def _aged(self, **kw):
        db, _, pa, eng = make()
        buy(eng)
        box = {"now": datetime.now(timezone.utc)}
        eng.clock = lambda: box["now"]                          # the engine validates the same quote against the same clock
        mon = monitor(eng, clock=lambda: box["now"], **kw)

        def advance(seconds):
            box["now"] += timedelta(seconds=seconds)
            pa.set_price("BTC/USDT", 100.0, as_of=box["now"])      # the feed is alive, price between stop and target
        return db, mon, advance

    def test_by_default_no_time_stop_exists(self):
        db, mon, advance = self._aged()
        advance(10 * 24 * 3600)
        run(mon.run_once())
        self.assertEqual(position(db)["status"], "OPEN")

    def test_an_explicit_limit_exits_at_or_after_the_threshold_and_not_before(self):
        db, mon, advance = self._aged(max_hold_seconds=3600)
        advance(3500)
        self.assertEqual(run(mon.run_once()).exits, [])
        self.assertEqual(position(db)["status"], "OPEN")
        advance(200)
        rep = run(mon.run_once())
        self.assertEqual([e[1] for e in rep.exits], ["MAX_HOLD"])
        self.assertEqual(position(db)["status"], "CLOSED")

    def test_configuration_validation_and_environment_parsing(self):
        for bad in (0, -1, math.nan, math.inf, True, 101):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Config(db_target="x", emergency_exit_max_spread_pct=bad)
        for bad in (0, -1, math.nan, math.inf, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Config(db_target="x", max_hold_seconds=bad)
        self.assertIsNone(Config(db_target="x").max_hold_seconds)
        keys = ("DEWAKO_MAX_HOLD_SECONDS", "DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            c = Config.from_env()
            self.assertEqual((c.max_hold_seconds, c.emergency_exit_max_spread_pct), (None, None))
            os.environ[keys[0]], os.environ[keys[1]] = "3600", "2.5"
            c = Config.from_env()
            self.assertEqual((c.max_hold_seconds, c.emergency_exit_max_spread_pct), (3600.0, 2.5))
            os.environ[keys[0]] = "soon"
            with self.assertRaises(ValueError):
                Config.from_env()
            os.environ[keys[0]] = "-4"
            with self.assertRaises(ValueError):
                Config.from_env()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


def _app(target, **cfg):
    box = {"now": NOW}

    async def prov(symbol, timeframe):
        return MarketState(symbol=symbol, as_of=box["now"], candles=DATA)

    app = Application(Config(db_target=target, workers_enabled=cfg.pop("workers_enabled", True), paper_enabled=True, market_data="none",
                             lease_ttl=60, **cfg), state_provider=prov, clock=lambda: box["now"])
    app._clock_box = box
    return app


async def _started(target, **cfg):
    app = _app(target, **cfg)
    status = await app.startup()
    app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
    return app, status


async def _enter(app):
    await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                 environment="PAPER", exchange="paper")
    app.bot_service.set_enabled("p1", True)
    for w in ("exit_monitor", "risk_equity", "bot_runner"):
        await tick(app, w)


class TimeStopThroughTheRunningApplication(unittest.TestCase):
    def test_the_configured_limit_reaches_the_monitor_and_closes_an_aged_position(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target, max_hold_seconds=3600)
            try:
                self.assertEqual(app.exit_monitor.max_hold_seconds, 3600)
                await _enter(app)
                self.assertEqual(len(rows(app, "SELECT * FROM positions WHERE status='OPEN'")), 1)
                app._clock_box["now"] = datetime.now(timezone.utc) + timedelta(hours=2)      # positions are stamped with the wall clock
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
                await tick(app, "exit_monitor")
                self.assertEqual(rows(app, "SELECT status FROM positions")[0]["status"], "CLOSED")
                self.assertTrue(rows(app, "SELECT 1 FROM execution_intents WHERE signal_id LIKE 'exit:%:MAX_HOLD:%'"))
            finally:
                await app.shutdown()
        run(go())

    def test_without_configuration_the_application_has_no_time_stop(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            try:
                self.assertIsNone(app.exit_monitor.max_hold_seconds)
                await _enter(app)
                app._clock_box["now"] = datetime.now(timezone.utc) + timedelta(days=30)
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
                await tick(app, "exit_monitor")
                self.assertEqual(rows(app, "SELECT status FROM positions")[0]["status"], "OPEN")
            finally:
                await app.shutdown()
        run(go())


# ------------------------------------------------------------------ P1-4
class ReadinessReflectsCurrentState(unittest.TestCase):
    async def _healthy(self, app):
        for w in app.supervisor.workers:
            if w.critical:
                await tick(app, w.name)

    def test_not_ready_until_the_critical_workers_have_reported_healthy_then_ready(self):
        db, target = tmp_db()

        async def go():
            app, status = await _started(target)
            try:
                self.assertEqual(status, "READY")                               # the STARTUP verdict
                self.assertEqual(app.readiness(), (False, "SYSTEM_DEGRADED"))   # nothing has proven itself yet
                await self._healthy(app)
                self.assertEqual(app.readiness(), (True, "READY"))
            finally:
                await app.shutdown()
        run(go())

    def test_a_failed_exit_monitor_makes_it_not_ready_and_recovery_makes_it_ready_again(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            try:
                await _enter(app)
                await self._healthy(app)
                self.assertTrue(app.readiness()[0])
                app._clock_box["now"] = NOW + timedelta(minutes=5)             # the price feed stops
                self.assertFalse(await app.supervisor.run_once(next(w for w in app.supervisor.workers if w.name == "exit_monitor")))
                self.assertEqual(app.readiness(), (False, "SYSTEM_DEGRADED"))
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())      # the feed returns
                for _ in range(3):
                    await self._healthy(app)
                self.assertEqual(app.readiness(), (True, "READY"))
            finally:
                await app.shutdown()
        run(go())

    def test_not_ready_when_stopping_or_when_the_status_is_not_ready(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            await self._healthy(app)
            self.assertTrue(app.readiness()[0])
            await app.shutdown()
            self.assertFalse(app.readiness()[0])
        run(go())
        app = Application(Config(db_target=tmp_db()[1], workers_enabled=False))
        run(app.startup())
        self.assertEqual(app.readiness(), (False, "NOT_CONFIGURED"))
        run(app.shutdown())

    def test_a_supervisor_that_cannot_answer_is_not_ready(self):
        db, target = tmp_db()

        async def go():
            app, _ = await _started(target)
            try:
                await self._healthy(app)

                def boom():
                    raise RuntimeError("cannot tell")
                app.supervisor.healthy = boom
                self.assertEqual(app.readiness(), (False, "SYSTEM_DEGRADED"))
            finally:
                await app.shutdown()
        run(go())

    def test_with_workers_disabled_the_startup_status_is_reported_and_entries_are_refused_anyway(self):
        db, target = tmp_db()

        async def go():
            app, status = await _started(target, workers_enabled=False)
            try:
                self.assertEqual(app.readiness(), (True, status))
                self.assertFalse(app._protection_ok()[0])
            finally:
                await app.shutdown()
        run(go())


if __name__ == "__main__":
    unittest.main()
