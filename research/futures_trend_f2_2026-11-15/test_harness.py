"""Checks of the F2 harness on small hand-made series (run: python -m pytest test_harness.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

D = H.DAY


def flat(n, px=100.0, rng=1.0, t0=0):
    return [H.Bar(t0 + i * D, px, px + rng, px - rng, px) for i in range(n)]


class Signals(unittest.TestCase):
    def test_breakout_uses_only_the_bars_before_it(self):
        bars = flat(10)
        bars.append(H.Bar(10 * D, 100, 130, 99, 100.5))      # a huge high today, close inside the old range: no signal
        self.assertEqual(H.signal(bars, 10, 5), "")
        bars.append(H.Bar(11 * D, 100, 131, 100, 130.5))     # closes above the previous 5 highs (max 130)
        self.assertEqual(H.signal(bars, 11, 5), "LONG")
        bars.append(H.Bar(12 * D, 100, 101, 90, 95))         # closes below the previous 5 lows (min 99)
        self.assertEqual(H.signal(bars, 12, 5), "SHORT")
        self.assertEqual(H.signal(bars, 3, 5), "")            # not enough history

    def test_wilder_atr(self):
        a = H.atr_wilder(flat(5, rng=1.0), 3)                 # true range 2 every day
        self.assertEqual(a[:2], [None, None])
        self.assertAlmostEqual(a[2], 2.0)
        self.assertAlmostEqual(a[4], 2.0)


def series(moves, n_flat=8):
    """n_flat quiet days at 100 (range +/-1), then bars given as (o, h, l, c)."""
    bars = flat(n_flat)
    for j, (o, h, l, c) in enumerate(moves):
        bars.append(H.Bar((n_flat + j) * D, o, h, l, c))
    return bars


class Trades(unittest.TestCase):
    KW = dict(n=5, atr_n=3, k=3.0, fee=0.0, slip=0.0)

    def test_long_enters_next_open_and_stops_at_the_trailing_stop(self):
        # day 8 closes at 103 (> 101): long at day 9 open 103. ATR at the signal: TRs 2,2,(103.5-99=4.5) -> (2+2+4.5)/3 = 2.8333
        bars = series([(100, 103.5, 99, 103), (103, 106, 102.5, 105), (105, 111, 104, 110), (110, 110.5, 90, 91)])
        r = H.run({"X": bars}, **self.KW)
        self.assertEqual(len(r["trades"]), 1)
        tr = r["trades"][0]
        self.assertEqual((tr.side, tr.entry_ms, tr.exit_ms, tr.liquidated), ("LONG", 9 * D, 11 * D, False))
        atr0 = 8.5 / 3
        qty = int(10_000 * 0.01 / (3 * atr0) / 0.001 + 1e-9) * 0.001       # 11.764 -> capped? cap = 10000/3/103 = 32.3
        atr9 = (atr0 * 2 + 3.5) / 3                                           # day 9: TR 3.5
        atr10 = (atr9 * 2 + 7) / 3                                            # day 10: TR 7 (111 - 104)
        stop = max(103 - 3 * atr0, 105 - 3 * atr9, 110 - 3 * atr10)
        funding = qty * (105 + 110) * H.FUND_DAY                              # a long pays at the close of days 9 and 10
        self.assertAlmostEqual(tr.pnl, qty * (stop - 103) - funding, places=6)   # day 11 touches the stop: filled at the stop
        self.assertAlmostEqual(tr.risked, qty * 3 * atr0, places=6)

    def test_short_side_and_a_gap_through_the_stop_fills_at_the_open(self):
        bars = series([(100, 101, 96.5, 97), (97, 97.5, 94, 95), (120, 121, 119, 120)])
        r = H.run({"X": bars}, **self.KW)
        tr = r["trades"][0]
        self.assertEqual((tr.side, tr.entry_ms, tr.exit_ms), ("SHORT", 9 * D, 10 * D))
        qty = int(10_000 * 0.01 / (3 * (8.5 / 3)) / 0.001 + 1e-9) * 0.001
        self.assertAlmostEqual(tr.pnl, -qty * (120 - 97), places=6)           # filled at the open 120, far past the stop

    def test_the_stop_never_loosens_and_one_side_can_be_switched_off(self):
        bars = series([(100, 103.5, 99, 103), (103, 106, 102.5, 105), (105, 105.5, 100, 101), (101, 101.5, 95, 97)])
        r = H.run({"X": bars}, **self.KW)
        atr0 = 8.5 / 3
        atr9 = (atr0 * 2 + 3.5) / 3
        stop9 = max(103 - 3 * atr0, 105 - 3 * atr9)             # after day 9
        tr = r["trades"][0]
        self.assertEqual(tr.exit_ms, 11 * D)                    # day 10 low 100 stays above the stop; day 11 touches it
        qty = int(10_000 * 0.01 / (3 * atr0) / 0.001 + 1e-9) * 0.001
        funding = qty * (105 + 101) * H.FUND_DAY
        self.assertAlmostEqual(tr.pnl, qty * (stop9 - 103) - funding, places=6)   # day 10's lower close did not lower the stop
        self.assertLess(stop9, 100)
        self.assertGreater(stop9, 95)
        self.assertEqual(H.run({"X": bars}, sides=("SHORT",), **self.KW)["trades"], [])

    def test_the_short_stop_never_loosens_either(self):
        bars = series([(100, 101, 96.5, 97), (97, 97.5, 94, 95), (95, 100, 94.5, 99), (99, 105, 98.5, 104)])
        tr = H.run({"X": bars}, **self.KW)["trades"][0]
        atr0 = 8.5 / 3
        stop9 = min(97 + 3 * atr0, 95 + 3 * (atr0 * 2 + 3.5) / 3)          # 104.1667 after day 9; day 10's wider ATR must not raise it
        qty = int(10_000 * 0.01 / (3 * atr0) / 0.001 + 1e-9) * 0.001
        self.assertEqual((tr.side, tr.exit_ms), ("SHORT", 11 * D))
        self.assertAlmostEqual(tr.pnl, -qty * (stop9 - 97), places=6)       # shorts receive no funding in the verdict setting

    def test_costs_and_long_funding_reduce_the_result_and_leverage_scales_the_size(self):
        bars = series([(100, 103.5, 99, 103), (103, 106, 102.5, 105), (105, 111, 104, 110), (110, 110.5, 90, 91)])
        free = H.run({"X": bars}, **self.KW)["trades"][0]
        cost = H.run({"X": bars}, n=5, atr_n=3, k=3.0)["trades"][0]
        self.assertLess(cost.pnl, free.pnl)
        x3 = H.run({"X": bars}, leverage=3, **self.KW)["trades"][0]
        self.assertAlmostEqual(x3.risked / free.risked, 3.0, places=2)

    def test_drawdown_and_buy_and_hold(self):
        self.assertAlmostEqual(H.max_drawdown([(0, 100), (1, 120), (2, 90), (3, 130)]), 0.25)
        bh = H.buy_and_hold({"A": [H.Bar(0, 10, 10, 10, 10), H.Bar(D, 10, 20, 10, 20)], "B": [H.Bar(D, 5, 5, 5, 5)]}, 1000)
        self.assertEqual(bh, [(0, 1000.0), (D, 1500.0)])       # A doubles (500 -> 1000); B starts on day 1 at its open


if __name__ == "__main__":
    unittest.main()
