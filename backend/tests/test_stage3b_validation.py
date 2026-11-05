"""Stage 3B step 2: cost stress, hold-out, period consistency, Monte Carlo and the human-set checklist."""
import dataclasses
import math
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.metrics import compute_metrics  # noqa: E402
from dewako.backtesting.replay import ReplayConfig, replay  # noqa: E402
from dewako.backtesting.sim_adapter import SimConfig  # noqa: E402
from dewako.backtesting.validation import (Thresholds, VariantLedger, compare, evaluate, monte_carlo_drawdown, replay_window,  # noqa: E402
                                           scale_costs, validate_strategy, weaknesses)
from dewako.core.types import Candle, Side, Signal  # noqa: E402
from dewako.strategies import base  # noqa: E402
from test_stage3a_replay import CLEAN, M5, SYM, T0, TF, dataset  # noqa: E402


def bars_closed(state):
    return int((state.candles[-1].t - T0) / M5) + 1      # position in the data, not the (capped) window length


def make_cycle(name, buy_mod=10, sell_mod=5):
    class Cycle(base.Strategy):
        supported_timeframes = ["5m"]
        default_parameters = {}

        def generate_signal(self, state):
            n = bars_closed(state)
            if n % buy_mod != 0:
                return None
            c = state.candles[-1].c
            return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name, strategy_version=self.version,
                          reason=["cycle"], entry_price=c, stop_price=c * 0.97, take_profit_price=None)

        def manage_position(self, state, position):
            if bars_closed(state) % buy_mod == sell_mod:
                c = state.candles[-1].c
                return Signal(symbol=state.symbol, side=Side.SELL, confidence=0.9, strategy=self.name, strategy_version=self.version,
                              reason=["cycle exit"], entry_price=c)
            return None
    Cycle.name, Cycle.version = name, "1.0"
    base.register(Cycle)


def trending(n=160, drift=0.002, seed=1):
    rng = random.Random(seed)
    out, p = [], 100.0
    for i in range(n):
        o = p
        p = p * math.exp(drift + rng.uniform(-0.0005, 0.0005))
        out.append(Candle(t=T0 + M5 * i, o=o, h=max(o, p) * 1.0002, l=min(o, p) * 0.9998, c=p, v=1e9))
    return out


def cfg(name, **sim):
    return ReplayConfig(strategy=name, version="1.0", symbol=SYM, timeframe=TF, lookback=20, sim=SimConfig(**{**CLEAN, **sim}))


class Costs(unittest.TestCase):
    def test_scale_costs_multiplies_every_cost_rate_and_nothing_else(self):
        s = SimConfig(fee_rate=0.001, min_half_spread=0.0002, spread_range_factor=0.05, slippage_base=0.0001, slippage_range_factor=0.04,
                      impact_k=0.5, max_participation=0.1, latency_seconds=2.0, reject_rate=0.0, seed=7, min_notional=5.0)
        k = scale_costs(s, 2.0)
        for f in ("fee_rate", "min_half_spread", "spread_range_factor", "slippage_base", "slippage_range_factor", "impact_k"):
            self.assertAlmostEqual(getattr(k, f), 2 * getattr(s, f), msg=f)
        for f in ("max_participation", "latency_seconds", "seed", "min_notional", "step_size", "min_qty", "max_qty"):
            self.assertEqual(getattr(k, f), getattr(s, f), f)
        with self.assertRaises(ValueError):
            scale_costs(s, 0)

    def test_the_same_strategy_that_wins_at_one_times_costs_loses_at_two(self):
        make_cycle("Cycle Cost")
        v = validate_strategy(dataset(trending()), cfg("Cycle Cost", fee_rate=0.003), Thresholds(min_trades=5, is_placeholder=False), mc_runs=50)
        self.assertGreater(v.stress[1.0].net_pnl, 0)
        self.assertLess(v.stress[2.0].net_pnl, 0)
        self.assertLess(v.stress[3.0].net_pnl, v.stress[2.0].net_pnl)
        failed = [c.name for c in v.checks if not c.ok]
        self.assertIn("positive at 2x costs", failed)
        self.assertFalse(v.passed)
        self.assertTrue(any("positive at 2x" in w for w in weaknesses(v)))


