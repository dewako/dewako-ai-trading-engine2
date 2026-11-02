"""Stage 1 / P0-4 (SAF-04): risk inputs must be supplied by a real source or reported NOT_ENFORCED; limits are durable, versioned and audited.

Before the fix: `correlated_risk_pct` was never supplied (always 0.0), `minutes_to_high_impact_event` never supplied, the running loop rebuilt
the default RiskLimits every iteration, equity was never marked to market, and exposure/open-positions counted only one bot's positions.
"""
import asyncio
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import tmp_db  # noqa: E402
from test_stage1_p0_application import (DATA, LAST, NOW, rows, run, setUpModule, started, tearDownModule, tick)  # noqa: E402,F401

from dewako.execution.positions import PositionStore  # noqa: E402
from dewako.persistence.audit import AuditLog  # noqa: E402
from dewako.risk.controls import control_status  # noqa: E402
from dewako.risk.engine import RiskLimits  # noqa: E402
from dewako.risk.limits_store import SIZING_HEADROOM, RiskLimitsStore  # noqa: E402
from dewako.risk.state import RiskStateStore  # noqa: E402

ADMIN = {"user_id": 1, "username": "alice", "role": "ADMIN"}


class DurableLimits(unittest.TestCase):
    def test_defaults_are_labelled_and_changes_are_versioned_audited_and_survive_restart(self):
        db, target = tmp_db()
        store = RiskLimitsStore(db, AuditLog(db))
        lim, meta = store.current()
        self.assertEqual(meta["source"], "DEFAULT")
        self.assertEqual(lim, RiskLimits())
        v = store.set({"max_drawdown_pct": 4.0}, actor=ADMIN, reason="tighten")
        self.assertEqual((v["source"], v["version"]), ("STORED", 1))
        store.set({"max_drawdown_pct": 3.0}, actor=ADMIN)
        self.assertEqual([h["limits"]["max_drawdown_pct"] for h in store.history()], [4.0, 3.0])        # history is kept, never edited
        from dewako.persistence.db import Database
        again = RiskLimitsStore(Database(target) if not target.startswith("postgres") else db)
        self.assertEqual(again.hard_caps().max_drawdown_pct, 3.0)
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE action='RISK_LIMITS_CHANGED'").fetchone()[0], 2)

    def test_invalid_values_and_disabling_the_stop_requirement_are_refused_and_write_nothing(self):
        db, _ = tmp_db()
        store = RiskLimitsStore(db, AuditLog(db))
        for bad in ({"max_position_pct": 500}, {"max_daily_loss_pct": -1}, {"risk_per_trade_pct": 50}, {"require_stop_loss": False}):
            with self.assertRaises(Exception, msg=str(bad)):
                store.set(bad, actor=ADMIN)
        self.assertEqual(store.history(), [])

    def test_sizing_limits_sit_inside_the_hard_caps(self):
        db, _ = tmp_db()
        store = RiskLimitsStore(db, AuditLog(db))
        hard, sizing = store.hard_caps(), store.sizing_limits()
        self.assertAlmostEqual(sizing.max_position_pct, hard.max_position_pct * SIZING_HEADROOM)
        self.assertLess(sizing.max_position_pct, hard.max_position_pct)
        self.assertEqual(sizing.max_daily_loss_pct, hard.max_daily_loss_pct)                       # only size and exposure get headroom


class SuppliedInputs(unittest.TestCase):
    def _positions(self, db):
        ps = PositionStore(db)
        with db.tx() as c:
            ps.open_or_add(c, bot_id="a", exchange="paper", environment="PAPER", symbol="BTC/USDT", side="BUY", quantity=10, price=100, stop_loss=95)
            ps.open_or_add(c, bot_id="b", exchange="paper", environment="PAPER", symbol="ETH/USDT", side="BUY", quantity=20, price=50, stop_loss=45)

    def test_correlated_risk_is_computed_from_open_positions_stop_distance(self):
        db, _ = tmp_db()
        rs = RiskStateStore(db)
        rs.init_scope("PAPER", 10_000)
        self._positions(db)
        ctx = rs.context("PAPER", "c", "PAPER", symbol="SOL/USDT")
        # a: 10 * (100-95) = 50 ; b: 20 * (50-45) = 100 ; one correlation group by default => 150 / 10000 = 1.5 %
        self.assertAlmostEqual(ctx.correlated_risk_pct, 1.5)

    def test_correlation_groups_limit_what_counts(self):
        db, _ = tmp_db()
        rs = RiskStateStore(db, correlation_groups={"BTC/USDT": "crypto", "ETH/USDT": "crypto", "XAU/USD": "gold"})
        rs.init_scope("PAPER", 10_000)
        self._positions(db)
        self.assertAlmostEqual(rs.context("PAPER", "c", "PAPER", symbol="XAU/USD").correlated_risk_pct, 0.0)
        self.assertAlmostEqual(rs.context("PAPER", "c", "PAPER", symbol="ETH/USDT").correlated_risk_pct, 1.5)

    def test_exposure_and_open_positions_count_every_bot_not_just_the_asking_bot(self):
        db, _ = tmp_db()
        rs = RiskStateStore(db)
        rs.init_scope("PAPER", 10_000)
        self._positions(db)
        ctx = rs.context("PAPER", "someone-else", "PAPER")
        self.assertEqual(ctx.open_positions, 2)
        self.assertAlmostEqual(ctx.exposure_pct, (10 * 100 + 20 * 50) / 10_000 * 100)

    def test_a_position_without_a_stop_counts_its_whole_notional_as_risk(self):
        db, _ = tmp_db()
        rs = RiskStateStore(db)
        rs.init_scope("PAPER", 10_000)
        with db.tx() as c:       # legacy row written before stops were mandatory
            c.execute("INSERT INTO positions(bot_id,exchange,environment,market_type,symbol,side,quantity,entry_price,average_price,leverage,status,opened_at,updated_at) "
                      "VALUES ('a','paper','PAPER','SPOT','BTC/USDT','BUY',10,100,100,1,'OPEN','2026-01-01','2026-01-01')")
        self.assertAlmostEqual(rs.context("PAPER", "x", "PAPER").correlated_risk_pct, 10.0)


