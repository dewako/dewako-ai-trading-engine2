"""Stage 1 / P0-3: the real Application (not the unit pieces) wires a paper bot end to end.

Evidence kind: REAL APPLICATION LIFECYCLE (Application class, supervised workers, real database, paper adapter built from configuration).
It is NOT an HTTP test: FastAPI is not installable in the audit sandbox, so the HTTP layer is covered separately (see test_stage1_http.py,
which is skipped here and required in CI).

Market data is deterministic and injected by TEST code as a state provider; production code has no route, flag or setting for fake prices.
"""
import asyncio
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))

import scenarios as S  # noqa: E402
from hardening_helpers import tmp_db  # noqa: E402

from dewako.core.types import MarketState  # noqa: E402
from dewako.runtime.application import Application, Config  # noqa: E402
from dewako.runtime.bot_service import BotError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


_ENV = {"DEWAKO_MAX_SPREAD_PCT": "0.5", "DEWAKO_MAX_PRICE_DEVIATION_PCT": "1.0", "DEWAKO_MAX_QUOTE_AGE_SECONDS": "120"}
_SAVED = {}


def setUpModule():            # the operator-set quote-guard limits that production requires; without them every order is blocked
    for k, v in _ENV.items():
        _SAVED[k] = os.environ.get(k)
        os.environ[k] = v


def tearDownModule():
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


DATA = S.steady_uptrend()
NOW = DATA[-1].t + S.STEP                      # the last candle has just closed
LAST = DATA[-1].c


def make_app(target, now=NOW, price=LAST, provider=True):
    clock_box = {"now": now}

    async def prov(symbol, timeframe):
        return MarketState(symbol=symbol, as_of=clock_box["now"], candles=DATA)

    app = Application(Config(db_target=target, workers_enabled=True, paper_enabled=True, market_data="none", lease_ttl=60),
                      state_provider=prov if provider else None, clock=lambda: clock_box["now"])
    app._clock_box = clock_box
    return app


def worker(app, name):
    return next(w for w in app.supervisor.workers if w.name == name)


async def tick(app, name):
    return await app.supervisor.run_once(worker(app, name))


def rows(app, sql):
    with app.rt.db.read() as c:
        return [dict(r) for r in c.execute(sql)]


async def started(target, price=LAST, **kw):
    app = make_app(target, **kw)
    status = await app.startup()
    app.adapters["paper"].set_price("BTC/USDT", price, as_of=app.clock())
    return app, status