class Windows(unittest.TestCase):
    def test_a_window_trades_only_inside_itself_and_its_warmup_is_never_traded(self):
        make_cycle("Cycle Window")
        ds = dataset(trending())
        start = ds.candles[80].t
        r = replay_window(ds, cfg("Cycle Window"), start, ds.end)
        self.assertGreater(len(r.trades), 3)
        self.assertTrue(all(t.entry_time >= start for t in r.trades))
        self.assertEqual(r.equity[0][0], start + M5)                                       # the curve starts with the first live bar
        self.assertEqual(r.config["trade_from"], start.isoformat())

    def test_the_strategy_is_shown_a_full_lookback_of_warm_up_candles_before_the_window_starts(self):
        from test_stage3a_replay import make_probe
        probe = make_probe("Probe Window Warm", buy_at=10**6)
        ds = dataset(trending())
        replay_window(ds, cfg("Probe Window Warm"), ds.candles[80].t, ds.end)
        self.assertEqual(probe.seen_log[0][0], 20)                                         # lookback candles, not one
        self.assertEqual(probe.seen_log[0][1], ds.candles[79].t)                           # the last one closed just before the window
        probe.seen_log.clear()
        replay_window(ds, cfg("Probe Window Warm"), ds.candles[5].t, ds.end)               # near the start: warm-up is what exists
        self.assertEqual(probe.seen_log[0][0], 5)

    def test_in_sample_and_out_of_sample_do_not_overlap_and_together_cover_the_data(self):
        make_cycle("Cycle Split")
        ds = dataset(trending())
        v = validate_strategy(ds, cfg("Cycle Split"), Thresholds(min_trades=5, is_placeholder=False), holdout_fraction=0.25, mc_runs=10)
        i, o = v.results["in_sample"], v.results["out_of_sample"]
        cut = ds.candles[120].t
        self.assertTrue(all(t.entry_time < cut for t in i.trades))
        self.assertTrue(all(t.entry_time >= cut for t in o.trades))
        self.assertEqual(i.equity[-1][0], cut)
        self.assertEqual(o.equity[0][0], cut + M5)
        self.assertEqual(len(v.periods), 4)
        ps = [v.results[f"period_{k}"] for k in (1, 2, 3, 4)]
        for a, b in zip(ps, ps[1:]):
            self.assertEqual(a.equity[-1][0] + M5, b.equity[0][0])                         # contiguous: no bar lost or counted twice
        self.assertEqual(ps[0].equity[0][0], ds.candles[0].t + M5)
        self.assertEqual(ps[-1].equity[-1][0], ds.end)                                     # and together they reach the end
        self.assertEqual(sum(len(r.equity) for r in ps), len(ds.candles))
        self.assertEqual(sorted(k for k in v.results if k.startswith("period_")), ["period_1", "period_2", "period_3", "period_4"])


class MonteCarlo(unittest.TestCase):
    def test_a_known_two_trade_case(self):
        mc = monte_carlo_drawdown([10.0, -10.0], 100.0, runs=200, seed=3)
        self.assertAlmostEqual(mc["worst"], 0.1)                                           # loss first: 100 -> 90
        self.assertAlmostEqual(mc["p50"], 10 / 110, places=9) if mc["p50"] < 0.095 else self.assertAlmostEqual(mc["p50"], 0.1)
        self.assertGreaterEqual(mc["p99"], mc["p95"])
        self.assertGreaterEqual(mc["p95"], mc["p50"])
        wide = monte_carlo_drawdown([5, -3, 2, -8, 6, 1, -2, 4], 100.0, runs=2000, seed=2)
        self.assertLess(wide["p50"], wide["p95"])                                          # a real spread, so each quantile is its own
        self.assertLessEqual(wide["p95"], wide["p99"])

    def test_it_is_repeatable_and_needs_at_least_two_trades(self):
        p = [5, -3, 2, -8, 6, 1, -2, 4]
        self.assertEqual(monte_carlo_drawdown(p, 1000.0, runs=100, seed=1), monte_carlo_drawdown(p, 1000.0, runs=100, seed=1))
        self.assertIsNone(monte_carlo_drawdown([5.0], 1000.0))

    def test_a_losing_streak_is_always_possible_so_the_worst_case_is_at_least_the_run_you_saw(self):
        p = [5, -3, 2, -8, 6, 1, -2, 4]
        mc = monte_carlo_drawdown(p, 100.0, runs=2000, seed=2)
        eq = peak = 100.0
        seen = 0.0
        for x in p:
            eq += x
            peak = max(peak, eq)
            seen = max(seen, (peak - eq) / peak)
        self.assertGreaterEqual(mc["worst"] + 1e-12, seen)


class Variants(unittest.TestCase):
    def test_trying_more_than_one_variant_is_a_warning_and_duplicates_are_not_counted_twice(self):
        led = VariantLedger()
        self.assertIsNone(led.warning())
        led.record("a")
        led.record("a")
        self.assertEqual((led.count, led.warning()), (1, None))
        led.record("b")
        self.assertEqual(led.count, 2)
        self.assertIn("2 variants", led.warning())


def base_metrics(**kw):
    eq = [(T0 + timedelta(hours=k), 1000.0) for k in range(1, 5)]
    m = compute_metrics([], eq, [], 1000.0)
    return dataclasses.replace(m, **kw)


