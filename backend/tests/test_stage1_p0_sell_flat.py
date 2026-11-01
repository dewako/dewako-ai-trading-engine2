"""Stage 1 / P0-2 (SAF-02): a spot SELL with no position must be blocked, and a fill that cannot be applied must never crash recovery.

Reproduced on the unmodified code first: SELL ENTRY while flat raised PositionError out of execute(), left the order SUBMITTED, drove the
paper adapter's holdings negative, and made recover() raise on every run.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import TEST_QUOTE_GUARD, decision, tmp_db  # noqa: E402

from dewako.core.types import Side  # noqa: E402
from dewako.execution.engine import ExecutionEngine  # noqa: E402
from dewako.paper_trading.paper_exchange import PaperAdapter  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make(adapter_cls=PaperAdapter):
    db, _ = tmp_db()
    pa = adapter_cls(balance=100000)
    pa.set_price("BTC/USDT", 100.0)
    pa.environment = "PAPER"
    eng = ExecutionEngine(db, SafetyController(db), {"paper": pa}, quote_guard=TEST_QUOTE_GUARD)
    return db, pa, eng


def sell(eng, sid, qty, intent):
    return run(eng.execute(decision(sid, Side.SELL, qty), bot_id="b1", environment="PAPER", exchange="paper", intent=intent))


def buy(eng, sid="e1", qty=1.0):
    return run(eng.execute(decision(sid, Side.BUY, qty), bot_id="b1", environment="PAPER", exchange="paper"))


def count(db, table):
    with db.read() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class SellWhileFlat(unittest.TestCase):
    def test_sell_entry_with_no_position_is_blocked_and_sends_nothing(self):
        db, pa, eng = make()
        r = sell(eng, "s1", 1.0, "ENTRY")
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertEqual(count(db, "orders"), 0)
        self.assertEqual(pa.holdings.get("BTC/USDT", 0.0), 0.0)         # nothing went to the exchange: no negative holdings
        self.assertEqual(count(db, "positions"), 0)

    def test_sell_exit_with_no_position_is_blocked(self):
        db, pa, eng = make()
        r = sell(eng, "s1", 1.0, "EXIT")
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertEqual(count(db, "orders"), 0)
        self.assertEqual(pa.holdings.get("BTC/USDT", 0.0), 0.0)

    def test_sell_exit_larger_than_the_position_is_blocked(self):
        db, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        r = sell(eng, "s1", 2.0, "EXIT")
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertAlmostEqual(pa.holdings["BTC/USDT"], 1.0)

    def test_valid_exit_still_closes_the_position(self):
        db, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        r = sell(eng, "x1", 1.0, "EXIT")
        self.assertEqual(r.outcome, "EXECUTED")
        with db.read() as c:
            self.assertEqual(c.execute("SELECT status FROM positions").fetchone()[0], "CLOSED")
        self.assertAlmostEqual(pa.holdings["BTC/USDT"], 0.0)

    def test_replaying_the_same_exit_is_a_duplicate_not_a_second_sell(self):
        db, pa, eng = make()
        buy(eng)
        self.assertEqual(sell(eng, "x1", 1.0, "EXIT").outcome, "EXECUTED")
        r = sell(eng, "x1", 1.0, "EXIT")
        self.assertEqual(r.outcome, "DUPLICATE")
        self.assertEqual(count(db, "orders"), 2)

    def test_two_different_exits_cannot_together_exceed_the_position(self):
        """Duplicate exit triggers with different signal ids: the second must not sell what the first already sells."""
        class Hold(PaperAdapter):            # the first exit is accepted but not yet filled
            async def create_order(self, req):
                from dewako.core.types import OrderStatus
                o = await super().create_order(req)
                if req.side is Side.SELL:
                    o = o.model_copy(update={"filled": 0.0, "status": OrderStatus.NEW})
                    self.orders[req.client_order_id] = o
                return o
        db, pa, eng = make(Hold)
        buy(eng)
        first = sell(eng, "x1", 1.0, "EXIT")
        self.assertNotEqual(first.outcome, "BLOCKED", first.detail)
        second = sell(eng, "x2", 1.0, "EXIT")
        self.assertEqual(second.outcome, "BLOCKED", second.detail)


class UnappliableFillDoesNotCrashRecovery(unittest.TestCase):
    def test_fill_for_a_vanished_position_opens_a_critical_incident_and_recover_completes(self):
        class Racy(PaperAdapter):
            """Fills the SELL on the exchange, then loses the response, while the local position disappears (state divergence)."""
            db = None
            async def create_order(self, req):
                o = await super().create_order(req)
                if req.side is Side.SELL:
                    with self.db.tx() as c:
                        c.execute("UPDATE positions SET quantity=0, status='CLOSED'")
                    raise TimeoutError("response lost")
                return o
        db, pa, eng = make(Racy)
        pa.db = db
        buy(eng)
        r = sell(eng, "x1", 1.0, "EXIT")          # must not raise
        self.assertIn(r.outcome, ("UNRESOLVED", "EXECUTED"))
        out = run(eng.recover())                   # must not raise, twice
        out = run(eng.recover())
        with db.read() as c:
            inc = [dict(x) for x in c.execute("SELECT kind, severity, status FROM incidents")]
        self.assertTrue(any(i["severity"] == "CRITICAL" and i["status"] == "OPEN" for i in inc), inc)
        with db.read() as c:
            st = [r[0] for r in c.execute("SELECT state FROM reconciliation_state")]
        self.assertIn("RECONCILIATION_REQUIRED", st)


if __name__ == "__main__":
    unittest.main()
