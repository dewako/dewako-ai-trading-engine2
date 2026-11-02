"""Stage 1 / P0-1 (SAF-01): stops and take-profits must be persisted with the position, enforced by an exit monitor, and survive a process kill.

Evidence before the fix (unmodified code): a filled BUY created a position with stop_loss=NULL and take_profit=NULL; nothing ever closed it.
Paper trading only: these tests use the in-memory paper adapter. Nothing here contacts an exchange.
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

from hardening_helpers import PAPER_ASK, TEST_QUOTE_GUARD, decision, tmp_db  # noqa: E402

from dewako.core.types import OrderStatus, Side  # noqa: E402
from dewako.execution.engine import ExecutionEngine  # noqa: E402
from dewako.paper_trading.paper_exchange import PaperAdapter  # noqa: E402
from dewako.risk.engine import RiskEngine  # noqa: E402
from dewako.safety.controller import SafetyController  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def bdec(sid, stop=95.0, tp=110.0, qty=1.0, symbol="BTC/USDT"):
    d = decision(sid, Side.BUY, qty, symbol=symbol, entry_price=PAPER_ASK)
    s = d.signal.model_copy(update={"stop_price": stop, "take_profit_price": tp})
    return d.model_copy(update={"signal": s})


def make(adapter_cls=PaperAdapter, db_target=None):
    db, target = tmp_db(db_target)
    pa = adapter_cls(balance=100000)
    pa.set_price("BTC/USDT", 100.0)
    pa.set_price("ETH/USDT", 100.0)
    pa.environment = "PAPER"
    eng = ExecutionEngine(db, SafetyController(db), {"paper": pa}, quote_guard=TEST_QUOTE_GUARD)
    return db, target, pa, eng


def monitor(eng, **kw):
    from dewako.execution.exit_monitor import ExitMonitor
    return ExitMonitor(eng, RiskEngine(), **kw)


def buy(eng, sid="e1", bot="b1", **kw):
    return run(eng.execute(bdec(sid, **kw), bot_id=bot, environment="PAPER", exchange="paper"))


def position(db, bot="b1"):
    with db.read() as c:
        r = c.execute("SELECT * FROM positions WHERE bot_id=? ORDER BY id DESC", (bot,)).fetchone()
        return dict(r) if r else None


def orders(db, side=None):
    with db.read() as c:
        q = "SELECT * FROM orders" + (" WHERE side=?" if side else "")
        return [dict(r) for r in c.execute(q, (side,) if side else ())]


class ProtectionIsPersisted(unittest.TestCase):
    def test_stop_and_take_profit_are_stored_on_the_position_at_first_fill(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng).outcome, "EXECUTED")
        p = position(db)
        self.assertEqual((p["stop_loss"], p["take_profit"]), (95.0, 110.0))

    def test_an_entry_without_a_stop_is_blocked_and_creates_nothing(self):
        db, _, pa, eng = make()
        r = buy(eng, stop=None)
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("stop", r.detail.lower())
        self.assertEqual(orders(db), [])

    def test_a_stop_on_the_wrong_side_of_entry_is_blocked(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng, stop=105.0).outcome, "BLOCKED")

    def test_the_position_store_refuses_to_create_an_unprotected_long(self):
        db, _, pa, eng = make()
        from dewako.execution.positions import PositionError
        with self.assertRaises(PositionError):
            with db.tx() as c:
                eng.positions.open_or_add(c, bot_id="b", exchange="paper", environment="PAPER", symbol="BTC/USDT", side="BUY",
                                          quantity=1.0, price=100.0)

    def test_partial_entry_fill_gives_a_correctly_sized_protected_position(self):
        class Partial(PaperAdapter):
            async def create_order(self, req):
                o = await super().create_order(req)
                if req.side is Side.BUY:
                    o = o.model_copy(update={"filled": req.quantity * 0.4, "status": OrderStatus.PARTIAL})
                    self.orders[req.client_order_id] = o
                return o
        db, _, pa, eng = make(Partial)
        buy(eng)
        p = position(db)
        self.assertAlmostEqual(p["quantity"], 0.4)
        self.assertEqual((p["stop_loss"], p["take_profit"]), (95.0, 110.0))


class MonitorEnforcesExits(unittest.TestCase):
    def test_stop_closes_the_position(self):
        db, _, pa, eng = make()
        buy(eng)
        pa.set_price("BTC/USDT", 94.0)
        rep = run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual(len(rep.exits), 1)

    def test_take_profit_closes_the_position(self):
        db, _, pa, eng = make()
        buy(eng)
        pa.set_price("BTC/USDT", 111.0)
        run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")

    def test_nothing_happens_between_stop_and_target(self):
        db, _, pa, eng = make()
        buy(eng)
        pa.set_price("BTC/USDT", 101.0)
        rep = run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual(rep.exits, [])

    def test_gap_through_the_stop_fills_at_the_market_not_at_the_stop(self):
        db, _, pa, eng = make()
        buy(eng)
        pa.set_price("BTC/USDT", 80.0)                    # gaps 20% below the 95 stop
        run(monitor(eng).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")
        fill = orders(db, "SELL")[0]["avg_fill_price"]
        self.assertLess(fill, 81.0)                       # executed near the gapped market, NOT at 95
        self.assertGreater(position(db)["realized_pnl"], -21.0)
        self.assertLess(position(db)["realized_pnl"], -19.0)

    def test_max_holding_time_closes_a_stale_position(self):
        db, _, pa, eng = make()
        buy(eng)
        with db.tx() as c:                                 # the position was opened two hours ago
            c.execute("UPDATE positions SET opened_at='2020-01-01T00:00:00+00:00'")
        pa.set_price("BTC/USDT", 101.0)                    # between stop and target: only the time stop can fire
        rep = run(monitor(eng, max_hold_seconds=3600).run_once())
        self.assertEqual(position(db)["status"], "CLOSED")
        self.assertEqual(rep.exits[0][1], "MAX_HOLD")

    def test_duplicate_triggers_create_one_exit_not_two(self):
        class Hold(PaperAdapter):                         # exits are accepted but not filled yet
            async def create_order(self, req):
                o = await super().create_order(req)
                if req.side is Side.SELL:
                    o = o.model_copy(update={"filled": 0.0, "status": OrderStatus.NEW})
                    self.orders[req.client_order_id] = o
                return o
        db, _, pa, eng = make(Hold)
        buy(eng)
        pa.set_price("BTC/USDT", 94.0)
        m = monitor(eng)
        run(m.run_once())
        run(m.run_once())
        run(monitor(eng).run_once())                      # a second monitor instance too
        self.assertEqual(len(orders(db, "SELL")), 1)

    def test_partial_exit_leaves_the_exact_residual_and_keeps_its_protection(self):
        class HalfExit(PaperAdapter):
            async def create_order(self, req):
                o = await super().create_order(req)
                if req.side is Side.SELL:
                    o = o.model_copy(update={"filled": req.quantity * 0.5, "status": OrderStatus.PARTIAL})
                    self.orders[req.client_order_id] = o
                return o
        db, _, pa, eng = make(HalfExit)
        buy(eng)
        pa.set_price("BTC/USDT", 94.0)
        run(monitor(eng).run_once())
        p = position(db)
        self.assertEqual(p["status"], "OPEN")
        self.assertAlmostEqual(p["quantity"], 0.5)
        self.assertEqual((p["stop_loss"], p["take_profit"]), (95.0, 110.0))


class UnverifiedProtectionBlocksEntries(unittest.TestCase):
    def test_missing_quote_opens_an_incident_blocks_new_entries_and_clears_after_clean_cycles(self):
        db, _, pa, eng = make()
        buy(eng)
        real = pa.get_ticker

        async def down(symbol):
            raise ConnectionError("feed down")
        pa.get_ticker = down
        m = monitor(eng)
        rep = run(m.run_once())
        self.assertEqual(len(rep.unverified), 1)
        with db.read() as c:
            inc = [dict(r) for r in c.execute("SELECT kind, status FROM incidents")]
        self.assertTrue(any(i["kind"] == "EXIT_PROTECTION_UNVERIFIED" and i["status"] == "OPEN" for i in inc), inc)
        blocked = buy(eng, sid="e2", bot="b2", symbol="ETH/USDT") if False else run(
            eng.execute(bdec("e2", symbol="ETH/USDT"), bot_id="b2", environment="PAPER", exchange="paper"))
        pa.get_ticker = real
        self.assertEqual(blocked.outcome, "BLOCKED")
        self.assertIn("protect", blocked.detail.lower())
        # one clean cycle is not enough; two consecutive verified cycles are
        run(m.run_once())
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0], 1)
        run(m.run_once())
        with db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0], 0)
        ok = run(eng.execute(bdec("e3", symbol="ETH/USDT"), bot_id="b2", environment="PAPER", exchange="paper"))
        self.assertEqual(ok.outcome, "EXECUTED", ok.detail)

    def test_a_stale_monitor_blocks_entries_through_the_entry_guard(self):
        db, _, pa, eng = make()
        eng.entry_guard = lambda: (False, "exit monitor is not healthy")
        r = buy(eng)
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("exit monitor", r.detail)


class UnprotectableFillIsEscalatedNotInvented(unittest.TestCase):
    def test_a_buy_fill_with_no_recorded_stop_creates_no_position_and_raises_a_critical_incident(self):
        """E.g. an order created before this release, or a crash that left no signal levels. recover() must not crash and must not invent
        an unprotected position."""
        from dewako.execution.state_machine import OrderState as S
        db, _, pa, eng = make()
        created = eng.orders.create(bot_id="b1", signal_id="legacy", intent="ENTRY", environment="PAPER", exchange="paper", symbol="BTC/USDT",
                                    side="BUY", order_type="MARKET", quantity=1.0, price=100, correlation_id="c1")
        eng.orders.transition(created.order_id, S.CREATED, S.SUBMITTING, "SUBMIT_ATTEMPT")
        from dewako.core.types import OrderRequest, OrderType
        run(pa.create_order(OrderRequest(symbol="BTC/USDT", side=Side.BUY, type=OrderType.MARKET, quantity=1.0,
                                         client_order_id=created.client_order_id)))
        out = run(eng.recover())                          # must not raise
        self.assertEqual(out[0].state, "FILLED")
        self.assertIsNone(position(db))
        with db.read() as c:
            inc = [dict(r) for r in c.execute("SELECT kind, severity, status FROM incidents")]
        self.assertTrue(any(i["kind"] == "FILL_NOT_APPLICABLE" and i["severity"] == "CRITICAL" for i in inc), inc)


class SurvivesProcessKill(unittest.TestCase):
    def test_stop_and_take_profit_survive_sigkill_and_a_fresh_engine_enforces_them(self):
        db, target, pa, eng = make()
        child = textwrap.dedent(f"""
            import asyncio, os, sys, threading
            sys.path[:0] = [{os.path.dirname(os.path.dirname(__file__))!r}, {os.path.dirname(__file__)!r}]
            from test_stage1_p0_exits import make, buy
            db, target, pa, eng = make(db_target={target!r})
            r = buy(eng)
            assert r.outcome == "EXECUTED", r
            print("OPENED", flush=True)
            threading.Event().wait()
        """)
        env = dict(os.environ)
        p = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, env=env)
        output = []
        opened = threading.Event()

        def capture_output():
            for line in p.stdout:
                output.append(line)
                if line.strip() == "OPENED":
                    opened.set()

        reader = threading.Thread(target=capture_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + 120
        try:
            while not opened.wait(0.05) and p.poll() is None and time.monotonic() < deadline:
                pass
            self.assertTrue(opened.is_set(), "".join(output)[-500:])
            self.assertIsNone(p.poll(), "child exited before forced termination")
            p.kill()
            returncode = p.wait(timeout=10)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            reader.join(timeout=10)
        if os.name == "nt":
            self.assertEqual(returncode, 1, "".join(output)[-500:])
        else:
            self.assertEqual(returncode, -_signal.SIGKILL, "".join(output)[-500:])
        self.assertIn("OPENED", "".join(output))
        # a brand-new engine and monitor on the same database (the "restart")
        db2, _ = tmp_db(target)
        pa2 = PaperAdapter(balance=100000)
        pa2.set_price("BTC/USDT", 94.0)
        pa2.environment = "PAPER"
        eng2 = ExecutionEngine(db2, SafetyController(db2), {"paper": pa2}, quote_guard=TEST_QUOTE_GUARD)
        with db2.read() as c:
            row = dict(c.execute("SELECT * FROM positions").fetchone())
        self.assertEqual((row["stop_loss"], row["take_profit"], row["status"]), (95.0, 110.0, "OPEN"))
        pa2.holdings["BTC/USDT"] = 1.0                    # the fresh paper adapter models the same holding
        run(monitor(eng2).run_once())
        with db2.read() as c:
            self.assertEqual(c.execute("SELECT status FROM positions").fetchone()[0], "CLOSED")


if __name__ == "__main__":
    unittest.main()