class Checklist(unittest.TestCase):
    TH = Thresholds(min_trades=100, cost_factor_that_must_stay_positive=2.0, max_drawdown_pct=0.15, out_of_sample_vs_in_sample=0.5,
                    min_profitable_period_share=0.5, is_placeholder=False)

    def run_checks(self, full=None, s2=None, ins=None, oos=None, periods=None):
        full = full or base_metrics(trades=100, max_drawdown_pct=0.15)
        stress = {1.0: full, 2.0: s2 or base_metrics(net_pnl=1.0)}
        ins = ins or base_metrics(expectancy=2.0)
        oos = oos or base_metrics(expectancy=1.0)
        periods = periods or [base_metrics(net_pnl=1.0), base_metrics(net_pnl=-1.0)]
        return {c.name: c.ok for c in evaluate(full, stress, ins, oos, periods, None, self.TH)}

    def test_every_boundary_passes_exactly_at_the_limit(self):
        self.assertTrue(all(self.run_checks().values()))

    def test_each_check_fails_on_its_own(self):
        self.assertFalse(self.run_checks(full=base_metrics(trades=99, max_drawdown_pct=0.15))["enough trades"])
        self.assertFalse(self.run_checks(s2=base_metrics(net_pnl=0.0))["positive at 2x costs"])
        self.assertFalse(self.run_checks(full=base_metrics(trades=100, max_drawdown_pct=0.1501))["drawdown within limit"])
        self.assertFalse(self.run_checks(oos=base_metrics(expectancy=0.99))["out-of-sample holds up"])
        self.assertFalse(self.run_checks(oos=base_metrics(expectancy=None))["out-of-sample holds up"])
        self.assertFalse(self.run_checks(ins=base_metrics(expectancy=-1.0), oos=base_metrics(expectancy=5.0))["out-of-sample holds up"])
        self.assertFalse(self.run_checks(periods=[base_metrics(net_pnl=1.0), base_metrics(net_pnl=-1.0), base_metrics(net_pnl=-1.0)])
                         ["consistent across periods"])

    def test_a_missing_stress_run_fails_rather_than_passes(self):
        full = base_metrics(trades=100, max_drawdown_pct=0.0)
        checks = {c.name: c.ok for c in evaluate(full, {1.0: full}, base_metrics(expectancy=1.0), base_metrics(expectancy=1.0),
                                                 [base_metrics(net_pnl=1.0)], None, self.TH)}
        self.assertFalse(checks["positive at 2x costs"])


class EndToEnd(unittest.TestCase):
    def test_a_steady_edge_passes_every_check_and_the_placeholder_warning_is_shown_until_a_human_sets_thresholds(self):
        make_cycle("Cycle Good")
        ds = dataset(trending(n=200, drift=0.003))
        th = Thresholds(min_trades=10, max_drawdown_pct=0.15, is_placeholder=False)
        v = validate_strategy(ds, cfg("Cycle Good", fee_rate=0.001), dataclasses.replace(th, is_placeholder=True), mc_runs=100)
        self.assertTrue(v.passed, [c for c in v.checks if not c.ok])
        self.assertTrue(any("placeholder" in w for w in v.warnings))
        tiny = validate_strategy(dataset(trending(n=60)), cfg("Cycle Good", fee_rate=0.001), th, mc_runs=5)   # human-set: no such warning
        self.assertFalse(any("placeholder" in w for w in tiny.warnings))
        self.assertTrue(Thresholds().is_placeholder)

    def test_default_thresholds_demand_a_hundred_trades(self):
        make_cycle("Cycle Few")
        v = validate_strategy(dataset(trending(n=120)), cfg("Cycle Few"), mc_runs=10)
        self.assertFalse(v.passed)
        self.assertFalse([c for c in v.checks if c.name == "enough trades"][0].ok)

    def test_compare_keeps_the_order_given_ranks_nothing_and_attaches_the_variant_count(self):
        make_cycle("Cycle A")
        make_cycle("Cycle B", buy_mod=12, sell_mod=6)
        ds = dataset(trending(n=90))
        res = compare(ds, [cfg("Cycle B"), cfg("Cycle A")], Thresholds(min_trades=5, is_placeholder=False), mc_runs=10)
        self.assertEqual([r.strategy for r in res], ["Cycle B", "Cycle A"])
        self.assertTrue(all(any("2 variants" in w for w in r.warnings) for r in res))
        self.assertFalse(hasattr(res[0], "rank"))

    def test_bad_arguments_are_refused(self):
        make_cycle("Cycle Args")
        ds = dataset(trending(n=60))
        with self.assertRaises(ValueError):
            validate_strategy(ds, cfg("Cycle Args"), periods=1)
        with self.assertRaises(ValueError):
            validate_strategy(ds, cfg("Cycle Args"), cost_factors=(2.0, 3.0))
        with self.assertRaises(ValueError):
            validate_strategy(ds, cfg("Cycle Args"), holdout_fraction=1.0)

    def test_the_validation_does_not_change_the_plain_replay(self):
        make_cycle("Cycle Same")
        ds = dataset(trending(n=120))
        c = cfg("Cycle Same")
        self.assertEqual(replay(ds, c).digest, validate_strategy(ds, c, Thresholds(min_trades=1), mc_runs=5).results["costs_1x"].digest)


if __name__ == "__main__":
    unittest.main()
