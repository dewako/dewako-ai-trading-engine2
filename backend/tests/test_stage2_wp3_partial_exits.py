"""Stage 2 WP3: partial exits are tracked durably and can never double-sell or close a position that still holds quantity.

* every SELL/EXIT order has a `position_exits` row (reason, requested / filled / remaining, status, reconciliation status), kept in step with
  the order inside the same transaction;
* a partly sold position is REDUCING while its exit is active, OPEN again if the exit ends with quantity left, CLOSED only when nothing is left;
* a duplicate request makes no second order and no second sale; replayed exchange reports apply a fill once;
* after a restart a pending partial exit is picked up from the database and finished from the exchange's report, not resent;
* an exit that makes no progress is reported as unprotected (incident, entries blocked); nothing is cancelled or resent automatically.
Paper trading only. Nothing here contacts an exchange.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import TEST_QUOTE_GUARD, tmp_db  # noqa: E402
from test_stage1_p0_exits import bdec, buy, make, monitor, orders, position, run  # noqa: E402

from dewako.core.types import OrderStatus, Side  # noqa: E402
from dewako.execution.engine import ExecutionEngine  # noqa: E402
from dewako.paper_trading.paper_exchange import PaperAdapter  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402


class Slow(PaperAdapter):
    """Exits are accepted but only filled `first` of the way (0 = not at all); the test finishes them with advance()/cancel_rest()."""
    first = 0.5

    async def create_order(self, req):
        if req.side is Side.BUY:
            return await super().create_order(req)
        if req.client_order_id in self.orders:
            return self.orders[req.client_order_id]
        px = self.prices[req.symbol] * (1 - self.slippage)
        f = req.quantity * self.first
        self._sell(req.symbol, px, f)
        o = self._mk(req, px, f)
        self.orders[req.client_order_id] = o
        return o

    def _sell(self, symbol, px, qty):
        self.cash += px * qty
        self.holdings[symbol] = self.holdings.get(symbol, 0) - qty

    def _mk(self, req, px, filled):
        from dewako.core.types import Order, OrderType
        st = OrderStatus.FILLED if filled >= req.quantity else OrderStatus.PARTIAL if filled > 0 else OrderStatus.NEW
        return Order(exchange_order_id=f"S{len(self.orders)}", client_order_id=req.client_order_id, symbol=req.symbol, side=req.side,
                     type=OrderType.MARKET, quantity=req.quantity, filled=filled, price=px, status=st)

    def advance(self, cid, to_filled, done=False):
        o = self.orders[cid]
        self._sell(o.symbol, o.price, to_filled - o.filled)
        self.orders[cid] = o.model_copy(update={"filled": to_filled, "status": OrderStatus.FILLED if done else OrderStatus.PARTIAL})

    def cancel_rest(self, cid):
        o = self.orders[cid]
        self.orders[cid] = o.model_copy(update={"status": OrderStatus.CANCELLED})


def exits(db):
    with db.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM position_exits ORDER BY id")]


def sells(db):
    return orders(db, "SELL")


def stop_out(eng, pa):
    pa.set_price("BTC/USDT", 94.0)
    return run(monitor(eng).run_once())


class Tracked(unittest.TestCase):
    def test_a_partial_exit_records_reason_quantities_order_and_reconciliation_status(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        (e,) = exits(db)
        (o,) = sells(db)
        self.assertEqual((e["reason"], e["status"], e["reconciliation_status"], e["order_id"]), ("STOP", "PARTIAL", "CLEAR", o["id"]))
        self.assertEqual((e["requested_quantity"], e["filled_quantity"], e["remaining_quantity"]), (1.0, 0.5, 0.5))
        p = position(db)
        self.assertEqual((p["status"], p["quantity"], e["position_id"]), ("REDUCING", 0.5, p["id"]))

    def test_a_delayed_exit_is_pending_then_completes_and_closes_exactly(self):
        db, _, pa, eng = make(Slow)
        pa.first = 0.0
        buy(eng)
        stop_out(eng, pa)
        self.assertEqual((exits(db)[0]["status"], position(db)["status"], position(db)["quantity"]), ("PENDING", "OPEN", 1.0))
        cid = sells(db)[0]["client_order_id"]
        pa.advance(cid, 1.0, done=True)
        run(eng.sync_active_orders())
        self.assertEqual((exits(db)[0]["status"], exits(db)[0]["remaining_quantity"]), ("COMPLETE", 0.0))
        self.assertEqual((position(db)["status"], position(db)["quantity"]), ("CLOSED", 0))

    def test_the_remainder_fills_in_stages_and_a_replayed_report_changes_nothing(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        cid = sells(db)[0]["client_order_id"]
        pa.advance(cid, 0.8)
        run(eng.sync_active_orders())
        run(eng.sync_active_orders())                                                     # the same report again
        self.assertEqual((position(db)["status"], round(position(db)["quantity"], 9)), ("REDUCING", 0.2))
        pa.advance(cid, 1.0, done=True)
        run(eng.sync_active_orders())
        run(eng.sync_active_orders())
        self.assertEqual((position(db)["status"], position(db)["quantity"]), ("CLOSED", 0))
        self.assertEqual(len(sells(db)), 1)                                               # one order, never a second sale
        with db.read() as c:
            sold = c.execute("SELECT SUM(quantity) FROM trades WHERE side='SELL'").fetchone()[0]
        self.assertAlmostEqual(sold, 1.0)

    def test_an_inconsistent_filled_report_is_not_trusted_and_never_closes_the_position(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        pa.advance(sells(db)[0]["client_order_id"], 0.9, done=True)                       # exchange says FILLED, but only 0.9 of 1.0
        run(eng.sync_active_orders())
        p = position(db)
        self.assertNotEqual(p["status"], "CLOSED")
        self.assertEqual(p["quantity"], 0.5)                                              # the report was not applied
        self.assertEqual((exits(db)[0]["status"], exits(db)[0]["reconciliation_status"]), ("UNKNOWN", "REQUIRED"))
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM incidents WHERE kind='SYNC_RESPONSE_UNTRUSTED' AND status='OPEN'").fetchone()[0], 1)

    def test_a_fill_for_less_than_the_position_leaves_the_rest_open_never_closed(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        from dewako.execution.positions import PositionStore
        with db.tx() as c:
            _, _, closed = PositionStore(db).reduce(c, bot_id="b1", exchange="paper", environment="PAPER", symbol="BTC/USDT", quantity=0.9,
                                                    price=100.0, exchange_confirmed=True)
        self.assertFalse(closed)
        self.assertEqual((position(db)["status"], round(position(db)["quantity"], 9)), ("OPEN", 0.1))


class EndsWithQuantityLeft(unittest.TestCase):
    def test_a_cancelled_remainder_returns_to_open_and_the_next_exit_sells_only_what_is_left(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        pa.cancel_rest(sells(db)[0]["client_order_id"])
        run(eng.sync_active_orders())
        self.assertEqual((exits(db)[0]["status"], position(db)["status"], position(db)["quantity"]), ("CANCELLED", "OPEN", 0.5))
        pa.first = 1.0
        run(monitor(eng).run_once())
        second = sells(db)[1]
        self.assertAlmostEqual(second["quantity"], 0.5)
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual([e["status"] for e in exits(db)], ["CANCELLED", "COMPLETE"])

    def test_a_rejected_exit_is_failed_tracked_leaves_the_position_open_and_can_be_retried(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        real = pa.create_order

        async def refuse(req):
            from dewako.exchanges.base import InvalidOrder
            raise InvalidOrder("exchange refused")
        pa.create_order = refuse
        stop_out(eng, pa)
        self.assertEqual(exits(db)[0]["status"], "FAILED")
        self.assertEqual((position(db)["status"], position(db)["quantity"]), ("OPEN", 1.0))
        pa.create_order = real
        pa.first = 1.0
        run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")


class NoDuplicateSelling(unittest.TestCase):
    def test_a_second_exit_request_while_one_is_pending_is_refused_and_a_replay_is_a_duplicate(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        from hardening_helpers import decision
        d = decision("manual-1", Side.SELL, 1.0, entry_price=94.0)                        # tries to sell the whole original size again
        r = run(eng.execute(d, bot_id="b1", environment="PAPER", exchange="paper", intent="EXIT"))
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("already being sold", r.detail)
        self.assertEqual(len(sells(db)), 1)
        self.assertEqual(len(exits(db)), 1)

    def test_replaying_the_same_exit_signal_makes_no_second_order_and_no_second_row(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        pa.set_price("BTC/USDT", 94.0)
        from hardening_helpers import decision
        d = decision("exit:1:STOP:1", Side.SELL, 1.0, entry_price=94.0)
        r1 = run(eng.execute(d, bot_id="b1", environment="PAPER", exchange="paper", intent="EXIT"))
        r2 = run(eng.execute(d, bot_id="b1", environment="PAPER", exchange="paper", intent="EXIT"))
        self.assertEqual(r2.outcome, "DUPLICATE")
        self.assertEqual((len(sells(db)), len(exits(db))), (1, 1))
        self.assertEqual(exits(db)[0]["reason"], "STOP")
        self.assertNotEqual(r1.outcome, "BLOCKED")


class Restart(unittest.TestCase):
    def _restart(self, target, pa):
        from hardening_helpers import tmp_db as reopen
        db, _ = reopen(target)
        return db, ExecutionEngine(db, SafetyController(db), {"paper": pa}, quote_guard=TEST_QUOTE_GUARD)

    def test_a_pending_partial_exit_survives_a_restart_and_is_finished_from_the_exchange_report(self):
        db, target, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        cid = sells(db)[0]["client_order_id"]
        pa.advance(cid, 1.0, done=True)                                                   # filled while the process was down
        db2, eng2 = self._restart(target, pa)
        from dewako.execution.positions import PositionStore
        PositionStore(db2).mark_incomplete_for_reconciliation()
        self.assertEqual(position(db2)["status"], "REDUCING")                             # a known partial exit is not quarantined
        run(eng2.recover())
        self.assertEqual((position(db2)["status"], position(db2)["quantity"]), ("CLOSED", 0))
        self.assertEqual(len(sells(db2)), 1)                                              # recovered by reading, never resent
        self.assertEqual(exits(db2)[0]["status"], "COMPLETE")

    def test_a_reducing_position_whose_exit_outcome_is_unknown_is_quarantined_for_reconciliation(self):
        db, target, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        with db.tx() as c:
            c.execute("UPDATE orders SET state='UNKNOWN'")
            from dewako.execution.exit_tracking import sync
            sync(c, sells(db)[0]["id"])
        self.assertEqual((exits(db)[0]["status"], exits(db)[0]["reconciliation_status"]), ("UNKNOWN", "REQUIRED"))
        from dewako.execution.positions import PositionStore
        self.assertEqual(PositionStore(db).mark_incomplete_for_reconciliation(), 1)
        self.assertEqual(position(db)["status"], "RECONCILING")


class Stalled(unittest.TestCase):
    def _late(self, db, secs):
        t = datetime.now(timezone.utc) + timedelta(seconds=secs)
        return lambda: t

    def test_an_exit_with_no_progress_is_reported_opens_an_incident_blocks_entries_and_is_not_resent(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        m = monitor(eng, exit_stall_seconds=60.0)
        m.clock = self._late(db, 120)
        pa.set_price("BTC/USDT", 94.0, as_of=m.clock())
        rep = run(m.run_once())
        self.assertTrue(any("no progress" in w for _, w in rep.unverified), rep.unverified)
        with db.read() as c:
            kinds = [r["kind"] for r in c.execute("SELECT kind FROM incidents WHERE status='OPEN'")]
            stalled = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='EXIT_STALLED'").fetchone()[0]
        self.assertIn("EXIT_PROTECTION_UNVERIFIED", kinds)
        self.assertEqual(stalled, 1)
        self.assertEqual(len(sells(db)), 1)                                               # nothing was cancelled or sent again
        blocked = run(eng.execute(bdec("e2", symbol="ETH/USDT"), bot_id="b2", environment="PAPER", exchange="paper"))
        self.assertEqual(blocked.outcome, "BLOCKED")

    def test_progress_inside_the_limit_is_not_a_stall_and_the_check_can_be_switched_off(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        stop_out(eng, pa)
        m = monitor(eng, exit_stall_seconds=60.0)
        m.clock = self._late(db, 30)
        pa.set_price("BTC/USDT", 94.0, as_of=m.clock())
        self.assertFalse(run(m.run_once()).unverified)
        off = monitor(eng, exit_stall_seconds=None)
        off.clock = self._late(db, 10_000)
        pa.set_price("BTC/USDT", 94.0, as_of=off.clock())
        self.assertFalse(run(off.run_once()).unverified)
        with self.assertRaises(ValueError):
            monitor(eng, exit_stall_seconds=0)

    def test_a_reducing_position_with_no_tracked_exit_is_not_treated_as_protected(self):
        db, _, pa, eng = make(Slow)
        buy(eng)
        with db.tx() as c:
            c.execute("UPDATE positions SET status='REDUCING'")
        rep = run(monitor(eng).run_once())
        self.assertTrue(any("no exit is tracked" in w for _, w in rep.unverified), rep.unverified)

    def test_the_setting_is_read_from_the_environment_and_refuses_nonsense(self):
        from dewako.runtime.application import _stall_seconds
        self.assertEqual(_stall_seconds({}), 120.0)
        self.assertEqual(_stall_seconds({"DEWAKO_EXIT_STALL_SECONDS": "30"}), 30.0)
        self.assertIsNone(_stall_seconds({"DEWAKO_EXIT_STALL_SECONDS": "off"}))
        for bad in ("abc", "0", "-5"):
            with self.assertRaises(ValueError):
                _stall_seconds({"DEWAKO_EXIT_STALL_SECONDS": bad})


if __name__ == "__main__":
    unittest.main()
