"""Stage 2 WP2: a triggered stop/target meets normal, abnormal and extreme market conditions.

* normal spread: exits through the ordinary path, no emergency record;
* abnormal spread (above the normal limit, inside the configured exit tolerance): sent, and the emergency use is audited;
* extreme spread (above the tolerance) or no tolerance configured: NOT sent, the position stays OPEN, a CRITICAL incident is raised,
  the deferral is recorded with its reason, new entries are blocked;
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
        db, pa, eng, mon = open_then_move(94.0, 0.01, emergency_exit_max_spread_pct=TOLERANCE)        # spread about 2 %
        rep = run(mon.run_once())
        self.assertEqual([(e[1], e[2]) for e in rep.exits], [("STOP", "EXECUTED")])
        self.assertEqual(position(db)["status"], "CLOSED")
        rows = audit_rows(db, "EMERGENCY_EXIT_WIDE_SPREAD")
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["emergency_limit"]), ("STOP", TOLERANCE))
        self.assertGreater(meta["spread_pct"], NORMAL_LIMIT)

    def test_an_extreme_spread_beyond_the_tolerance_is_deferred_recorded_and_blocks_entries(self):
        db, pa, eng, mon = open_then_move(94.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)        # spread about 10 %
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(position(db)["status"], "OPEN")                                               # not reported closed
        self.assertEqual(orders(db, "SELL"), [])
        inc = incidents(db, WIDE_SPREAD_KIND)
        self.assertEqual([(i["severity"], i["status"]) for i in inc], [("CRITICAL", "OPEN")])
        rows = audit_rows(db, "EXIT_BLOCKED_WIDE_SPREAD")
        self.assertEqual(len(rows), 1)
        meta = json.loads(rows[0]["metadata"])
        self.assertEqual((meta["reason"], meta["emergency_limit"]), ("STOP", TOLERANCE))               # the reason and the limit are on record
        self.assertGreater(meta["spread_pct"], TOLERANCE)
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e2", bot="b2").outcome, "BLOCKED")                              # critical exit protection is degraded

    def test_without_a_configured_tolerance_even_a_modest_wide_spread_is_deferred(self):
        db, pa, eng, mon = open_then_move(94.0, 0.01)                                                  # no emergency tolerance
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(len(incidents(db, WIDE_SPREAD_KIND)), 1)

    def test_the_boundary_a_spread_just_inside_and_just_outside_the_tolerance(self):
        inside = open_then_move(94.0, 0.0245, emergency_exit_max_spread_pct=TOLERANCE)                # spread about 4.9 %
        self.assertEqual([e[2] for e in run(inside[3].run_once()).exits], ["EXECUTED"])
        outside = open_then_move(94.0, 0.0255, emergency_exit_max_spread_pct=TOLERANCE)               # spread about 5.1 %
        self.assertEqual(run(outside[3].run_once()).exits, [])
        self.assertEqual(position(outside[0])["status"], "OPEN")

    def test_a_take_profit_follows_the_same_policy(self):
        db, pa, eng, mon = open_then_move(117.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)       # bid 111.15 is above the 110 target
        rep = run(mon.run_once())
        self.assertEqual(rep.exits, [])
        self.assertEqual([w[1] for w in rep.wide], ["TAKE_PROFIT"])
        self.assertEqual(position(db)["status"], "OPEN")


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
        db, pa, eng, mon = open_then_move(94.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)        # exit deferred
        run(mon.run_once())
        p = position(db)
        self.assertEqual((p["status"], p["quantity"]), ("OPEN", 1.0))
        self.assertIsNone(p["closed_at"])
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0], 0)


class RecoveryAfterDeferral(unittest.TestCase):
    def test_when_the_spread_normalises_the_exit_goes_once_the_incident_clears_and_entries_resume(self):
        db, pa, eng, mon = open_then_move(94.0, 0.05, emergency_exit_max_spread_pct=TOLERANCE)
        run(mon.run_once())
        run(mon.run_once())                                                                            # still deferred: one audit record per streak
        self.assertEqual(len(audit_rows(db, "EXIT_BLOCKED_WIDE_SPREAD")), 1)
        pa.slippage = 0.0005
        pa.set_price("BTC/USDT", 94.0)
        rep = run(mon.run_once())
        self.assertEqual([e[2] for e in rep.exits], ["EXECUTED"])
        self.assertEqual(len(orders(db, "SELL")), 1)
        self.assertEqual(position(db)["status"], "CLOSED")
        run(mon.run_once())
        self.assertEqual([i["status"] for i in incidents(db, WIDE_SPREAD_KIND)], ["RESOLVED"])
        pa.set_price("BTC/USDT", 100.0)
        self.assertEqual(buy(eng, sid="e3", bot="b3").outcome, "EXECUTED")


if __name__ == "__main__":
    unittest.main()
