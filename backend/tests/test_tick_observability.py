"""D3 (Phase 5 remediation): a bot tick is never silent.

Before: the bot worker discarded every TickResult and the application configured no logging, so a tick that found no signal and a
tick that could not get market data looked identical (nothing logged, nothing audited, worker HEALTHY).
Now: one log line per change of evaluated bar or outcome; failing outcomes are WARNINGs; `fail_after` consecutive failing ticks make
the critical bot worker raise, so the system shows DEGRADED until a tick succeeds. Reporting only: no decision, limit or order changes.
"""
import asyncio
import io
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import scenarios as S  # noqa: E402
from hardening_helpers import TEST_QUOTE_GUARD, tmp_db  # noqa: E402

from dewako.core.types import MarketState  # noqa: E402
from dewako.execution.engine import ExecutionEngine  # noqa: E402
from dewako.logging_setup import configure_logging  # noqa: E402
from dewako.risk.engine import RiskEngine  # noqa: E402
from dewako.risk.state import RiskStateStore  # noqa: E402
from dewako.runtime.application import Application, Config  # noqa: E402
from dewako.runtime.bot_runner import BotConfig, BotRunner, TickMonitor, TickResult  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402
from dewako.strategies import library  # noqa: E402,F401
from test_market_execution_guards import QuotePaper  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def capture(logger_name="dewako.test.ticks"):
    lg = logging.getLogger(logger_name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    lg.addHandler(h)
    return lg, buf


def lines(buf):
    return [x for x in buf.getvalue().splitlines() if x]


class MonitorLines(unittest.TestCase):
    def test_no_signal_tick_writes_exactly_one_line_naming_the_bar_and_repeats_are_quiet(self):
        lg, buf = capture()
        m = TickMonitor(logger=lg)
        for _ in range(5):                                   # the worker ticks every 30 s; the same 4h bar is seen many times
            m.observe("b1", TickResult("NO_SIGNAL", "no breakout", bar="2026-11-13T12:00:00+00:00"))
        self.assertEqual(lines(buf), ["INFO bot b1 bar 2026-11-13T12:00:00+00:00: NO_SIGNAL (no breakout)"])
        m.observe("b1", TickResult("NO_SIGNAL", "no breakout", bar="2026-11-13T16:00:00+00:00"))   # a new candle: one new line
        self.assertEqual(len(lines(buf)), 2)
        self.assertIn("2026-11-13T16:00:00", lines(buf)[1])
        self.assertEqual(m.failing(), [])

    def test_failing_ticks_warn_and_name_the_bot_after_fail_after_then_recover(self):
        lg, buf = capture()
        m = TickMonitor(fail_after=3, logger=lg)
        for i in range(2):
            m.observe("b1", TickResult("ERROR", "ProviderError"))
            self.assertEqual(m.failing(), [], f"after {i + 1} failing tick(s)")
        m.observe("b1", TickResult("ERROR", "ProviderError"))
        self.assertEqual(m.failing(), ["b1"])
        out = lines(buf)
        self.assertTrue(out[0].startswith("WARNING bot b1 bar -: ERROR (ProviderError)"), out)
        self.assertEqual(sum("3 consecutive failing ticks" in x for x in out), 1, out)
        self.assertTrue(all(x.startswith("WARNING") for x in out), out)
        m.observe("b1", TickResult("ERROR", "ProviderError"))          # still failing: no repeated threshold warning
        self.assertEqual(sum("consecutive failing ticks" in x for x in lines(buf)), 1)
        m.observe("b1", TickResult("NO_SIGNAL", "no breakout", bar="2026-11-13T16:00:00+00:00"))
        self.assertEqual(m.failing(), [])
        self.assertTrue(lines(buf)[-1].startswith("INFO bot b1"))

    def test_blocked_counts_as_failing_but_risk_rejection_does_not(self):
        m = TickMonitor(fail_after=2, logger=capture()[0])
        for _ in range(3):
            m.observe("r", TickResult("RISK_REJECTED", "cooldown", bar="x"))
            m.observe("h", TickResult("HALTED", "kill switch active", bar="x"))
            m.observe("k", TickResult("BLOCKED", "market data could not be validated", bar="x"))
        self.assertEqual(m.failing(), ["k"])

    def test_a_bot_that_is_no_longer_enabled_stops_counting(self):
        m = TickMonitor(fail_after=1, logger=capture()[0])
        m.observe("gone", TickResult("ERROR", "x"))
        self.assertEqual(m.failing(), ["gone"])
        m.forget_except(["other"])
        self.assertEqual(m.failing(), [])


class RunnerSetsTheBar(unittest.TestCase):
    def _runner(self, provider, now):
        db, _ = tmp_db()
        adapter = QuotePaper()
        adapter.set_price("BTC/USDT", 110)
        engine = ExecutionEngine(db, SafetyController(db), {"paper": adapter}, quote_guard=TEST_QUOTE_GUARD)
        rs = RiskStateStore(db)
        rs.init_scope("PAPER", 10_000)
        return BotRunner(engine, RiskEngine(), rs, provider, clock=lambda: now)

    def test_tick_reports_the_last_candle_it_evaluated(self):
        data = S.steady_uptrend()
        stale_now = data[-1].t + S.STEP * 4                    # stale data: BLOCKED, but the bar it looked at is still reported

        async def prov(symbol, timeframe):
            return MarketState(symbol=symbol, as_of=stale_now, candles=data)
        res = run(self._runner(prov, stale_now).tick(BotConfig("b", "Trend AI", "2.2", "BTC/USDT", "15m", "PAPER", "paper")))
        self.assertEqual(res.outcome, "BLOCKED")
        self.assertEqual(res.bar, data[-1].t.isoformat())

    def test_a_failed_fetch_reports_no_bar(self):
        async def prov(symbol, timeframe):
            raise ConnectionError("down")
        res = run(self._runner(prov, S.steady_uptrend()[-1].t).tick(BotConfig("b", "Trend AI", "2.2", "BTC/USDT", "15m", "PAPER", "paper")))
        self.assertEqual((res.outcome, res.detail, res.bar), ("ERROR", "ConnectionError", ""))


_ENV = {"DEWAKO_MAX_SPREAD_PCT": "0.5", "DEWAKO_MAX_PRICE_DEVIATION_PCT": "1.0", "DEWAKO_MAX_QUOTE_AGE_SECONDS": "120"}


class WorkerShowsDegraded(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV}
        os.environ.update(_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_repeated_fetch_failures_make_the_bot_worker_and_system_degraded_until_a_tick_succeeds(self):
        db, target = tmp_db()
        data = S.steady_uptrend()
        now = data[-1].t + S.STEP
        box = {"fail": False}

        async def prov(symbol, timeframe):
            if box["fail"]:
                raise ConnectionError("feed down")
            return MarketState(symbol=symbol, as_of=now, candles=data)

        async def go():
            app = Application(Config(db_target=target, workers_enabled=True, paper_enabled=True, market_data="none", lease_ttl=60),
                              state_provider=prov, clock=lambda: now)
            self.assertEqual(await app.startup(), "READY")
            try:
                app.adapters["paper"].set_price("BTC/USDT", data[-1].c, as_of=now)
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p1", True)
                for name in ("exit_monitor", "risk_equity"):        # entries wait for exit protection; as in production they run first
                    self.assertTrue(await app.supervisor.run_once(next(x for x in app.supervisor.workers if x.name == name)))
                w = next(x for x in app.supervisor.workers if x.name == "bot_runner")
                self.assertTrue(w.critical)
                self.assertTrue(await app.supervisor.run_once(w))
                box["fail"] = True
                results = [await app.supervisor.run_once(w) for _ in range(3)]
                self.assertEqual(results, [True, True, False], "only the third consecutive failing tick fails the worker")
                with app.rt.db.read() as c:
                    st = c.execute("SELECT status, last_error FROM worker_health WHERE name='bot_runner'").fetchone()
                self.assertEqual(tuple(st), ("DEGRADED", "RuntimeError"))
                self.assertEqual(app.rt.safety.trading_state().value, "SYSTEM_DEGRADED")
                box["fail"] = False
                self.assertTrue(await app.supervisor.run_once(w))
                with app.rt.db.read() as c:
                    self.assertEqual(c.execute("SELECT status FROM worker_health WHERE name='bot_runner'").fetchone()[0], "HEALTHY")
                for x in app.supervisor.workers:                     # every other critical worker healthy too, then the flag clears
                    if x.critical and x.name != "bot_runner":
                        self.assertTrue(await app.supervisor.run_once(x), x.name)
                app.supervisor.recompute_degraded()
                self.assertNotEqual(app.rt.safety.trading_state().value, "SYSTEM_DEGRADED")
            finally:
                await app.shutdown()
        run(go())


class LoggingSetup(unittest.TestCase):
    def tearDown(self):
        lg = logging.getLogger("dewako")
        lg.handlers[:] = [h for h in lg.handlers if not getattr(h, "_dewako_handler", False)]
        lg.setLevel(logging.NOTSET)

    def test_engine_info_lines_reach_the_log_and_setup_is_idempotent(self):
        buf = io.StringIO()
        saved, sys.stderr = sys.stderr, buf
        try:
            configure_logging({})
            configure_logging({})
            logging.getLogger("dewako.bots").info("bot p1 bar X: NO_SIGNAL")
        finally:
            sys.stderr = saved
        self.assertEqual(sum(getattr(h, "_dewako_handler", False) for h in logging.getLogger("dewako").handlers), 1)
        self.assertEqual(buf.getvalue().count("INFO dewako.bots: bot p1 bar X: NO_SIGNAL"), 1)

    def test_invalid_level_refuses(self):
        with self.assertRaises(ValueError):
            configure_logging({"DEWAKO_LOG_LEVEL": "chatty"})
        self.assertEqual(configure_logging({"DEWAKO_LOG_LEVEL": "warning"}).level, logging.WARNING)
