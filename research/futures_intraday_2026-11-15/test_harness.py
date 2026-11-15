"""Checks of the intraday harness on small hand-made series (python -m pytest test_harness.py)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402

STEP = H.TF_MS["5m"]


def arr(bars):
    a = np.array(bars, dtype=float)
    return np.arange(len(a), dtype=np.int64) * STEP, a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def rule(n, long_at=(), short_at=(), atr=1.0, **kw):
    L, S = np.zeros(n, bool), np.zeros(n, bool)
    L[list(long_at)] = True
    S[list(short_at)] = True
    return H.Rule(L, S, np.full(n, atr), **kw)


FLAT = (100, 100.5, 99.5, 100)


class Indicators(unittest.TestCase):
    def test_wilder_average_atr_and_rsi(self):
        self.assertTrue(np.allclose(H.rma(np.array([1.0, 2, 3, 4]), 2)[1:], [1.5, 2.25, 3.125]))
        h, l, c = np.array([10.0, 12, 11]), np.array([9.0, 10, 9]), np.array([9.5, 11, 10])
        self.assertTrue(np.allclose(H.atr(h, l, c, 2)[1:], [(1 + 2.5) / 2, ((1 + 2.5) / 2 + 2) / 2]))
        r = H.rsi(np.array([1.0, 2, 3, 4, 5]), 2)
        self.assertEqual(r[-1], 100.0)                       # only gains
        r = H.rsi(np.array([10.0, 11, 10, 11, 10]), 2)       # gains 1,0,1,0 losses 0,1,0,1
        # averages after bar 2: up .5 dn .5; bar 3: up .75 dn .25 -> 75; bar 4: up .375 dn .625 -> 37.5
        self.assertTrue(np.allclose(r[2:], [50.0, 75.0, 37.5]))

    def test_rolling_values_use_only_the_past_and_resampling_is_right(self):
        x = np.array([1.0, 5, 2, 9, 3])
        self.assertTrue(np.allclose(H.prev(H.roll(x, 2, np.max))[2:], [5, 5, 9]))    # bar i sees bars i-2..i-1
        e = H.ema(np.array([1.0, 2, 3]), 2)
        self.assertTrue(np.isnan(e[0]) and np.isclose(e[1], 1 + 2 / 3) and np.isclose(e[2], 2 / 3 * 3 + 1 / 3 * (1 + 2 / 3)))

    def test_breakout_rule_does_not_look_at_the_current_bar_high(self):
        n = 300
        bars = [FLAT] * n
        bars[295] = (100, 110, 99.5, 100.2)                  # spike high, close inside the range
        bars[296] = (100, 100.7, 99.6, 100.6)                # closes above the old 100.5 but the spike is now in the window
        t, o, h, l, c = arr(bars)
        r = H.make_rule("BRK", "5m", o, h, l, c)
        self.assertFalse(r.long[295])
        self.assertFalse(r.long[296])
        bars[295] = FLAT
        t, o, h, l, c = arr(bars)
        self.assertTrue(H.make_rule("BRK", "5m", o, h, l, c).long[296])


    def test_reversion_rule_needs_both_the_band_and_the_rsi(self):
        closes = [float(i) for i in range(1, 41)] + [15.0, 5.0]
        bars = [(x, x + 0.5, x - 0.5, x) for x in closes]
        t, o, h, l, c = arr(bars)
        r = H.make_rule("MR", "5m", o, h, l, c)
        # bar 40: below the lower band (17.6) but RSI is 34 -> no trade; bar 41: RSI 27 and below the band -> long
        self.assertAlmostEqual(H.rsi(c, 14)[40], 100 - 100 / (1 + (13 / 14) / (25 / 14)), places=6)
        self.assertEqual((bool(r.long[40]), bool(r.long[41])), (False, True))
        self.assertFalse(r.short.any())
        self.assertEqual(r.time_stop, 48)
        bars = [(60 - x, 60 - x + 0.5, 60 - x - 0.5, 60 - x) for x in closes]       # mirror image: the short side
        t, o, h, l, c = arr(bars)
        r = H.make_rule("MR", "5m", o, h, l, c)
        self.assertEqual((bool(r.short[40]), bool(r.short[41])), (False, True))

    def test_pullback_rule_needs_a_touch_of_the_ema20_in_an_uptrend(self):
        bars = [(100 + 0.1 * i - 0.05, 100 + 0.1 * i + 0.1, 100 + 0.1 * i - 0.1, 100 + 0.1 * i) for i in range(262)]
        t, o, h, l, c = arr(bars)
        e20 = H.ema(c, 20)
        self.assertFalse(H.make_rule("PB", "5m", o, h, l, c).long.any())      # a smooth trend never touches its EMA20
        o260, c260 = bars[260][0], bars[260][3]
        bars[260] = (o260, c260 + 0.1, e20[260] - 0.05, c260)                  # dips to the EMA20, closes back up
        t, o, h, l, c = arr(bars)
        r = H.make_rule("PB", "5m", o, h, l, c)
        self.assertEqual(np.flatnonzero(r.long).tolist(), [260])
        self.assertEqual((r.stop_mult, r.target_r), (1.5, 2.0))
        down = [(200 - x[0], 200 - x[2], 200 - x[1], 200 - x[3]) for x in bars]     # mirror image: a short
        t, o, h, l, c = arr(down)
        self.assertEqual(np.flatnonzero(H.make_rule("PB", "5m", o, h, l, c).short).tolist(), [260])
        e = H.ema(c, 20)[261]                                                  # a bounce in a downtrend is not a long
        down[261] = (e - 0.3, e + 0.2, e - 0.4, e + 0.1)
        t, o, h, l, c = arr(down)
        r = H.make_rule("PB", "5m", o, h, l, c)
        self.assertTrue(l[261] <= H.ema(c, 20)[261] < c[261] and c[261] > o[261])
        self.assertFalse(r.long.any())


class Simulation(unittest.TestCase):
    def test_long_next_open_entry_fixed_stop_and_costs(self):
        bars = [FLAT, FLAT, (101, 102, 100.5, 101.5), (101.5, 101.6, 98, 99), FLAT]
        t, o, h, l, c = arr(bars)
        r = rule(5, long_at=[1], atr=1.0, stop_mult=2.0)
        r.atr[2:] = 7.0                                       # the entry bar's own ATR is not known at its open: must not be used
        tr, curve = H.simulate(t, o, h, l, c, r, "5m", fee=0.0, slip=0.0, cash=1000.0)
        # signal at bar 1 -> long at bar 2 open 101; stop 99; qty = min(1000*0.01/2, 1000/101) = 5; bar 3 low 98 -> out at 99
        self.assertEqual(len(tr), 1)
        fund = 5 * 101.5 * H.FUND_8H * 5 / 480                # one bar held to its close before the stop
        self.assertAlmostEqual(tr[0].pnl, 5 * (99 - 101) - fund, places=9)
        self.assertAlmostEqual(tr[0].risked, 10.0)
        self.assertEqual((tr[0].entry_ms, tr[0].exit_ms), (2 * STEP, 3 * STEP))
        self.assertAlmostEqual(curve[-1][1], 1000 + tr[0].pnl)
        cost, _ = H.simulate(t, o, h, l, c, r, "5m", fee=0.001, slip=0.0005, cash=1000.0)
        e, x = 101 * 1.0005, 99 * 0.9995                      # buy higher, sell lower; stop is measured from the filled entry
        x = (e - 2) * 0.9995
        self.assertAlmostEqual(cost[0].pnl, 5 * (x - e) - 5 * e * 0.001 - 5 * x * 0.001 - 5 * 101.5 * H.FUND_8H * 5 / 480, places=9)

    def test_position_value_is_capped_at_1x(self):
        bars = [FLAT, FLAT, (100, 100.2, 99.9, 100), (100, 100.1, 99.7, 99.8), FLAT]
        t, o, h, l, c = arr(bars)
        tr, _ = H.simulate(t, o, h, l, c, rule(5, long_at=[1], atr=0.1, stop_mult=2.0), "5m", 0.0, 0.0, 1000.0)
        # 1% risk over a 0.2 stop would be 50 units (5000 value): capped at 1000/100 = 10 units
        self.assertAlmostEqual(tr[0].risked, 10 * 0.2)

    def test_short_gap_through_the_stop_fills_at_the_open_and_shorts_pay_no_funding(self):
        bars = [FLAT, FLAT, (100, 100.5, 99, 99.5), (104, 105, 103, 104), FLAT]
        t, o, h, l, c = arr(bars)
        tr, _ = H.simulate(t, o, h, l, c, rule(5, short_at=[1], atr=1.0, stop_mult=2.0), "5m", 0.0, 0.0, 1000.0)
        self.assertEqual(tr[0].side, -1)
        self.assertAlmostEqual(tr[0].pnl, -5 * (104 - 100), places=9)

    def test_target_and_stop_in_one_bar_counts_as_the_stop_and_target_alone_pays_2r(self):
        both = [FLAT, FLAT, (100, 100.2, 99.9, 100), (100, 103.5, 98.4, 101), FLAT]
        t, o, h, l, c = arr(both)
        r = rule(5, long_at=[1], atr=1.0, stop_mult=1.5, target_r=2.0)
        tr, _ = H.simulate(t, o, h, l, c, r, "5m", 0.0, 0.0, 1000.0)
        q = 1000 * 0.01 / 1.5
        f1 = q * 100 * H.FUND_8H * 5 / 480
        self.assertAlmostEqual(tr[0].pnl, -q * 1.5 - f1, places=9)
        only = [FLAT, FLAT, (100, 100.2, 99.9, 100), (100, 103.5, 99.5, 103), FLAT]
        t, o, h, l, c = arr(only)
        tr, _ = H.simulate(t, o, h, l, c, r, "5m", 0.0, 0.0, 1000.0)
        self.assertAlmostEqual(tr[0].pnl, q * 3.0 - f1, places=9)
        self.assertAlmostEqual(tr[0].r, 2.0, places=3)

    def test_stop_on_the_entry_bar_and_trailing_stop_never_loosens(self):
        bars = [FLAT, FLAT, (100, 100.2, 97, 99), FLAT]
        t, o, h, l, c = arr(bars)
        tr, _ = H.simulate(t, o, h, l, c, rule(4, long_at=[1], atr=1.0, stop_mult=2.0), "5m", 0.0, 0.0, 1000.0)
        self.assertEqual(tr[0].exit_ms, 2 * STEP)
        self.assertAlmostEqual(tr[0].pnl, -5 * 2.0, places=9)
        # trail: entry 100 (stop 98); close 104 -> stop 102; ATR jumps to 3 (would be 104 - 6 = 98): stays 102; low 101.5 -> out at 102
        bars = [FLAT, FLAT, (100, 104.5, 99.5, 104), (104, 104.5, 103, 103.5), (103.5, 103.6, 101.5, 102.5), FLAT]
        t, o, h, l, c = arr(bars)
        r = rule(6, long_at=[1], atr=1.0, stop_mult=2.0, trail=True)
        r.atr[3:] = 3.0
        tr, _ = H.simulate(t, o, h, l, c, r, "5m", 0.0, 0.0, 1000.0)
        fund = 5 * (104 + 103.5) * H.FUND_8H * 5 / 480
        self.assertEqual(tr[0].exit_ms, 4 * STEP)
        self.assertAlmostEqual(tr[0].pnl, 5 * (102 - 100) - fund, places=9)

    def test_short_trailing_stop_never_loosens_either(self):
        # short at 100 (stop 102); close 96 -> stop 98; ATR jumps to 3 (would be 96 + 6 = 102): stays 98; high 98.5 -> out at 98
        bars = [FLAT, FLAT, (100, 100.5, 95.5, 96), (96, 97, 95.5, 96.5), (96.5, 98.5, 96.4, 97.5), FLAT]
        t, o, h, l, c = arr(bars)
        r = rule(6, short_at=[1], atr=1.0, stop_mult=2.0, trail=True)
        r.atr[3:] = 3.0
        tr, _ = H.simulate(t, o, h, l, c, r, "5m", 0.0, 0.0, 1000.0)
        self.assertEqual(tr[0].exit_ms, 4 * STEP)
        self.assertAlmostEqual(tr[0].pnl, 5 * (100 - 98), places=9)

    def test_squeeze_rule_needs_the_squeeze_on_the_bar_before_the_breakout(self):
        bars = [(100, 100.4, 99.8, 100 + 0.1 * (i % 2)) for i in range(60)] + [(100, 103, 99.9, 103)]
        t, o, h, l, c = arr(bars)
        r = H.make_rule("SQZ", "5m", o, h, l, c)
        self.assertTrue(r.long[60])                           # quiet before, then a close far outside the band
        self.assertFalse(r.long[:60].any() or r.short.any())
        bars[59], bars[60] = (100, 103, 99.9, 103), (103, 110, 102.9, 110)     # the bands were already blown open on bar 59
        t, o, h, l, c = arr(bars)
        r = H.make_rule("SQZ", "5m", o, h, l, c)
        self.assertTrue(r.long[59])
        self.assertFalse(r.long[60])

    def test_signal_exit_and_time_stop_leave_at_the_next_open_and_the_books_add_up(self):
        bars = [FLAT, FLAT, (100, 100.4, 99.8, 100.2), (100.2, 100.6, 100, 100.5), (100.7, 100.9, 100.6, 100.8), FLAT, FLAT]
        t, o, h, l, c = arr(bars)
        r = rule(7, long_at=[1], atr=1.0, stop_mult=2.0)
        r.exit_long = np.zeros(7, bool)
        r.exit_long[3] = True
        tr, curve = H.simulate(t, o, h, l, c, r, "5m", 0.001, 0.0005, 1000.0)
        self.assertEqual(tr[0].exit_ms, 4 * STEP)             # decided at bar 3's close, filled at bar 4's open 100.7
        r2 = rule(7, long_at=[1], atr=1.0, stop_mult=2.0, time_stop=2)
        tr2, curve2 = H.simulate(t, o, h, l, c, r2, "5m", 0.001, 0.0005, 1000.0)
        self.assertEqual(tr2[0].exit_ms, 4 * STEP)            # held bars 2 and 3
        for trades, cv in ((tr, curve), (tr2, curve2)):
            self.assertAlmostEqual(1000 + sum(x.pnl for x in trades), cv[-1][1], places=9)

    def test_no_entry_while_a_position_is_open_and_drawdown(self):
        bars = [FLAT] * 8
        t, o, h, l, c = arr(bars)
        tr, _ = H.simulate(t, o, h, l, c, rule(8, long_at=[1, 2, 3], atr=1.0, stop_mult=2.0, time_stop=3), "5m", 0.0, 0.0, 1000.0)
        self.assertEqual([x.entry_ms // STEP for x in tr], [2])
        self.assertAlmostEqual(H.max_drawdown([100, 120, 90, 130]), 0.25)


if __name__ == "__main__":
    unittest.main()
