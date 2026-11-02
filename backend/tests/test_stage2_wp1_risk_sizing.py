"""Stage 2 WP1: entry sizing is finished by the Risk Engine from the EXECUTABLE price.

* the arithmetic lives in `dewako.risk.sizing` and is exposed by RiskEngine.size_for_executable_entry (audited, no I/O);
* the quantity is never above the approved one and its loss at the stop is never above the approved loss, for any executable price;
* the last gate (RiskEngine.final_entry_problem) re-checks the quantity that is really going out, after exchange rounding;
* anything missing, non-finite or inconsistent refuses the entry; nothing is guessed.

The grid test is the evidence used to reassess the 95 % sizing headroom (see docs/PHASE5_2_STAGE2_CHANGELOG.md).
Paper trading only. Nothing here contacts an exchange.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from hardening_helpers import PAPER_ASK, decision  # noqa: E402
from test_stage1_p0_exits import bdec, buy, make, orders, position, run  # noqa: E402

from dewako.core.types import Side  # noqa: E402
from dewako.execution.validation import validate_order  # noqa: E402
from dewako.risk.engine import RiskEngine  # noqa: E402
from dewako.risk.sizing import SizingError, executable_entry_quantity, loss_at_stop  # noqa: E402


def dec(qty=10.0, ref=100.0, stop=95.0):
    d = decision("s1", Side.BUY, qty, entry_price=ref)
    return d.model_copy(update={"signal": d.signal.model_copy(update={"stop_price": stop})})


class Arithmetic(unittest.TestCase):
    def test_reference_price_versus_executable_price(self):
        f = lambda ex: executable_entry_quantity(10.0, reference_price=100.0, executable_price=ex, stop_price=95.0)   # noqa: E731
        self.assertEqual(f(100.0), 10.0)                       # equal: unchanged
        self.assertEqual(f(99.0), 10.0)                        # cheaper ask: never sized UP
        self.assertAlmostEqual(f(101.0), 10.0 * 5 / 6)         # dearer ask: scaled by (ref-stop)/(ask-stop)
        self.assertEqual(f(100.0 * (1 + 1e-12)), 10.0)         # float noise is not a reduction

    def test_a_stop_at_or_above_the_executable_price_is_refused(self):
        for ex in (95.0, 94.0):
            with self.assertRaises(SizingError):
                executable_entry_quantity(10.0, reference_price=100.0, executable_price=ex, stop_price=95.0)
        with self.assertRaises(SizingError):                   # stop above the reference too
            executable_entry_quantity(10.0, reference_price=94.0, executable_price=101.0, stop_price=95.0)

    def test_invalid_inputs_are_refused(self):
        good = dict(reference_price=100.0, executable_price=101.0, stop_price=95.0)
        for bad in (0, -1, None, float("nan"), float("inf"), True, "10"):
            with self.assertRaises(SizingError):
                executable_entry_quantity(bad, **good)
        for key in good:
            for bad in (0, -1, None, float("nan"), float("inf")):
                with self.assertRaises(SizingError):
                    executable_entry_quantity(10.0, **{**good, key: bad})


class InvariantsOverAGrid(unittest.TestCase):
    """Deterministic grid: reference prices, ask premiums (widening spread / slippage) and stop distances."""
    REFS = (0.5, 100.0, 2500.0, 65000.0)
    PREMIUMS = [i * 0.0005 for i in range(0, 41)]               # 0 .. 2 % above the reference
    STOP_DISTANCES = (0.001, 0.005, 0.02, 0.05, 0.2)

    def test_loss_at_the_stop_and_notional_never_exceed_what_was_approved(self):
        n = 0
        for ref in self.REFS:
            for dist in self.STOP_DISTANCES:
                stop = ref * (1 - dist)
                approved = 7.0
                approved_loss = loss_at_stop(approved, ref, stop)
                previous = approved
                for prem in self.PREMIUMS:
                    ask = ref * (1 + prem)
                    if stop >= ask:
                        continue
                    q = executable_entry_quantity(approved, reference_price=ref, executable_price=ask, stop_price=stop)
                    self.assertLessEqual(q, approved)                                             # never sized up
                    self.assertLessEqual(q, previous + 1e-12)                                     # a wider spread never gives a larger order
                    self.assertLessEqual(loss_at_stop(q, ask, stop), approved_loss * (1 + 1e-9))  # approved risk holds at the executable price
                    # Headroom evidence: the notional the backstop sees (quantity x executable price) is never above the notional the Risk
                    # Engine sized from the reference price, so the position cap cannot be breached by the ask premium alone.
                    self.assertLessEqual(q * ask, approved * ref * (1 + 1e-9))
                    previous = q
                    n += 1
        self.assertGreater(n, 500)


class RiskEngineMethods(unittest.TestCase):
    def test_it_returns_the_scaled_decision_and_audits_it(self):
        r = RiskEngine()
        out = r.size_for_executable_entry(dec(10.0), executable_price=101.0)
        self.assertTrue(out.approved)
        self.assertAlmostEqual(out.quantity, 10.0 * 5 / 6)
        ev = [e for e in r.audit if e["event"] == "executable_sizing"][-1]
        self.assertEqual((ev["approved"], ev["approved_quantity"], ev["executable_price"]), (True, 10.0, 101.0))

    def test_an_unapproved_decision_is_returned_unchanged(self):
        d = decision("x", Side.BUY, 5.0, approved=False)
        self.assertIs(RiskEngine().size_for_executable_entry(d, executable_price=101.0), d)

    def test_a_stop_at_or_above_the_executable_price_gives_a_refused_decision_with_reasons(self):
        r = RiskEngine()
        out = r.size_for_executable_entry(dec(10.0, stop=101.5), executable_price=101.0)
        self.assertFalse(out.approved)
        self.assertEqual(out.quantity, 0.0)
        self.assertTrue(out.reasons and "stop" in out.reasons[0])
        self.assertFalse([e for e in r.audit if e["event"] == "executable_sizing"][-1]["approved"])

    def test_a_missing_stop_gives_a_refused_decision_not_an_exception(self):
        d = dec(10.0)
        d = d.model_copy(update={"signal": d.signal.model_copy(update={"stop_price": None})})
        out = RiskEngine().size_for_executable_entry(d, executable_price=101.0)
        self.assertFalse(out.approved)

    def test_the_final_gate(self):
        r = RiskEngine()
        d = dec(10.0)                                           # approved loss = 10 x (100 - 95) = 50
        self.assertIsNone(r.final_entry_problem(d, quantity=10.0, executable_price=100.0))
        self.assertIsNone(r.final_entry_problem(d, quantity=8.0, executable_price=101.0))      # 8 x 6 = 48 <= 50
        self.assertIn("exceeds the approved quantity", r.final_entry_problem(d, quantity=10.5, executable_price=100.0))
        self.assertIn("approved risk", r.final_entry_problem(d, quantity=9.0, executable_price=101.0))     # 9 x 6 = 54 > 50
        for bad in (0, -1, None, float("nan")):
            self.assertIsNotNone(r.final_entry_problem(d, quantity=bad, executable_price=100.0))
        self.assertIsNotNone(r.final_entry_problem(d, quantity=5.0, executable_price=float("nan")))


class QuantityRulesAtTheExchange(unittest.TestCase):
    M = [{"symbol": "BTC/USDT", "step_size": 0.001, "min_qty": 0.01, "max_qty": 5.0, "min_notional": 10.0, "enabled": True}]
    B = {"USDT": 1_000_000.0}

    def v(self, q, price=100.0):
        return validate_order(self.M, self.B, symbol="BTC/USDT", side="BUY", quantity=q, price=price)

    def test_rounding_goes_down_never_up(self):
        r = self.v(0.0129999, price=1000.0)
        self.assertFalse(r.problems)
        self.assertEqual(r.quantity, 0.012)
        d = dec(0.0129999, ref=1000.0, stop=950.0)
        self.assertIsNone(RiskEngine().final_entry_problem(d, quantity=r.quantity, executable_price=1000.0))

    def test_minimum_and_maximum_order_sizes(self):
        self.assertTrue(any("below the exchange minimum" in p for p in self.v(0.009).problems))
        self.assertTrue(any("above the exchange maximum" in p for p in self.v(5.5).problems))
        self.assertTrue(any("rounds down to zero" in p for p in self.v(0.0004).problems))
        self.assertTrue(any("notional" in p for p in self.v(0.02, price=100.0).problems))      # 2.0 < min notional 10


class ThroughTheExecutionEngine(unittest.TestCase):
    def test_the_engine_sizes_through_the_risk_engine_and_the_loss_stays_inside_the_approved_risk(self):
        db, _, pa, eng = make()
        pa.set_price("BTC/USDT", 100.3 / (1 + pa.slippage))
        self.assertEqual(buy(eng, qty=10.0).outcome, "EXECUTED")
        p = position(db)
        self.assertLess(p["quantity"], 10.0)
        self.assertLessEqual(p["quantity"] * (p["average_price"] - 95.0), 10.0 * (PAPER_ASK - 95.0) + 1e-6)
        self.assertTrue([e for e in eng.risk_engine.audit if e["event"] == "executable_sizing" and e["approved"]])

    def test_a_stop_above_the_executable_price_blocks_the_entry_and_creates_nothing(self):
        db, _, pa, eng = make()
        r = run(eng.execute(bdec("e9", stop=100.2), bot_id="b1", environment="PAPER", exchange="paper"))
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("stop", r.detail)
        self.assertEqual(orders(db), [])

    def test_the_last_gate_stops_an_oversized_quantity_even_if_sizing_misbehaves(self):
        class Broken(RiskEngine):
            def size_for_executable_entry(self, decision, *, executable_price):         # a sizing bug: returns MORE than was approved
                return decision.model_copy(update={"quantity": decision.quantity * 2})
        db, _, pa, eng = make()
        eng.risk_engine = Broken()
        r = buy(eng, qty=1.0)
        self.assertEqual(r.outcome, "BLOCKED")
        self.assertIn("exceeds the approved quantity", r.detail)
        self.assertEqual(orders(db), [])

    def test_exits_are_not_resized_by_the_entry_sizing(self):
        db, _, pa, eng = make()
        self.assertEqual(buy(eng, qty=1.0).outcome, "EXECUTED")
        self.assertFalse(math.isnan(position(db)["quantity"]))
        sizing_events_before = len([e for e in eng.risk_engine.audit if e["event"] == "executable_sizing"])
        from test_stage1_p0_exits import monitor
        pa.set_price("BTC/USDT", 94.0)
        run(monitor(eng).run_once())
        self.assertEqual(len([e for e in eng.risk_engine.audit if e["event"] == "executable_sizing"]), sizing_events_before)


if __name__ == "__main__":
    unittest.main()