class ControlsAreReportedHonestly(unittest.TestCase):
    def test_news_blackout_and_live_pnl_are_never_reported_as_enforced(self):
        r = control_status(equity_feed_ok=True)
        self.assertEqual(r["high_impact_news_blackout"]["status"], "NOT_ENFORCED")
        self.assertEqual(r["live_realised_pnl"]["status"], "NOT_ENFORCED")
        self.assertEqual(r["max_drawdown"]["status"], "ENFORCED")

    def test_drawdown_and_daily_loss_depend_on_a_healthy_equity_feed(self):
        r = control_status(equity_feed_ok=False)
        self.assertEqual(r["max_drawdown"]["status"], "NOT_ENFORCED")
        self.assertEqual(r["max_daily_loss"]["status"], "NOT_ENFORCED")
        self.assertTrue(r["max_drawdown"]["reason"])


class UnrealisedLossBlocksNewEntries(unittest.TestCase):
    def test_drawdown_from_an_unrealised_loss_blocks_a_new_entry_through_the_running_application(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                app.limits_store.set({"max_drawdown_pct": 0.01}, actor=ADMIN, reason="test: very tight drawdown")
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p1", True)
                await tick(app, "exit_monitor"); await tick(app, "risk_equity"); await tick(app, "bot_runner")
                self.assertEqual(len(rows(app, "SELECT * FROM positions")), 1)
                stop = rows(app, "SELECT stop_loss FROM positions")[0]["stop_loss"]
                dip = max(LAST * 0.997, stop * 1.001)                       # a small unrealised loss, still above the stop
                app.adapters["paper"].set_price("BTC/USDT", dip, as_of=app.clock())
                await tick(app, "risk_equity")
                st = rows(app, "SELECT current_equity, peak_equity FROM risk_state WHERE scope='PAPER'")[0]
                self.assertLess(st["current_equity"], st["peak_equity"])    # the unrealised loss is visible to the risk layer
                # a second bot on another symbol now asks to enter
                app.adapters["paper"].set_price("ETH/USDT", LAST, as_of=app.clock())
                await app.bot_service.create(bot_id="p2", strategy="Trend AI", version="2.2", symbol="ETH/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p2", True)
                await tick(app, "bot_runner")
                p2 = rows(app, "SELECT status, rejection_reason FROM signals WHERE bot_id='p2'")
                self.assertTrue(p2 and p2[0]["status"] == "REJECTED" and "drawdown" in (p2[0]["rejection_reason"] or "").lower(), p2)
                self.assertEqual(len(rows(app, "SELECT * FROM positions WHERE bot_id='p2'")), 0)
            finally:
                await app.shutdown()
        run(go())

    def test_a_failing_equity_feed_makes_the_worker_unhealthy_and_blocks_entries(self):
        db, target = tmp_db()

        async def go():
            app, _ = await started(target)
            try:
                await app.bot_service.create(bot_id="p1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                             environment="PAPER", exchange="paper")
                app.bot_service.set_enabled("p1", True)
                await tick(app, "exit_monitor"); await tick(app, "risk_equity"); await tick(app, "bot_runner")
                self.assertEqual(len(rows(app, "SELECT * FROM positions")), 1)
                app._clock_box["now"] = NOW + timedelta(minutes=5)         # the price feed stops; marks go stale
                ok = await app.supervisor.run_once(next(w for w in app.supervisor.workers if w.name == "risk_equity"))
                self.assertFalse(ok)
                self.assertFalse(app._protection_ok()[0])
                self.assertIn("risk_equity", app._protection_ok()[1])
            finally:
                await app.shutdown()
        run(go())


if __name__ == "__main__":
    unittest.main()
