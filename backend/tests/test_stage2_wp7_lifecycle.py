"""Stage 2 WP7: lifecycle and crash recovery with REAL process termination (SIGKILL on Linux, TerminateProcess on Windows), no graceful shutdown.

* restart with an open position: stop-loss AND take-profit are enforced by a brand-new process;
* restart with a pending partial exit: the position stays REDUCING, the exchange's report finishes it, nothing is sold twice;
* repeated kill/restart cycles change nothing and sell nothing;
* the market-data feed failing, the database failing mid-cycle and a critical worker failing are reported, fail closed (no order from bad
  data), and recover without manual repair.
Paper trading only. Nothing here contacts an exchange.
"""
import asyncio
import os
import signal as _signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import TEST_QUOTE_GUARD, tmp_db  # noqa: E402
from test_stage1_p0_exits import bdec, buy, make, monitor, orders, position, run  # noqa: E402
from test_stage2_wp3_partial_exits import Slow, exits, sells, stop_out  # noqa: E402

from dewako.core.types import OrderStatus  # noqa: E402
from dewako.execution.engine import ExecutionEngine  # noqa: E402
from dewako.paper_trading.paper_exchange import PaperAdapter  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)


def killed_child(body: str, timeout: float = 120.0) -> str:
    """Run `body` in a separate process (after importing the helpers), wait for it to print READY, then kill it abruptly.
    Returns what it printed. Fails the test if it exits early or never gets ready."""
    code = f"import sys\nsys.path[:0] = [{BACKEND!r}, {HERE!r}]\n" + textwrap.dedent(body) + "\nprint('READY', flush=True)\nimport threading\nthreading.Event().wait()\n"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    out, ready = [], threading.Event()

    def pump():
        for line in p.stdout:
            out.append(line)
            if line.strip() == "READY":
                ready.set()
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + timeout
        while not ready.wait(0.05) and p.poll() is None and time.monotonic() < deadline:
            pass
        assert ready.is_set(), "child never became ready:\n" + "".join(out)[-800:]
        assert p.poll() is None, "child exited before it was killed"
        p.kill()
        rc = p.wait(timeout=15)
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=15)
        t.join(timeout=10)
    expected = 1 if os.name == "nt" else -_signal.SIGKILL
    assert rc == expected, f"child was not killed abruptly (rc={rc})\n" + "".join(out)[-500:]
    return "".join(out)


def restarted(target, price=100.0, holdings=1.0, adapter_cls=PaperAdapter):
    """A brand-new engine on the same database: the 'restart'. The fresh adapter models the exchange still holding the position."""
    db, _ = tmp_db(target)
    pa = adapter_cls(balance=100000)
    pa.set_price("BTC/USDT", price)
    pa.environment = "PAPER"
    pa.holdings["BTC/USDT"] = holdings
    return db, pa, ExecutionEngine(db, SafetyController(db), {"paper": pa}, quote_guard=TEST_QUOTE_GUARD)


