"""Stage 2 WP2 (+ WP2b policy split): a triggered exit meets normal, abnormal and extreme market conditions.

STOP-LOSS: never deferred for spread. It is sent at once through any spread; only a missing, stale, crossed, non-positive or
non-finite quote stops it (then protection is unverified, never a guessed price).
TAKE-PROFIT and TIME STOP keep the bounded tolerance (DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT):
* normal spread: exits through the ordinary path, no emergency record;
* abnormal spread (above the normal limit, inside the configured exit tolerance): sent, and the emergency use is audited;
* a take-profit / time stop beyond the tolerance (or with none configured) WAITS: audited with its reason, no incident, the worker stays
  healthy, new entries are not blocked; a stop-loss goes out at once, except through an absurd spread (a suspect quote), which must persist
  for several cycles first, loudly (CRITICAL incident, entries blocked);
* malformed quotes (crossed, zero, negative, NaN, missing side): no order, an unverified-protection incident, never a guessed price;
* the fill price is the quoted bid, never the stop or take-profit price (no fabricated fill), and nothing is reported closed before the
  exit has filled.

Paper trading only. Nothing here contacts an exchange.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from test_stage1_1_p1 import incidents  # noqa: E402
from test_stage1_p0_exits import buy, make, monitor, orders, position, run  # noqa: E402

from dewako.execution.exit_monitor import INCIDENT_KIND, WIDE_SPREAD_KIND  # noqa: E402

TP_PRICE = 120.0             # bid 118.8 at a 2 % spread: above the 110 take-profit
NORMAL_LIMIT = 0.25          # TEST_QUOTE_GUARD.max_spread_pct (percent)
TOLERANCE = 5.0              # the configured exit-spread tolerance for these tests (percent)


def open_then_move(price, slippage, **monitor_kw):
    """An open BTC/USDT position (stop 95, take-profit 110), then the market moves to `price` with half-spread `slippage`."""
    db, _, pa, eng = make()
    assert buy(eng).outcome == "EXECUTED"
    pa.slippage = slippage                                   # bid = price x (1 - s), ask = price x (1 + s): spread is about 2 s
    pa.set_price("BTC/USDT", price)
    return db, pa, eng, monitor(eng, **monitor_kw)


def audit_rows(db, action):
    with db.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]


class SpreadMatrix(unittest.TestCase):
    def test_a_normal_spread_exits_through_the_ordinary_path(self):
        db, pa, eng, mon = open_then_move(94.0, 0.0005, emergency_exit_max_spread_pct=TOLERANCE)       # spread about 0.1 %
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual(audit_rows(db, "EMERGENCY_EXIT_WIDE_SPREAD"), [])
        self.assertEqual(incidents(db, WIDE_SPREAD_KIND), [])

    def test_an_abnormal_spread_inside_the_tolerance_is_sent_and_the_emergency_use_is_recorded(self):
        db, pa, eng, mon = open_then_move(TP_PRICE, 0.01, emergency_exit_max_spread_pct=TOLERANCE)    # spread about 2 %
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("TAKE_PROFIT", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")
        rows = audit_rows(db, "EMERGENCY_EXIT_WIDE_SPREAD")
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["emergency_limit"]), ("TAKE_PROFIT", TOLERANCE))
        self.assertGreater(meta["spread_pct"], NORMAL_LIMIT)

    def test_an_extreme_spread_beyond_the_tolerance_makes_a_take_profit_wait_audited_and_without_blocking_entries(self):
        db, pa, eng, mon = open_then_move(117.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)       # spread about 10 %, bid 111.15 > 110
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(position(db)["status"], "OPEN")                                               # not reported closed
        self.assertEqual(orders(db, "SELL"), [])
        self.assertEqual([(w[1], round(w[2])) for w in rep.waiting], [("TAKE_PROFIT", 10)])
        self.assertEqual((rep.wide, rep.unverified), ([], []))
        self.assertEqual((incidents(db, WIDE_SPREAD_KIND), incidents(db, INCIDENT_KIND)), ([], []))   # a waiting target is not an incident
        rows = audit_rows(db, "EXIT_WAITING_WIDE_SPREAD")
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["tolerance"]), ("TAKE_PROFIT", TOLERANCE))              # the reason and the tolerance are on record
        self.assertGreater(meta["spread_pct"], TOLERANCE)
        run(mon.worker_tick())                                                                         # the worker stays healthy
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e2", bot="b2").outcome, "EXECUTED")                             # entries are not blocked by a waiting target

    def test_without_a_configured_tolerance_even_a_modest_wide_spread_makes_a_take_profit_wait(self):
        db, pa, eng, mon = open_then_move(TP_PRICE, 0.01)                                              # no emergency tolerance
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(len(rep.waiting), 1)
        self.assertEqual(incidents(db, WIDE_SPREAD_KIND), [])

    def test_the_boundary_a_spread_just_inside_and_just_outside_the_tolerance(self):
        inside = open_then_move(TP_PRICE, 0.0245, emergency_exit_max_spread_pct=TOLERANCE)            # spread about 4.9 %
        self.assertEqual([e[2] for e in run(inside[3].run_once()).exits], ["EXECUTED"])
        outside = open_then_move(TP_PRICE, 0.0255, emergency_exit_max_spread_pct=TOLERANCE)           # spread about 5.1 %
        self.assertEqual(run(outside[3].run_once()).exits, [])
        self.assertEqual(position(outside[0])["status"], "OPEN")

    def test_a_time_stop_keeps_the_tolerance_policy_like_a_take_profit(self):
        from datetime import datetime, timedelta, timezone
        db, _, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        eng.clock = lambda: later
        pa.slippage = 0.01
        pa.set_price("BTC/USDT", 100.0, as_of=later)                                                   # bid 99: between stop 95 and target 110
        mon = monitor(eng, max_hold_seconds=3600, clock=lambda: later)
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual([w[1] for w in rep.waiting], ["MAX_HOLD"])                                    # waits: no tolerance configured
        self.assertEqual((rep.wide, incidents(db, WIDE_SPREAD_KIND)), ([], []))                        # a time stop is not an emergency either
        self.assertEqual(position(db)["status"], "OPEN")


class StopLossGoesOutAtOnce(unittest.TestCase):
    """A stop-loss exits at market now, through any spread a real market can show, and the loss beyond the stop is recorded honestly. The one
    exception is a spread above the suspect-quote limit (default 10 %), which must persist for 3 consecutive cycles before it is believed."""
    SPREADS = (0.0005, 0.01, 0.04, 0.0495)                      # half-spreads: about 0.1 %, 2 %, 8 % and 9.9 %

    def test_through_any_spread_up_to_the_suspect_limit_and_with_no_tolerance_the_stop_is_sent_at_once(self):
        for slippage in self.SPREADS:
            with self.subTest(spread_pct=round(slippage * 200, 2)):
                db, pa, eng, mon = open_then_move(94.0, slippage)                                      # NO exit tolerance configured at all
                rep = run(mon.run_once())
                self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
                self.assertEqual(position(db)["status"], "CLOSED")
                self.assertEqual(len(orders(db, "SELL")), 1)
                self.assertEqual((incidents(db, WIDE_SPREAD_KIND), audit_rows(db, "EXIT_STOP_QUOTE_SUSPECT")), ([], []))
                with db.read() as c:
                    fill = c.execute("SELECT price FROM trades WHERE side='SELL'").fetchone()[0]
                self.assertAlmostEqual(fill, 94.0 * (1 - slippage), places=6)                          # the quoted bid, not the stop price

    def test_a_wide_spread_stop_is_audited_as_an_immediate_stop_loss_exit(self):
        db, pa, eng, mon = open_then_move(94.0, 0.04)
        run(mon.run_once())
        rows = audit_rows(db, "EMERGENCY_EXIT_WIDE_SPREAD")
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["policy"], meta["emergency_limit"]), ("STOP", "STOP_LOSS_IMMEDIATE", None))
        self.assertGreater(meta["spread_pct"], NORMAL_LIMIT)

    def test_a_configured_tolerance_neither_delays_nor_changes_a_stop(self):
        db, pa, eng, mon = open_then_move(94.0, 0.04, emergency_exit_max_spread_pct=TOLERANCE)         # 8 % spread, beyond the 5 % tolerance
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")

    def test_a_stop_is_sent_once_and_a_second_cycle_does_not_sell_again(self):
        db, pa, eng, mon = open_then_move(94.0, 0.04)
        run(mon.run_once())
        run(mon.run_once())
        self.assertEqual(len(orders(db, "SELL")), 1)
        self.assertEqual(position(db)["quantity"], 0)

    def test_new_entries_are_not_blocked_after_an_immediate_stop_exit(self):
        db, pa, eng, mon = open_then_move(94.0, 0.04)
        run(mon.run_once())
        run(mon.run_once())
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e5", bot="b5").outcome, "EXECUTED")

    def test_a_stale_quote_still_stops_a_stop_exit_and_a_price_is_never_guessed(self):
        from datetime import datetime, timedelta, timezone
        db, pa, eng, mon = open_then_move(94.0, 0.04)
        pa.set_price("BTC/USDT", 94.0, as_of=datetime.now(timezone.utc) - timedelta(minutes=10))
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(orders(db, "SELL"), [])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual([i["status"] for i in incidents(db, INCIDENT_KIND)], ["OPEN"])


class SuspectStopQuote(unittest.TestCase):
    """A spread above the suspect-quote limit is far more likely a bad quote than a market: selling on it can dump the position at a glitch
    price. It must persist for the confirmation cycles first; the hold is never silent."""
    GLITCH = 0.2                                                  # half-spread 20 %: spread about 40 %

    def test_the_stop_is_held_for_two_cycles_loudly_and_entries_are_blocked(self):
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH)
        for cycle in (1, 2):
            rep = run(mon.run_once())
            self.assertEqual((rep.exits, orders(db, "SELL")), ([], []), cycle)
            self.assertEqual(position(db)["status"], "OPEN", cycle)
            self.assertEqual([(w[1], w[3]) for w in rep.wide], [("STOP", cycle)], cycle)
            self.assertTrue(rep.unverified, cycle)                                                     # protection is reported as NOT enforceable
        self.assertEqual([(i["severity"], i["status"]) for i in incidents(db, WIDE_SPREAD_KIND)], [("CRITICAL", "OPEN")])
        rows = audit_rows(db, "EXIT_STOP_QUOTE_SUSPECT")
        self.assertEqual(len(rows), 1)                                                                 # one record per streak
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["suspect_limit"], meta["confirm_cycles"]), ("STOP", 10.0, 3))
        self.assertEqual(buy(eng, sid="e2", bot="b2").outcome, "BLOCKED")                              # entries are blocked while the stop is held
        self.assertEqual(len(orders(db, "SELL")), 0)

    def test_while_the_stop_is_held_the_worker_does_not_look_healthy(self):
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH)
        with self.assertRaises(RuntimeError):
            run(mon.worker_tick())                                                                     # first cycle: held, protection not enforceable
        self.assertEqual(orders(db, "SELL"), [])

    def test_a_persisting_wide_quote_is_believed_and_the_stop_sells_at_the_quoted_bid(self):
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH)
        run(mon.run_once())
        run(mon.run_once())
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        with db.read() as c:
            fill = c.execute("SELECT price FROM trades WHERE side='SELL'").fetchone()[0]
        self.assertAlmostEqual(fill, 94.0 * (1 - self.GLITCH), places=6)
        meta = json.loads(audit_rows(db, "EMERGENCY_EXIT_WIDE_SPREAD")[0]["metadata"])
        self.assertEqual(meta["policy"], "STOP_LOSS_CONFIRMED_WIDE")
        for _ in range(2):
            run(mon.run_once())
        self.assertEqual([i["status"] for i in incidents(db, WIDE_SPREAD_KIND)], ["RESOLVED"])
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e3", bot="b3").outcome, "EXECUTED")

    def test_a_glitch_that_clears_before_confirmation_never_sells_at_the_glitch_price(self):
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH)
        run(mon.run_once())
        run(mon.run_once())
        pa.slippage = 0.0005                                                                           # the feed recovers: a normal quote, bid still below the stop
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        with db.read() as c:
            fill = c.execute("SELECT price FROM trades WHERE side='SELL'").fetchone()[0]
        self.assertAlmostEqual(fill, 94.0 * (1 - 0.0005), places=6)                                    # sold at the real price, not the glitch
        self.assertEqual(mon._wide_attempts, {})

    def test_the_streak_restarts_after_a_clean_cycle(self):
        db, pa, eng, mon = open_then_move(100.0, self.GLITCH)                                          # bid 80 is below the stop: triggered
        self.assertEqual(run(mon.run_once()).wide[0][3], 1)
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0 + 0.0)                                                          # normal quote, bid 99.95: no trigger
        run(mon.run_once())
        self.assertEqual(mon._wide_attempts, {})

    def test_the_limit_and_the_confirmation_are_configurable(self):
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH, stop_suspect_spread_pct=50.0)             # 40 % is under a 50 % limit: sent at once
        self.assertEqual([e[2] for e in run(mon.run_once()).exits], ["EXECUTED"])
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH, stop_suspect_confirm_cycles=1)            # no confirmation wait at all
        self.assertEqual([e[2] for e in run(mon.run_once()).exits], ["EXECUTED"])
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH, stop_suspect_spread_pct=None)             # the safeguard switched off
        self.assertEqual([e[2] for e in run(mon.run_once()).exits], ["EXECUTED"])
        db, pa, eng, mon = open_then_move(94.0, self.GLITCH, stop_suspect_confirm_cycles=5)
        for _ in range(4):
            self.assertEqual(run(mon.run_once()).exits, [])
        self.assertEqual([e[2] for e in run(mon.run_once()).exits], ["EXECUTED"])

    def test_invalid_policy_values_are_refused(self):
        db, _, pa, eng = make()
        for kw in ({"stop_suspect_spread_pct": 0}, {"stop_suspect_spread_pct": -1}, {"stop_suspect_spread_pct": 101},
                   {"stop_suspect_spread_pct": float("nan")}, {"stop_suspect_spread_pct": True},
                   {"stop_suspect_confirm_cycles": 0}, {"stop_suspect_confirm_cycles": -1}, {"stop_suspect_confirm_cycles": 1.5},
                   {"stop_suspect_confirm_cycles": True}):
            with self.assertRaises(ValueError, msg=str(kw)):
                monitor(eng, **kw)


class MalformedQuotes(unittest.TestCase):
    def _with_quote(self, **changes):
        db, pa, eng, mon = open_then_move(94.0, 0.0005, emergency_exit_max_spread_pct=TOLERANCE)
        original = pa.get_ticker

        async def broken(symbol):
            q = dict(await original(symbol))
            q.update(changes)
            return q
        pa.get_ticker = broken
        return db, pa, eng, mon

    def test_a_malformed_quote_never_produces_an_order_a_guessed_price_or_a_closed_position(self):
        bid = 93.0
        cases = {
            "crossed": dict(bid=95.0, ask=bid),
            "zero bid": dict(bid=0.0),
            "negative bid": dict(bid=-1.0),
            "nan bid": dict(bid=float("nan")),
            "infinite ask": dict(ask=float("inf")),
            "missing ask": dict(ask=None),
            "missing bid": dict(bid=None),
        }
        for name, changes in cases.items():
            with self.subTest(name):
                db, pa, eng, mon = self._with_quote(**changes)
                rep = run(mon.run_once())
                self.assertEqual(rep.exits, [], name)
                self.assertEqual(orders(db, "SELL"), [], name)
                self.assertEqual(position(db)["status"], "OPEN", name)
                self.assertTrue(rep.unverified, name)
                self.assertEqual([i["status"] for i in incidents(db, INCIDENT_KIND)], ["OPEN"], name)
                self.assertEqual(buy(eng, sid="e2", bot="b2").outcome, "BLOCKED", name)


class NoFabricatedFill(unittest.TestCase):
    def test_a_gap_through_the_stop_fills_at_the_quoted_bid_not_at_the_stop_price(self):
        db, pa, eng, mon = open_then_move(90.0, 0.0005, emergency_exit_max_spread_pct=TOLERANCE)       # gaps from 100 to 90, stop was 95
        run(mon.run_once())
        with db.read() as c:
            fills = [r[0] for r in c.execute("SELECT price FROM trades WHERE side='SELL'")]
        self.assertEqual(len(fills), 1)
        bid = 90.0 * (1 - 0.0005)
        self.assertAlmostEqual(fills[0], bid, places=6)
        self.assertLess(fills[0], 95.0)                                                                # the loss beyond the stop is real and recorded
        self.assertLess(position(db)["realized_pnl"], 0)

    def test_nothing_is_closed_before_the_exit_has_filled(self):
        db, pa, eng, mon = open_then_move(117.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)       # take-profit deferred
        run(mon.run_once())
        p = position(db)
        self.assertEqual((p["status"], p["quantity"]), ("OPEN", 1.0))
        self.assertIsNone(p["closed_at"])
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0], 0)


class RecoveryAfterWaiting(unittest.TestCase):
    def test_when_the_spread_normalises_the_waiting_take_profit_exits_once_and_nothing_was_ever_an_incident(self):
        db, pa, eng, mon = open_then_move(117.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)
        run(mon.run_once())
        run(mon.run_once())                                                                            # still waiting: one audit record per streak
        self.assertEqual(len(audit_rows(db, "EXIT_WAITING_WIDE_SPREAD")), 1)
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 117.0)
        rep = run(mon.run_once())
        self.assertEqual([e[2] for e in rep.exits], ["EXECUTED"])
        self.assertEqual(len(orders(db, "SELL")), 1)
        self.assertEqual(position(db)["status"], "CLOSED")
        run(mon.run_once())
        self.assertEqual((incidents(db), mon._waiting), ([], {}))
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e3", bot="b3").outcome, "EXECUTED")

    def test_a_waiting_take_profit_is_still_protected_by_an_immediate_stop(self):
        db, pa, eng, mon = open_then_move(117.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)
        run(mon.run_once())                                                                            # take-profit waiting
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 90.0)                                                                 # the market reverses through the stop
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")


class ConfigurationPath(unittest.TestCase):
    ENV = ("DEWAKO_STOP_SUSPECT_SPREAD_PCT", "DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES")

    def _from_env(self, **env):
        from unittest import mock

        from dewako.runtime.application import Config
        clean = {k: v for k, v in os.environ.items() if k not in self.ENV}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return Config.from_env()

    def test_defaults_are_the_protective_ones(self):
        c = self._from_env()
        self.assertEqual((c.stop_suspect_spread_pct, c.stop_suspect_confirm_cycles), (10.0, 3))

    def test_environment_overrides_and_off(self):
        c = self._from_env(DEWAKO_STOP_SUSPECT_SPREAD_PCT="25", DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES="2")
        self.assertEqual((c.stop_suspect_spread_pct, c.stop_suspect_confirm_cycles), (25.0, 2))
        self.assertIsNone(self._from_env(DEWAKO_STOP_SUSPECT_SPREAD_PCT="off").stop_suspect_spread_pct)

    def test_invalid_values_refuse_to_start(self):
        for env in ({"DEWAKO_STOP_SUSPECT_SPREAD_PCT": "abc"}, {"DEWAKO_STOP_SUSPECT_SPREAD_PCT": "0"},
                    {"DEWAKO_STOP_SUSPECT_SPREAD_PCT": "-5"}, {"DEWAKO_STOP_SUSPECT_SPREAD_PCT": "150"},
                    {"DEWAKO_STOP_SUSPECT_SPREAD_PCT": "nan"}, {"DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES": "0"},
                    {"DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES": "x"}, {"DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES": "1.5"}):
            with self.assertRaises(ValueError, msg=str(env)):
                self._from_env(**env)

    def test_the_running_application_gives_the_values_to_its_exit_monitor(self):
        from hardening_helpers import tmp_db as _tmp
        from test_stage1_1_p1 import _started
        db, target = _tmp()

        async def go():
            app, _ = await _started(target, stop_suspect_spread_pct=25.0, stop_suspect_confirm_cycles=2)
            try:
                self.assertEqual((app.exit_monitor.stop_suspect_spread_pct, app.exit_monitor.stop_suspect_confirm_cycles), (25.0, 2))
            finally:
                await app.shutdown()
            app2, _ = await _started(_tmp()[1])
            try:
                self.assertEqual((app2.exit_monitor.stop_suspect_spread_pct, app2.exit_monitor.stop_suspect_confirm_cycles), (10.0, 3))
            finally:
                await app2.shutdown()
        import asyncio
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
