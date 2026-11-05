"""Stage 3B step 3: regimes are labelled from the past only, and trades are attributed to the regime they entered in."""
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.regimes import (DOWN, HIGH, LOW, NORMAL, RANGE, UNKNOWN, UP, RegimeConfig, by_regime, label_candles,  # noqa: E402
                                        regime_at)
from dewako.backtesting.replay import Trade  # noqa: E402
from dewako.core.types import Candle  # noqa: E402

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)


def mk(i, close, spread=0.1):
    return Candle(t=T0 + M5 * i, o=close, h=close + spread, l=close - spread, c=close, v=1.0)


def line(n, start=100.0, step=0.5, spread=0.1):
    return [mk(i, start + step * i, spread) for i in range(n)]


class Trend(unittest.TestCase):
    def test_a_straight_rise_is_up_a_straight_fall_is_down_a_zigzag_is_range_and_early_candles_are_unknown(self):
        up = label_candles(line(80))
        self.assertEqual({x.trend for x in up[:50]}, {UNKNOWN})
        self.assertEqual({x.trend for x in up[50:]}, {UP})
        self.assertEqual({x.trend for x in label_candles(line(80, step=-0.5))[50:]}, {DOWN})
        zig = label_candles([mk(i, 100 + (1 if i % 2 else -1)) for i in range(80)])
        self.assertEqual({x.trend for x in zig[50:]}, {RANGE})

    def test_the_ratio_threshold_decides_between_trend_and_range(self):
        # up 2, down 1 each cycle: net/path = 1/3 over 50 closes -> just above 0.30 is UP, a stricter threshold makes it RANGE
        c, p = [], 100.0
        for i in range(90):
            p += 2 if i % 3 != 2 else -2
            c.append(mk(i, p))
        self.assertEqual(label_candles(c)[80].trend, UP)
        self.assertEqual(label_candles(c, RegimeConfig(trend_ratio=0.9))[80].trend, RANGE)


class Volatility(unittest.TestCase):
    def test_a_sudden_jump_in_range_is_high_and_a_sudden_calm_is_low_and_the_start_is_unknown(self):
        calm = [mk(i, 100.0, spread=0.1) for i in range(150)]
        wild = [mk(150 + i, 100.0, spread=1.0) for i in range(30)]
        lab = label_candles(calm + wild)
        self.assertEqual(lab[10].volatility, UNKNOWN)
        self.assertEqual(lab[149].volatility, NORMAL)                                      # equal to its past: neither high nor low
        self.assertEqual(lab[150 + 14].volatility, HIGH)
        back = label_candles([mk(i, 100.0, 1.0) for i in range(150)] + [mk(150 + i, 100.0, 0.1) for i in range(30)])
        self.assertEqual(back[150 + 14 + 14].volatility, LOW)


class Boundaries(unittest.TestCase):
    def test_a_ratio_exactly_at_the_threshold_is_a_trend(self):
        c = [mk(i, v, spread=0.1) for i, v in enumerate([100.0, 102.0, 101.0, 102.0])]     # net 2, path 4: ratio exactly 0.5
        cfg = RegimeConfig(trend_window=3, trend_ratio=0.5)
        self.assertEqual(label_candles(c, cfg)[3].trend, UP)
        self.assertEqual(label_candles(c, RegimeConfig(trend_window=3, trend_ratio=0.51))[3].trend, RANGE)

    def test_volatility_needs_exactly_the_minimum_history_and_the_rank_edges_belong_to_high_and_low(self):
        cfg = RegimeConfig(trend_window=2, atr_window=1, vol_history=10, vol_min_history=3)
        def run(cur):                                                                      # ATR% of the first three: 1 %, 2 %, 3 %
            return label_candles([mk(i, 100.0, spread=sp) for i, sp in enumerate([0.5, 1.0, 1.5, cur])], cfg)
        lab = run(0.1)
        self.assertEqual((lab[2].volatility, lab[3].volatility), (UNKNOWN, LOW))            # 3 earlier values are enough at index 3, 2 are not
        self.assertEqual(run(1.25)[3].volatility, HIGH)                                     # 2.5 %: above 2 of 3 -> rank exactly 2/3 -> HIGH
        self.assertEqual(run(0.75)[3].volatility, LOW)                                      # 1.5 %: above 1 of 3 -> rank exactly 1/3 -> LOW
        self.assertEqual(run(1.0)[3].volatility, NORMAL)                                    # 2.0 %: equals one, above one, below one -> middle

    def test_gaps_between_candles_count_as_range(self):
        calm = [mk(i, 100.0, spread=0.1) for i in range(120)]
        gappy = [mk(120 + i, 100.0 + (10 if i % 2 else 0), spread=0.01) for i in range(30)]   # tiny candles, big jumps between them
        self.assertEqual(label_candles(calm + gappy)[120 + 20].volatility, HIGH)


class NoLookAhead(unittest.TestCase):
    def test_changing_the_future_never_changes_an_earlier_label(self):
        rng = random.Random(4)
        a = [mk(i, 100 + rng.uniform(-3, 3), rng.uniform(0.05, 0.5)) for i in range(400)]
        k = 250
        b = a[:k] + [mk(i, 5000.0 + 40 * i, 50.0) for i in range(k, 400)]                 # a violent, one-directional future
        la, lb = label_candles(a), label_candles(b)
        self.assertEqual(la[:k], lb[:k])                                                   # any peek at candle k would flip candle k-1
        self.assertNotEqual(la[k:], lb[k:])


class Attribution(unittest.TestCase):
    def test_a_trade_belongs_to_the_last_candle_that_was_closed_when_it_entered(self):
        lab = label_candles(line(80))
        at_close = T0 + M5 * 70                                                            # candle 69 closes exactly now
        self.assertEqual(regime_at(lab, "5m", at_close).time, T0 + M5 * 69)
        self.assertEqual(regime_at(lab, "5m", at_close - timedelta(seconds=1)).time, T0 + M5 * 68)
        self.assertIsNone(regime_at(lab, "5m", T0))

    def test_every_trade_lands_in_exactly_one_group_per_dimension_and_unknowns_are_kept(self):
        lab = label_candles(line(80))
        def t(i, pnl, entry_bar):
            return Trade(T0 + M5 * entry_bar, T0 + M5 * (entry_bar + 3), 1.0, 100.0, 100.0 + pnl, 0.0, pnl, "X")
        trades = [t(0, 5.0, 10), t(1, -2.0, 60), t(2, 3.0, 70), t(3, 1.0, 1)]            # 10, 1: unknown trend; 60, 70: UP
        out = by_regime(trades, lab, "5m", 1000.0)
        self.assertEqual(sum(m.trades for m in out["trend"].values()), 4)
        self.assertEqual(sum(m.trades for m in out["volatility"].values()), 4)
        self.assertEqual(out["trend"][UP].trades, 2)
        self.assertEqual(out["trend"][UP].net_pnl, 1.0)
        self.assertEqual(out["trend"][UNKNOWN].trades, 2)
        self.assertEqual(out["trend"][UNKNOWN].net_pnl, 6.0)


class Config(unittest.TestCase):
    def test_bad_settings_are_refused(self):
        for kw in ({"trend_window": 1}, {"atr_window": 0}, {"low_rank": 0.8, "high_rank": 0.5}, {"trend_ratio": 0.0},
                   {"vol_history": 10, "vol_min_history": 50}):
            with self.assertRaises(ValueError, msg=str(kw)):
                RegimeConfig(**kw)


if __name__ == "__main__":
    unittest.main()
