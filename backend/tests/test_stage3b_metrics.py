"""Stage 3B step 1: performance metrics, checked against numbers worked out by hand."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.metrics import compute_metrics, drawdown  # noqa: E402
from dewako.backtesting.replay import Trade  # noqa: E402
from dewako.backtesting.sim_adapter import SimFill  # noqa: E402

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
H = timedelta(hours=1)


def trade(i, pnl, hours=2):
    return Trade(entry_time=T0 + H * (i * 10), exit_time=T0 + H * (i * 10 + hours), quantity=1.0, entry_price=100.0,
                 exit_price=100.0 + pnl, fees=0.0, pnl=pnl, exit_reason="X")


def curve(values, start_hour=1):
    return [(T0 + H * (start_hour + k), v) for k, v in enumerate(values)]


def fill(side, q, price, mid, fee, hour):
    return SimFill("c", "BTC/USDT", side, q, price, fee, T0 + H * hour, mid, 0.0, 0.0, T0 + H * hour)


def metrics(pnls, equity=None, fills=(), initial=1000.0, **kw):
    ts = [trade(i, p) for i, p in enumerate(pnls)]
    return compute_metrics(ts, equity or curve([initial] * 10), list(fills), initial, **kw)


class TradeStatistics(unittest.TestCase):
    def test_counts_rates_profit_factor_expectancy_and_payoff(self):
        m = metrics([10.0, -5.0, 20.0, -5.0, 0.0])
        self.assertEqual((m.trades, m.wins, m.losses, m.breakeven), (5, 2, 2, 1))
        self.assertAlmostEqual(m.win_rate, 0.4)
        self.assertEqual((m.gross_profit, m.gross_loss, m.net_pnl), (30.0, 10.0, 20.0))
        self.assertAlmostEqual(m.profit_factor, 3.0)
        self.assertAlmostEqual(m.expectancy, 4.0)
        self.assertEqual((m.avg_win, m.avg_loss), (15.0, 5.0))
        self.assertAlmostEqual(m.payoff_ratio, 3.0)
        self.assertEqual((m.best_trade, m.worst_trade), (20.0, -5.0))

    def test_undefined_ratios_are_none_not_infinity_or_zero(self):
        m = metrics([5.0, 7.0])
        self.assertIsNone(m.profit_factor)
        self.assertIsNone(m.payoff_ratio)
        self.assertIsNone(m.avg_loss)
        m = metrics([-5.0, -7.0])
        self.assertEqual((m.profit_factor, m.payoff_ratio, m.avg_win), (0.0, None, None))   # all losers: PF is a real 0, payoff is undefined
        m = metrics([])
        self.assertEqual((m.trades, m.win_rate, m.expectancy, m.best_trade, m.avg_holding_seconds), (0, None, None, None, None))

    def test_consecutive_streaks_and_a_zero_trade_breaks_both(self):
        m = metrics([1, 1, 1, -1, -1, 0, -1, 1, 1])
        self.assertEqual((m.max_consecutive_wins, m.max_consecutive_losses), (3, 2))
        self.assertEqual(metrics([1, 1, 0, 1]).max_consecutive_wins, 2)                    # the zero ends the winning run

    def test_holding_time(self):
        ts = [trade(0, 1.0, hours=1), trade(1, 1.0, hours=3), trade(2, 1.0, hours=8)]
        m = compute_metrics(ts, curve([1000.0] * 10), [], 1000.0)
        self.assertEqual((m.avg_holding_seconds, m.median_holding_seconds, m.max_holding_seconds), (4 * 3600.0, 3 * 3600.0, 8 * 3600.0))


class DrawdownAndReturn(unittest.TestCase):
    def test_max_drawdown_is_peak_to_trough_in_money_and_percent_of_the_peak(self):
        dd, pct, _ = drawdown(curve([1000, 1100, 990, 1050, 880, 1200]), 1000.0)
        self.assertEqual(dd, 220.0)                                                        # 1100 -> 880
        self.assertAlmostEqual(pct, 0.2)

    def test_the_initial_cash_is_the_first_peak(self):
        dd, pct, _ = drawdown(curve([900, 950, 1000]), 1000.0)
        self.assertEqual((dd, round(pct, 6)), (100.0, 0.1))

    def test_longest_time_under_water_counts_only_below_a_peak_and_includes_the_open_end(self):
        eq = curve([1000, 1000, 900, 950, 1000, 1000, 800, 900])                           # under water: h3-h5 (2h) then h7-h9 (still)
        _, _, longest = drawdown(eq, 1000.0)
        self.assertEqual(longest, 3 * 3600.0)
        eq2 = curve([1000, 1000, 1000, 1000])                                              # flat at the peak is not a drawdown
        self.assertEqual(drawdown(eq2, 1000.0), (0.0, 0.0, 0.0))

    def test_total_return_includes_an_open_position_and_says_so(self):
        m = metrics([], equity=curve([1000, 950, 900]), open_at_end=True)
        self.assertAlmostEqual(m.total_return_pct, -0.10)
        self.assertEqual(m.final_equity, 900)
        self.assertTrue(any("still open" in w for w in m.warnings))


class CostsAndExposure(unittest.TestCase):
    def test_fees_execution_cost_and_pnl_before_costs(self):
        fills = [fill("BUY", 2.0, 100.10, 100.0, 0.2, 2), fill("SELL", 2.0, 109.90, 110.0, 0.22, 5)]
        m = metrics([14.5], fills=fills)
        self.assertAlmostEqual(m.fees, 0.42)
        self.assertAlmostEqual(m.execution_cost, 2 * 0.10 + 2 * 0.10)
        self.assertAlmostEqual(m.pnl_before_costs, 14.5 + 0.42 + 0.4)

    def test_exposure_is_the_share_of_time_a_position_was_open_including_one_still_open(self):
        eq = curve([1000.0] * 10, start_hour=0)                                            # hours 0..9, period 9 h
        fills = [fill("BUY", 1, 100, 100, 0, 1), fill("SELL", 1, 100, 100, 0, 4), fill("BUY", 1, 100, 100, 0, 6)]
        m = compute_metrics([], eq, fills, 1000.0)
        self.assertAlmostEqual(m.exposure_pct, (3 + 3) / 9)                                # 1->4, then 6->end(9)
        self.assertEqual(compute_metrics([], eq, [], 1000.0).exposure_pct, 0.0)

    def test_trades_per_day(self):
        eq = curve([1000.0] * 49, start_hour=0)                                            # 48 h
        m = compute_metrics([trade(0, 1.0), trade(1, 1.0)], eq, [], 1000.0)
        self.assertAlmostEqual(m.trades_per_day, 1.0)


class Warnings(unittest.TestCase):
    def test_too_few_trades_none_no_losers_and_one_trade_dominating_are_all_flagged(self):
        self.assertTrue(any("Only 3" in w for w in metrics([1, 2, 3]).warnings))
        self.assertTrue(any("No closed trades" in w for w in metrics([]).warnings))
        self.assertTrue(any("No losing trade" in w for w in metrics([1, 2, 3]).warnings))
        self.assertTrue(any("half of the gross profit" in w for w in metrics([100, 1, 1, -1]).warnings))
        clean = metrics([2, 3, -1, 2, 3, -1] * 6)
        self.assertEqual(clean.warnings, ())

    def test_an_empty_equity_curve_is_refused(self):
        with self.assertRaises(ValueError):
            compute_metrics([], [], [], 1000.0)


if __name__ == "__main__":
    unittest.main()
