"""Stage 3A step 2: the simulated exchange is pessimistic, deterministic and honest about what it cannot fill.

Every modelled effect (spread, slippage, fees, participation cap, latency, rounding, rejections, outages) has a test that fails when the
effect is removed (mutation-checked, see the changelog). One test runs the REAL ExecutionEngine against it. Nothing contacts an exchange.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.clock import VirtualClock  # noqa: E402
from dewako.backtesting.sim_adapter import SimConfig, SimulatedExchange  # noqa: E402
from dewako.core.types import Candle, OrderRequest, OrderStatus, OrderType, Side  # noqa: E402
from dewako.exchanges.base import InsufficientBalance, InvalidOrder, OrderNotFound  # noqa: E402

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
M1 = timedelta(minutes=1)
SYM = "BTC/USDT"


def run(coro):
    return asyncio.run(coro)


def bar(i=0, o=100.0, h=101.0, l=99.0, c=100.0, v=1000.0):   # noqa: E741
    return Candle(t=T0 + M1 * i, o=o, h=h, l=l, c=c, v=v)


def req(cid="c1", side=Side.BUY, qty=1.0):
    return OrderRequest(symbol=SYM, side=side, type=OrderType.MARKET, quantity=qty, client_order_id=cid)


def sim(cfg=None, balance=1_000_000.0, **kw):
    clock = VirtualClock(T0)
    ex = SimulatedExchange(clock, balance=balance, config=cfg or SimConfig(**kw))
    return ex, clock


def at(ex, clock, mid=100.0, i=0, **barkw):
    b = bar(i, **barkw)
    clock.set(T0 + M1 * i)
    return ex.set_market(SYM, mid, T0 + M1 * i, b)


class Quote(unittest.TestCase):
    def test_bid_is_below_mid_ask_above_and_the_spread_widens_with_the_bar_range(self):
        ex, clock = sim()
        at(ex, clock, h=100.2, l=99.8)
        calm = run(ex.get_ticker(SYM))
        at(ex, clock, i=1, h=104.0, l=96.0)
        wild = run(ex.get_ticker(SYM))
        for q in (calm, wild):
            self.assertLess(q["bid"], 100.0)
            self.assertGreater(q["ask"], 100.0)
        self.assertGreater(wild["ask"] - wild["bid"], calm["ask"] - calm["bid"])
        self.assertEqual(calm["timestamp"], T0)                                            # virtual time, never the wall clock
        self.assertEqual(wild["timestamp"], T0 + M1)

    def test_the_minimum_half_spread_applies_to_a_flat_bar(self):
        ex, clock = sim(min_half_spread=0.001)
        at(ex, clock, h=100.0, l=100.0)
        q = run(ex.get_ticker(SYM))
        self.assertAlmostEqual(q["ask"], 100.1)
        self.assertAlmostEqual(q["bid"], 99.9)


class Fills(unittest.TestCase):
    def test_a_buy_pays_more_than_the_ask_and_a_sell_gets_less_than_the_bid(self):
        ex, clock = sim()
        at(ex, clock)
        q = run(ex.get_ticker(SYM))
        b = run(ex.create_order(req("b", Side.BUY, 1.0)))
        self.assertEqual(b.status, OrderStatus.FILLED)
        self.assertGreater(b.price, q["ask"])
        s = run(ex.create_order(req("s", Side.SELL, 1.0)))
        self.assertLess(s.price, q["bid"])

    def test_the_fill_price_follows_the_documented_formula_and_is_recorded(self):
        ex, clock = sim(fee_rate=0.001, min_half_spread=0.0002, spread_range_factor=0.0, slippage_base=0.0003, slippage_range_factor=0.0, impact_k=0.5)
        at(ex, clock, v=1000.0)
        run(ex.create_order(req("b", Side.BUY, 10.0)))
        (f,) = ex.fills
        slip = 0.0003 + 0.5 * (10.0 / 1000.0)
        self.assertAlmostEqual(f.price, 100.0 * 1.0002 * (1 + slip), places=9)
        self.assertAlmostEqual(f.fee, f.price * 10.0 * 0.001, places=9)
        self.assertEqual((f.mid, f.half_spread, f.bar_time), (100.0, 0.0002, T0))
        self.assertAlmostEqual(ex.cash, 1_000_000.0 - f.price * 10.0 - f.fee, places=6)
        self.assertEqual(ex.holdings[SYM], 10.0)

    def test_a_bigger_order_pays_more_per_unit(self):
        ex, clock = sim()
        at(ex, clock, v=100000.0)
        small = run(ex.create_order(req("s", qty=1.0))).price
        big = run(ex.create_order(req("b", qty=2000.0))).price
        self.assertGreater(big, small)

    def test_a_wilder_bar_costs_more_than_a_calm_one(self):
        ex, clock = sim()
        at(ex, clock, h=100.1, l=99.9)
        calm = run(ex.create_order(req("a"))).price
        at(ex, clock, i=1, h=105.0, l=95.0)
        wild = run(ex.create_order(req("b"))).price
        self.assertGreater(wild, calm)

    def test_range_slippage_alone_makes_a_wild_bar_cost_more_by_exactly_the_configured_amount(self):
        cfg = dict(min_half_spread=0.0001, spread_range_factor=0.0, slippage_base=0.0, slippage_range_factor=0.5, impact_k=0.0)
        ex, clock = sim(**cfg)
        at(ex, clock, h=100.0, l=100.0)                                                    # range 0
        calm = run(ex.create_order(req("a"))).price
        at(ex, clock, i=1, h=102.0, l=98.0)                                                # range 4%
        wild = run(ex.create_order(req("b"))).price
        self.assertAlmostEqual(calm, 100.0 * 1.0001, places=9)
        self.assertAlmostEqual(wild, 100.0 * 1.0001 * (1 + 0.5 * 0.04), places=9)
        self.assertAlmostEqual(ex.fills[1].slippage, 0.02, places=12)

    def test_a_gap_through_a_stop_is_filled_at_the_gapped_quote_not_at_the_stop_price(self):
        ex, clock = sim()
        at(ex, clock)
        run(ex.create_order(req("b", Side.BUY, 1.0)))
        at(ex, clock, mid=90.0, i=1, o=90.0, h=91.0, l=89.0, c=90.0)                       # the next bar opens far below a stop at 95
        s = run(ex.create_order(req("s", Side.SELL, 1.0)))
        self.assertLess(s.price, 90.0)
        self.assertNotAlmostEqual(s.price, 95.0, delta=1.0)

    def test_fees_are_charged_on_every_fill_including_partial_ones(self):
        ex, clock = sim(max_participation=0.1, fee_rate=0.002)
        at(ex, clock, v=100.0)
        run(ex.create_order(req("b", qty=25.0)))
        at(ex, clock, i=1, v=100.0)
        self.assertEqual(len(ex.fills), 2)
        self.assertAlmostEqual(sum(f.fee for f in ex.fills), sum(f.price * f.quantity for f in ex.fills) * 0.002, places=9)


class PartialFillsAndVolume(unittest.TestCase):
    def test_an_order_larger_than_the_participation_cap_fills_in_parts_over_later_bars(self):
        ex, clock = sim(max_participation=0.1)
        at(ex, clock, v=100.0)                                                             # cap 10 per bar
        o = run(ex.create_order(req("b", qty=25.0)))
        self.assertEqual((o.status, o.filled), (OrderStatus.PARTIAL, 10.0))
        self.assertEqual([x.client_order_id for x in run(ex.get_open_orders())], ["b"])
        at(ex, clock, mid=100.0, i=0, v=100.0)                                             # a second price point inside the same bar
        self.assertEqual(run(ex.get_order(SYM, client_order_id="b")).filled, 10.0)         # the bar's volume is already used up
        at(ex, clock, i=1, v=100.0)
        self.assertEqual(run(ex.get_order(SYM, client_order_id="b")).filled, 20.0)
        at(ex, clock, i=2, v=100.0)
        done = run(ex.get_order(SYM, client_order_id="b"))
        self.assertEqual((done.status, done.filled), (OrderStatus.FILLED, 25.0))
        self.assertEqual(run(ex.get_open_orders()), [])
        vwap = sum(f.price * f.quantity for f in ex.fills) / sum(f.quantity for f in ex.fills)
        self.assertAlmostEqual(done.price, vwap, places=9)                                 # the average price is volume weighted

    def test_older_orders_are_filled_first_and_share_the_bar_volume(self):
        ex, clock = sim(max_participation=0.1)
        at(ex, clock, v=100.0)
        run(ex.create_order(req("first", qty=8.0)))
        run(ex.create_order(req("second", qty=8.0)))
        self.assertEqual(run(ex.get_order(SYM, client_order_id="first")).filled, 8.0)
        self.assertEqual(run(ex.get_order(SYM, client_order_id="second")).filled, 2.0)     # only the volume that was left

    def test_when_volume_is_short_the_older_waiting_order_gets_it_not_the_newer_one(self):
        ex, clock = sim(max_participation=0.1, latency_seconds=30.0)
        at(ex, clock, v=100.0)
        run(ex.create_order(req("old", qty=8.0)))                                          # both wait for the latency to pass
        run(ex.create_order(req("new", qty=8.0)))
        t = T0 + timedelta(seconds=31)
        clock.set(t)
        ex.set_market(SYM, 100.0, t, bar(0, v=100.0))
        self.assertEqual(run(ex.get_order(SYM, client_order_id="old")).filled, 8.0)
        self.assertEqual(run(ex.get_order(SYM, client_order_id="new")).filled, 2.0)
        self.assertEqual([f.client_order_id for f in ex.fills], ["old", "new"])

    def test_a_bar_with_no_volume_fills_nothing(self):
        ex, clock = sim()
        at(ex, clock, v=0.0)
        o = run(ex.create_order(req("b")))
        self.assertEqual((o.status, o.filled, o.price), (OrderStatus.NEW, 0.0, None))
        self.assertEqual(ex.fills, [])

    def test_cancelling_keeps_what_was_filled_and_stops_the_rest(self):
        ex, clock = sim(max_participation=0.1)
        at(ex, clock, v=100.0)
        run(ex.create_order(req("b", qty=25.0)))
        c = run(ex.cancel_order(SYM, "b"))
        self.assertEqual((c.status, c.filled), (OrderStatus.CANCELLED, 10.0))
        at(ex, clock, i=1, v=100.0)
        self.assertEqual(run(ex.get_order(SYM, client_order_id="b")).filled, 10.0)
        with self.assertRaises(InvalidOrder):
            run(ex.cancel_order(SYM, "b"))
        with self.assertRaises(OrderNotFound):
            run(ex.cancel_order(SYM, "nope"))

    def test_a_partial_buy_stops_when_the_cash_runs_out(self):
        ex, clock = sim(max_participation=0.5, balance=1500.0)
        at(ex, clock, v=100.0)
        run(ex.create_order(req("b", qty=14.0)))                                           # affordable at submit, 7 now
        at(ex, clock, i=1, mid=200.0, v=100.0)                                             # price doubles: cannot pay for the next part
        o = run(ex.get_order(SYM, client_order_id="b"))
        self.assertEqual(o.status, OrderStatus.CANCELLED)
        self.assertLessEqual(o.filled, 7.0)
        self.assertGreaterEqual(ex.cash, 0.0)


class Latency(unittest.TestCase):
    def test_an_order_cannot_fill_before_the_latency_has_passed_and_fills_at_the_later_price(self):
        ex, clock = sim(latency_seconds=30.0)
        at(ex, clock, mid=100.0)
        o = run(ex.create_order(req("b")))
        self.assertEqual((o.status, o.filled), (OrderStatus.NEW, 0.0))
        clock.set(T0 + timedelta(seconds=10))
        self.assertEqual(ex.set_market(SYM, 99.0, T0 + timedelta(seconds=10), bar(0)), [])     # too early
        clock.set(T0 + timedelta(seconds=31))
        (f,) = ex.set_market(SYM, 120.0, T0 + timedelta(seconds=31), bar(0))
        self.assertEqual(f.mid, 120.0)                                                     # the price when it arrived, not when it was sent
        self.assertEqual(run(ex.get_order(SYM, client_order_id="b")).status, OrderStatus.FILLED)


class ExchangeRules(unittest.TestCase):
    def test_quantity_rounds_down_to_the_step_and_small_or_large_orders_are_rejected(self):
        ex, clock = sim(step_size=0.001, min_qty=0.01, max_qty=5.0, min_notional=0.1)
        at(ex, clock)
        o = run(ex.create_order(req("a", qty=0.0129999)))
        self.assertEqual(o.quantity, 0.012)                                                # never rounded up
        for cid, q in (("small", 0.0099), ("big", 5.5)):
            with self.assertRaises(InvalidOrder):
                run(ex.create_order(req(cid, qty=q)))

    def test_minimum_notional_balance_and_short_selling_are_enforced(self):
        ex, clock = sim(min_notional=50.0, balance=500.0)
        at(ex, clock)
        with self.assertRaises(InvalidOrder):
            run(ex.create_order(req("tiny", qty=0.1)))                                     # 10 < 50
        with self.assertRaises(InsufficientBalance):
            run(ex.create_order(req("rich", qty=10.0)))                                    # 1000 > 500
        with self.assertRaises(InvalidOrder):
            run(ex.create_order(req("short", Side.SELL, 1.0)))
        with self.assertRaises(InvalidOrder):
            run(ex.create_order(req("nomarket").model_copy(update={"symbol": "ETH/USDT"})))

    def test_only_market_orders_and_a_repeated_client_id_is_the_same_order(self):
        ex, clock = sim()
        at(ex, clock)
        with self.assertRaises(InvalidOrder):
            run(ex.create_order(req("lim").model_copy(update={"type": OrderType.LIMIT})))
        a = run(ex.create_order(req("same")))
        cash = ex.cash
        b = run(ex.create_order(req("same")))
        self.assertEqual((a.exchange_order_id, b.exchange_order_id, ex.cash, len(ex.fills)), (a.exchange_order_id, a.exchange_order_id, cash, 1))


class Rejections(unittest.TestCase):
    def test_a_maintenance_window_rejects_every_order_and_it_ends(self):
        ex, clock = sim(reject_windows=((T0 + M1 * 5, T0 + M1 * 10),))
        at(ex, clock)
        run(ex.create_order(req("before")))
        clock.set(T0 + M1 * 6)
        with self.assertRaises(InvalidOrder):
            run(ex.create_order(req("during")))
        at(ex, clock, i=10)                                                                # a fresh price point after the window
        self.assertEqual(run(ex.create_order(req("after"))).status, OrderStatus.FILLED)

    def test_seeded_rejection_is_repeatable_and_roughly_at_the_configured_rate(self):
        def outcomes(seed):
            ex, clock = sim(reject_rate=0.3, seed=seed, balance=10**9)
            at(ex, clock, v=10**9)
            out = []
            for i in range(600):
                try:
                    run(ex.create_order(req(f"o{i}", qty=0.1)))
                    out.append(True)
                except InvalidOrder:
                    out.append(False)
            return out
        a, b, other = outcomes(7), outcomes(7), outcomes(8)
        self.assertEqual(a, b)
        self.assertNotEqual(a, other)
        self.assertTrue(0.22 < a.count(False) / 600 < 0.38, a.count(False))
        self.assertEqual(outcomes(7)[:50], a[:50])

    def test_rates_zero_and_one_are_exact(self):
        for rate, expect_rejects in ((0.0, 0), (1.0, 20)):
            ex, clock = sim(reject_rate=rate)
            at(ex, clock)
            bad = 0
            for i in range(20):
                try:
                    run(ex.create_order(req(f"o{i}", qty=0.1)))
                except InvalidOrder:
                    bad += 1
            self.assertEqual(bad, expect_rejects)


class Outages(unittest.TestCase):
    def test_every_call_fails_inside_an_outage_and_works_again_after_it(self):
        ex, clock = sim(outage_windows=((T0 + M1 * 5, T0 + M1 * 8),))
        at(ex, clock)
        run(ex.create_order(req("ok")))
        clock.set(T0 + M1 * 6)
        for call in (lambda: ex.get_ticker(SYM), lambda: ex.get_balance(), lambda: ex.get_markets(), lambda: ex.create_order(req("x")),
                     lambda: ex.get_order(SYM, client_order_id="ok"), lambda: ex.get_open_orders(), lambda: ex.get_positions()):
            with self.assertRaises(ConnectionError):
                run(call())
        self.assertNotIn("x", ex.orders)                                                   # the order sent during the outage was never accepted
        clock.set(T0 + M1 * 8)
        self.assertEqual(run(ex.get_order(SYM, client_order_id="ok")).status, OrderStatus.FILLED)


class Determinism(unittest.TestCase):
    def test_the_same_inputs_give_identical_fills_every_time(self):
        def trace():
            ex, clock = sim(max_participation=0.2, latency_seconds=5.0, reject_rate=0.1, seed=3)
            at(ex, clock, v=500.0)
            for i in range(10):
                try:
                    run(ex.create_order(req(f"o{i}", Side.BUY if i % 2 == 0 else Side.SELL, 3.0)))
                except Exception:
                    pass
                at(ex, clock, mid=100 + i, i=i + 1, v=500.0)
            return [(f.client_order_id, f.quantity, f.price, f.fee) for f in ex.fills], ex.cash
        self.assertEqual(trace(), trace())

    def test_the_virtual_clock_never_goes_backwards_and_refuses_naive_time(self):
        clock = VirtualClock(T0)
        clock.advance(M1)
        with self.assertRaises(ValueError):
            clock.set(T0)
        with self.assertRaises(ValueError):
            clock.advance(-M1)
        with self.assertRaises(ValueError):
            VirtualClock(datetime(2026, 1, 1))
        self.assertEqual(clock(), T0 + M1)


class ConfigIsChecked(unittest.TestCase):
    def test_nonsense_settings_are_refused(self):
        for kw in (dict(fee_rate=-0.1), dict(fee_rate=float("nan")), dict(max_participation=0), dict(max_participation=1.5),
                   dict(latency_seconds=-1), dict(step_size=0), dict(min_notional=float("inf")), dict(reject_rate=2.0),
                   dict(min_qty=2.0, max_qty=1.0), dict(outage_windows=((T0 + M1, T0),)),
                   dict(reject_windows=((datetime(2026, 1, 1), datetime(2026, 1, 2)),))):
            with self.assertRaises(ValueError, msg=str(kw)):
                SimConfig(**kw)

    def test_a_bad_mid_is_refused_and_the_adapter_says_it_is_simulated(self):
        ex, clock = sim()
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ex.set_market(SYM, bad, T0, bar())
        self.assertTrue(ex.SIMULATED)
        self.assertEqual((ex.name, ex.environment), ("sim", "PAPER"))


class WithTheRealEngine(unittest.TestCase):
    def test_the_real_execution_engine_trades_against_it_and_an_outage_leaves_an_unknown_order_not_a_guess(self):
        from hardening_helpers import TEST_QUOTE_GUARD, tmp_db
        from test_stage1_p0_exits import bdec, orders, position

        from dewako.execution.engine import ExecutionEngine
        from dewako.safety.controller import SafetyController
        db, _ = tmp_db()
        ex, clock = sim(outage_windows=((T0 + M1 * 5, T0 + M1 * 8),))
        at(ex, clock, h=100.1, l=99.9)
        eng = ExecutionEngine(db, SafetyController(db), {"paper": ex}, quote_guard=TEST_QUOTE_GUARD, clock=clock)
        r = run(eng.execute(bdec("e1"), bot_id="b1", environment="PAPER", exchange="paper"))
        self.assertEqual(r.outcome, "EXECUTED")
        p = position(db)
        self.assertEqual(p["status"], "OPEN")
        self.assertAlmostEqual(p["average_price"], ex.fills[0].price, places=9)            # the engine booked the simulated fill price
        clock.set(T0 + M1 * 6)
        r2 = run(eng.execute(bdec("e2", symbol="BTC/USDT"), bot_id="b2", environment="PAPER", exchange="paper"))
        self.assertNotEqual(r2.outcome, "EXECUTED")
        self.assertEqual(len([o for o in orders(db) if o["state"] == "FILLED"]), 1)
        self.assertEqual((r2.outcome, len(ex.orders)), ("BLOCKED", 1))                    # fails closed before anything is sent; the exchange saw nothing
        self.assertEqual(len(orders(db)), 1)

    def test_a_lost_order_response_is_reconciled_from_the_exchange_and_never_sold_twice(self):
        from hardening_helpers import TEST_QUOTE_GUARD, tmp_db
        from test_stage1_p0_exits import bdec, orders, position

        from dewako.execution.engine import ExecutionEngine
        from dewako.safety.controller import SafetyController
        db, _ = tmp_db()
        ex, clock = sim(lost_response_windows=((T0 + M1 * 5, T0 + M1 * 8),))
        at(ex, clock, h=100.1, l=99.9)
        eng = ExecutionEngine(db, SafetyController(db), {"paper": ex}, quote_guard=TEST_QUOTE_GUARD, clock=clock)
        clock.set(T0 + M1 * 6)
        ex.set_market(SYM, 100.0, T0 + M1 * 6, bar(6, h=100.1, l=99.9))
        r = run(eng.execute(bdec("e1"), bot_id="b1", environment="PAPER", exchange="paper"))
        self.assertEqual(r.outcome, "EXECUTED")                                             # resolved by asking the exchange, not by resending
        with db.read() as c:
            events = [x["event_type"] for x in c.execute("SELECT event_type FROM order_events ORDER BY id")]
        self.assertEqual(events, ["ORDER_CREATED", "SUBMIT_ATTEMPT", "OUTCOME_UNKNOWN", "RECONCILE_START", "EXCHANGE_FILLED"])
        self.assertEqual(position(db)["status"], "OPEN")
        self.assertEqual((len(orders(db)), len(ex.orders), len(ex.fills)), (1, 1, 1))       # one order, one fill, no resend
        self.assertAlmostEqual(position(db)["average_price"], ex.fills[0].price, places=9)


if __name__ == "__main__":
    unittest.main()