class KilledProcessThenRestart(unittest.TestCase):
    def test_take_profit_is_enforced_by_a_fresh_process_after_a_kill(self):
        _, target, _, _ = make()
        killed_child(f"""
            from test_stage1_p0_exits import make, buy
            db, target, pa, eng = make(db_target={target!r})
            assert buy(eng).outcome == "EXECUTED"
        """)
        db2, pa2, eng2 = restarted(target, price=111.0)
        rep = run(monitor(eng2).run_once())
        self.assertEqual([e[1] for e in rep.exits], ["TAKE_PROFIT"])
        self.assertEqual(position(db2)["status"], "CLOSED")
        self.assertEqual(len(orders(db2, "SELL")), 1)

    def test_a_pending_partial_exit_survives_a_kill_and_the_exchange_report_finishes_it_without_a_second_sale(self):
        _, target, _, _ = make()
        killed_child(f"""
            from test_stage2_wp3_partial_exits import Slow, stop_out
            from test_stage1_p0_exits import make, buy
            db, target, pa, eng = make(Slow, db_target={target!r})
            assert buy(eng).outcome == "EXECUTED"
            stop_out(eng, pa)
        """)
        db2, pa2, eng2 = restarted(target, price=94.0, holdings=0.5, adapter_cls=Slow)
        self.assertEqual((position(db2)["status"], position(db2)["quantity"]), ("REDUCING", 0.5))
        self.assertEqual(exits(db2)[0]["status"], "PARTIAL")
        o = sells(db2)[0]
        from dewako.core.types import Order, OrderType, Side
        pa2.orders[o["client_order_id"]] = Order(exchange_order_id=o["exchange_order_id"], client_order_id=o["client_order_id"], symbol="BTC/USDT",
                                                 side=Side.SELL, type=OrderType.MARKET, quantity=1.0, filled=1.0, price=94.0, status=OrderStatus.FILLED)
        from dewako.execution.positions import PositionStore
        PositionStore(db2).mark_incomplete_for_reconciliation()
        self.assertEqual(position(db2)["status"], "REDUCING")                            # startup recovery keeps a known partial exit
        run(eng2.recover())
        self.assertEqual((position(db2)["status"], position(db2)["quantity"]), ("CLOSED", 0))
        self.assertEqual(len(sells(db2)), 1)
        self.assertEqual(exits(db2)[0]["status"], "COMPLETE")

    def test_the_rest_of_a_partly_sold_position_is_still_stopped_after_a_kill_when_the_exit_order_is_cancelled(self):
        _, target, _, _ = make()
        killed_child(f"""
            from test_stage2_wp3_partial_exits import Slow, stop_out
            from test_stage1_p0_exits import make, buy
            db, target, pa, eng = make(Slow, db_target={target!r})
            assert buy(eng).outcome == "EXECUTED"
            stop_out(eng, pa)
        """)
        db2, pa2, eng2 = restarted(target, price=94.0, holdings=0.5, adapter_cls=Slow)
        o = sells(db2)[0]
        from dewako.core.types import Order, OrderType, Side
        pa2.orders[o["client_order_id"]] = Order(exchange_order_id=o["exchange_order_id"], client_order_id=o["client_order_id"], symbol="BTC/USDT",
                                                 side=Side.SELL, type=OrderType.MARKET, quantity=1.0, filled=0.5, price=94.0, status=OrderStatus.CANCELLED)
        run(eng2.recover())
        self.assertEqual((position(db2)["status"], position(db2)["quantity"]), ("OPEN", 0.5))
        pa2.first = 1.0
        run(monitor(eng2).run_once())
        self.assertEqual(position(db2)["status"], "CLOSED")
        self.assertEqual([round(s["quantity"], 9) for s in sells(db2)], [1.0, 0.5])      # the second sale is only the remainder

    def test_repeated_kill_restart_cycles_change_nothing_and_sell_nothing(self):
        _, target, _, _ = make()
        killed_child(f"""
            from test_stage1_p0_exits import make, buy
            db, target, pa, eng = make(db_target={target!r})
            assert buy(eng).outcome == "EXECUTED"
        """)
        for _ in range(3):
            killed_child(f"""
                from test_stage2_wp7_lifecycle import restarted
                from test_stage1_p0_exits import monitor, run, position, orders
                db, pa, eng = restarted({target!r}, price=101.0)
                rep = run(monitor(eng).run_once())
                assert rep.exits == [] and rep.unverified == [], rep
                assert position(db)["status"] == "OPEN" and position(db)["quantity"] == 1.0
                assert len(orders(db)) == 1
            """)
        db, pa, eng = restarted(target, price=94.0)
        run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual((len(orders(db, "BUY")), len(orders(db, "SELL"))), (1, 1))


class FailureHandling(unittest.TestCase):
    def test_a_market_data_failure_is_reported_blocks_entries_sells_nothing_and_recovers_by_itself(self):
        db, _, pa, eng = make()
        buy(eng)
        real = pa.get_ticker

        async def down(symbol):
            raise ConnectionError("feed down")
        pa.get_ticker = down
        m = monitor(eng)
        rep = run(m.run_once())
        self.assertEqual((len(rep.unverified), rep.exits), (1, []))
        self.assertEqual(len(orders(db, "SELL")), 0)                                       # bad data never produces an order
        self.assertEqual(run(eng.execute(bdec("e2", symbol="ETH/USDT"), bot_id="b2", environment="PAPER", exchange="paper")).outcome, "BLOCKED")
        pa.get_ticker = real
        pa.set_price("BTC/USDT", 94.0)
        rep = run(m.run_once())
        self.assertEqual([e[1] for e in rep.exits], ["STOP"])                              # the stop still fires as soon as data is back
        self.assertEqual(position(db)["status"], "CLOSED")

    def test_a_database_failure_in_the_middle_of_a_cycle_sends_nothing_and_the_next_cycle_recovers(self):
        db, _, pa, eng = make()
        buy(eng)
        pa.set_price("BTC/USDT", 94.0)
        real = db.tx

        def broken(*a, **k):
            raise OSError("database unavailable")
        db.tx = broken
        try:
            with self.assertRaises(Exception):
                run(monitor(eng).run_once())
        finally:
            db.tx = real
        self.assertEqual(len(orders(db, "SELL")), 0)
        self.assertEqual(position(db)["status"], "OPEN")
        run(monitor(eng).run_once())
        self.assertEqual((position(db)["status"], len(orders(db, "SELL"))), ("CLOSED", 1))

    def test_a_failing_critical_worker_makes_the_system_unhealthy_and_one_good_cycle_restores_it(self):
        from dewako.runtime.workers import WorkerSupervisor
        db, _, pa, eng = make()
        buy(eng)
        sup = WorkerSupervisor(db, SafetyController(db))
        m = monitor(eng)
        sup.add("exit_monitor", m.worker_tick, 5, critical=True)
        w = sup.workers[0]
        real = pa.get_ticker

        async def down(symbol):
            raise ConnectionError("feed down")
        pa.get_ticker = down
        self.assertFalse(asyncio.run(sup.run_once(w)))
        self.assertFalse(sup.healthy()[0])
        pa.get_ticker = real
        self.assertTrue(asyncio.run(sup.run_once(w)))
        self.assertTrue(sup.healthy()[0])


if __name__ == "__main__":
    unittest.main()