class WiringAndReadiness(unittest.TestCase):
    def test_missing_quote_guard_limits_is_not_ready_even_with_adapter_and_provider(self):
        db, target = tmp_db()
        saved = {k: os.environ.pop(k) for k in _ENV}
        try:
            async def go():
                app, status = await started(target)
                try:
                    self.assertEqual(status, "NOT_CONFIGURED")
                    self.assertIn("quote-guard", [x for x in [s.detail for s in app.steps if s.name == "trading_configuration"]][0])
                finally:
                    await app.shutdown()
            run(go())
        finally:
            os.environ.update(saved)

    def test_nothing_configured_is_not_ready(self):
        db, target = tmp_db()
        app = Application(Config(db_target=target, workers_enabled=False))     # no adapter, no provider
        status = run(app.startup())
        self.assertEqual(status, "NOT_CONFIGURED")
        self.assertEqual(app.adapters, {})
        run(app.shutdown())

    def test_adapter_without_market_data_is_not_ready(self):
        db, target = tmp_db()
        app = Application(Config(db_target=target, workers_enabled=False, paper_enabled=True))
        self.assertEqual(run(app.startup()), "NOT_CONFIGURED")
        run(app.shutdown())

    def test_adapter_and_provider_is_ready_and_registers_the_paper_adapter(self):
        db, target = tmp_db()

        async def go():
            app, status = await started(target)
            try:
                self.assertEqual(status, "READY")
                self.assertIn("paper", app.adapters)
                self.assertEqual(app.adapters["paper"].environment, "PAPER")
                self.assertIn("exit_monitor", [w.name for w in app.supervisor.workers])
            finally:
                await app.shutdown()
        run(go())

    def test_no_hard_coded_default_symbol_is_tracked_when_no_bot_exists(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                await tick(app, "market_data")
                self.assertEqual(rows(app, "SELECT COUNT(*) AS n FROM safety_state WHERE key LIKE 'market_data_candle_at:%'")[0]["n"], 0)
            finally:
                await app.shutdown()
        run(go())


class BotCreationRules(unittest.TestCase):
    def _create(self, app, **kw):
        base = dict(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m", environment="PAPER", exchange="paper")
        base.update(kw)
        return run(app.bot_service.create(**base))

    def test_validation_and_risk_scope_initialisation(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                for bad in (dict(symbol="XAU/USD"), dict(timeframe="1m"), dict(environment="LIVE"), dict(exchange="nope"),
                            dict(strategy="Nope"), dict(bot_id="bad id!")):
                    with self.assertRaises(BotError, msg=str(bad)):
                        await app.bot_service.create(**{**dict(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT",
                                                               timeframe="15m", environment="PAPER", exchange="paper"), **bad})
                self.assertEqual(rows(app, "SELECT COUNT(*) AS n FROM bots")[0]["n"], 0)       # nothing half-created
                bot = await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                                   environment="PAPER", exchange="paper")
                self.assertEqual(bot["enabled"], 0)
                self.assertEqual(len(rows(app, "SELECT * FROM risk_state WHERE scope='PAPER'")), 1)
                with self.assertRaises(BotError):
                    await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                                 environment="PAPER", exchange="paper")
                self.assertEqual(rows(app, "SELECT COUNT(*) AS n FROM audit_log WHERE action='BOT_CREATED'")[0]["n"], 1)
            finally:
                await app.shutdown()
        run(go())


class PaperLifecycle(unittest.TestCase):
    def test_enter_protected_then_exit_on_stop_then_restart_recovers_an_open_position(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p1", True)
                # 1. before the exit monitor has verified anything, no entry is possible
                await tick(app, "bot_runner")
                self.assertEqual(rows(app, "SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
                # 2. the monitor runs (nothing to watch yet, so it is healthy), then the bot enters
                await tick(app, "exit_monitor")
                await tick(app, "risk_equity")
                await tick(app, "bot_runner")
                pos = rows(app, "SELECT * FROM positions")
                self.assertEqual(len(pos), 1, rows(app, "SELECT signal_id,status,rejection_reason FROM signals"))
                p = pos[0]
                self.assertEqual(p["status"], "OPEN")
                self.assertIsNotNone(p["stop_loss"])
                self.assertLess(p["stop_loss"], p["average_price"])
                stop = p["stop_loss"]
                # 3. price falls through the stop; the monitor sells it
                app.adapters["paper"].set_price("BTC/USDT", stop * 0.97, as_of=app.clock())
                await tick(app, "exit_monitor")
                await tick(app, "risk_equity")
                closed = rows(app, "SELECT * FROM positions")[0]
                self.assertEqual(closed["status"], "CLOSED")
                self.assertLess(closed["realized_pnl"], 0)
                self.assertEqual(rows(app, "SELECT COUNT(*) AS n FROM orders WHERE side='SELL'")[0]["n"], 1)
            finally:
                await app.shutdown()
        run(go())

    def test_open_position_survives_a_restart_keeps_its_stop_and_is_still_exitable(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                         environment="PAPER", exchange="paper")
            app.bot_service.set_enabled("p1", True)
            await tick(app, "exit_monitor")
            await tick(app, "risk_equity")
            await tick(app, "bot_runner")
            before = rows(app, "SELECT * FROM positions")
            self.assertEqual(len(before), 1)
            await app.shutdown()                                           # "kill" the application
            app2, status = await started(target)                           # a new process on the same database
            try:
                self.assertEqual(status, "READY")
                after = rows(app2, "SELECT * FROM positions")
                self.assertEqual((after[0]["stop_loss"], after[0]["take_profit"], after[0]["quantity"], after[0]["status"]),
                                 (before[0]["stop_loss"], before[0]["take_profit"], before[0]["quantity"], "OPEN"))
                self.assertAlmostEqual(app2.adapters["paper"].holdings["BTC/USDT"], before[0]["quantity"])     # holdings rebuilt from the database
                app2.adapters["paper"].set_price("BTC/USDT", before[0]["stop_loss"] * 0.95, as_of=app2.clock())
                await tick(app2, "exit_monitor")
                await tick(app2, "risk_equity")
                self.assertEqual(rows(app2, "SELECT status FROM positions")[0]["status"], "CLOSED")
            finally:
                await app2.shutdown()
        run(go())

    def test_a_stale_price_feed_blocks_entries_and_makes_the_exit_monitor_unhealthy(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p1", True)
                await tick(app, "exit_monitor")
                await tick(app, "risk_equity")
                await tick(app, "bot_runner")
                self.assertEqual(len(rows(app, "SELECT * FROM positions")), 1)
                app._clock_box["now"] = NOW + timedelta(minutes=5)        # the feed stops: the price timestamp is now 5 minutes old
                ok = await app.supervisor.run_once(worker(app, "exit_monitor"))
                self.assertFalse(ok)                                       # the worker reports failure, so it cannot look healthy
                self.assertEqual(app.supervisor.status()["exit_monitor"]["status"], "DEGRADED")
                self.assertFalse(app._protection_ok()[0])
                self.assertTrue(any(i["kind"] == "EXIT_PROTECTION_UNVERIFIED" and i["status"] == "OPEN"
                                    for i in rows(app, "SELECT kind, status FROM incidents")))
                self.assertEqual(app.health()["workers"], "unhealthy")
            finally:
                await app.shutdown()
        run(go())

    def test_workers_disabled_means_no_entries(self):
        db, target = tmp_db()

        async def go():
            app = make_app(target)
            app.config.workers_enabled = False
            await app.startup()
            try:
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                self.assertFalse(app._protection_ok()[0])
            finally:
                await app.shutdown()
        run(go())


if __name__ == "__main__":
    unittest.main()
