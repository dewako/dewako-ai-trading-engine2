"""Is a strategy's backtest result believable? Cost stress, hold-out, period consistency, trade-order Monte Carlo, and a human-set checklist.

Plain statement of what this does NOT do: no parameter is fitted anywhere in this system yet (the runner uses each strategy's fixed
defaults), so "in-sample vs out-of-sample" here is a consistency check on one fixed strategy, not an over-fitting test. The over-fitting
risk lives in how many variants a person tried: `VariantLedger` counts them and the report says so. Nothing in this module promotes
anything: `evaluate` returns checks for a human to read.

Every threshold below is a PLACEHOLDER chosen by the assistant, not by the owner, and only a human may change it.
"""
from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from dewako.backtesting.datasets import Dataset
from dewako.backtesting.metrics import Metrics, metrics_of
from dewako.backtesting.replay import ReplayConfig, ReplayResult, replay
from dewako.backtesting.sim_adapter import SimConfig
from dewako.market_data.timeframes import seconds as timeframe_seconds


@dataclass(frozen=True)
class Thresholds:
    """PLACEHOLDER values (set by the assistant, to be replaced by the owner). `is_placeholder` stays True until a human sets them."""
    min_trades: int = 100
    cost_factor_that_must_stay_positive: float = 2.0
    max_drawdown_pct: float = 0.15
    out_of_sample_vs_in_sample: float = 0.5        # out-of-sample expectancy must be at least this share of in-sample expectancy
    min_profitable_period_share: float = 0.5       # of equal consecutive periods
    is_placeholder: bool = True


def scale_costs(sim: SimConfig, k: float) -> SimConfig:
    """Every cost that is a rate (fee, spread, slippage, impact) multiplied by k; limits, latency and faults untouched."""
    if not (k > 0):
        raise ValueError("cost factor must be positive")
    return dataclasses.replace(sim, fee_rate=sim.fee_rate * k, min_half_spread=sim.min_half_spread * k,
                               spread_range_factor=sim.spread_range_factor * k, slippage_base=sim.slippage_base * k,
                               slippage_range_factor=sim.slippage_range_factor * k, impact_k=sim.impact_k * k)


def replay_window(ds: Dataset, cfg: ReplayConfig, start: datetime, end: datetime) -> ReplayResult:
    """Replay only [start, end): earlier candles (up to the lookback) warm the strategy up but are never traded."""
    step = timedelta(seconds=timeframe_seconds(cfg.timeframe))
    warm = max(ds.start, start - step * cfg.lookback)
    return replay(ds.slice(warm, end), dataclasses.replace(cfg, trade_from=start))


def _cut(ds: Dataset, fraction_out: float) -> datetime:
    if not (0 < fraction_out < 1):
        raise ValueError("hold-out fraction must be between 0 and 1")
    n = len(ds.candles)
    i = min(n - 1, max(1, int(n * (1 - fraction_out))))
    return ds.candles[i].t


def monte_carlo_drawdown(pnls: Sequence[float], initial: float, *, runs: int = 1000, seed: int = 0) -> Optional[dict]:
    """Shuffle the order of the same trades `runs` times: how bad could the drawdown have been with the same trades in another order?"""
    if len(pnls) < 2:
        return None
    rng = random.Random(seed)
    order = list(pnls)
    dds = []
    for _ in range(runs):
        rng.shuffle(order)
        eq = peak = initial
        worst = 0.0
        for p in order:
            eq += p
            peak = max(peak, eq)
            worst = max(worst, (peak - eq) / peak if peak > 0 else 0.0)
        dds.append(worst)
    dds.sort()
    q = lambda f: dds[min(len(dds) - 1, int(f * len(dds)))]           # noqa: E731
    return {"runs": runs, "seed": seed, "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "worst": dds[-1]}


@dataclass
class VariantLedger:
    """How many strategy / parameter / timeframe variants were tried before this one was picked. Trying many and reporting the best
    makes the best look better than it is; the count has to travel with the result."""
    tried: list[str] = field(default_factory=list)

    def record(self, label: str) -> None:
        self.tried.append(label)

    @property
    def count(self) -> int:
        return len(set(self.tried))

    def warning(self) -> Optional[str]:
        if self.count > 1:
            return (f"{self.count} variants were tried on this data. The best of {self.count} looks better than a single honest test; "
                    "treat its numbers as optimistic.")
        return None


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class StrategyValidation:
    strategy: str
    version: str
    full: Metrics
    stress: dict[float, Metrics]
    in_sample: Metrics
    out_of_sample: Metrics
    periods: list[Metrics]
    monte_carlo: Optional[dict]
    thresholds: Thresholds
    checks: list[Check]
    warnings: list[str]
    results: dict = field(default_factory=dict, repr=False)      # the raw replay results, keyed by label (for run records / reports)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


def evaluate(full: Metrics, stress: dict[float, Metrics], ins: Metrics, oos: Metrics, periods: Sequence[Metrics],
             mc: Optional[dict], th: Thresholds) -> list[Check]:
    out: list[Check] = []
    out.append(Check("enough trades", full.trades >= th.min_trades, f"{full.trades} closed trades (need {th.min_trades})"))
    k = th.cost_factor_that_must_stay_positive
    sm = stress.get(k)
    out.append(Check(f"positive at {k:g}x costs", sm is not None and sm.net_pnl > 0,
                     "not run" if sm is None else f"net P&L {sm.net_pnl:.2f} at {k:g}x costs"))
    out.append(Check("drawdown within limit", full.max_drawdown_pct <= th.max_drawdown_pct,
                     f"max drawdown {full.max_drawdown_pct:.1%} (limit {th.max_drawdown_pct:.0%})"))
    if ins.expectancy is None or ins.expectancy <= 0:
        out.append(Check("out-of-sample holds up", False, "no positive in-sample expectancy to compare against"))
    else:
        o = oos.expectancy
        out.append(Check("out-of-sample holds up", o is not None and o > 0 and o >= th.out_of_sample_vs_in_sample * ins.expectancy,
                         f"out-of-sample expectancy {'n/a' if o is None else f'{o:.2f}'} vs in-sample {ins.expectancy:.2f} "
                         f"(need >= {th.out_of_sample_vs_in_sample:.0%} of it, and positive)"))
    good = sum(1 for p in periods if p.net_pnl > 0)
    share = good / len(periods) if periods else 0.0
    out.append(Check("consistent across periods", share >= th.min_profitable_period_share,
                     f"{good} of {len(periods)} equal periods profitable (need {th.min_profitable_period_share:.0%})"))
    return out


def validate_strategy(ds: Dataset, cfg: ReplayConfig, th: Thresholds = Thresholds(), *, holdout_fraction: float = 0.25,
                      periods: int = 4, cost_factors: Sequence[float] = (1.0, 2.0, 3.0), mc_runs: int = 1000, seed: int = 0,
                      ledger: Optional[VariantLedger] = None) -> StrategyValidation:
    if periods < 2:
        raise ValueError("need at least two periods")
    if 1.0 not in cost_factors:
        raise ValueError("cost_factors must include 1.0 (the unstressed run)")
    results: dict = {}
    stress: dict[float, Metrics] = {}
    for k in cost_factors:
        r = replay(ds, dataclasses.replace(cfg, sim=scale_costs(cfg.sim, k)))
        results[f"costs_{k:g}x"] = r
        stress[k] = metrics_of(r)
    full = stress[1.0]
    cut = _cut(ds, holdout_fraction)
    r_in = replay_window(ds, cfg, ds.candles[0].t, cut)
    r_out = replay_window(ds, cfg, cut, ds.end)
    results["in_sample"], results["out_of_sample"] = r_in, r_out
    n = len(ds.candles)
    per: list[Metrics] = []
    for i in range(periods):
        a, b = ds.candles[i * n // periods].t, (ds.candles[(i + 1) * n // periods].t if i + 1 < periods else ds.end)
        r = replay_window(ds, cfg, a, b)
        results[f"period_{i + 1}"] = r
        per.append(metrics_of(r))
    mc = monte_carlo_drawdown([t.pnl for t in results["costs_1x"].trades], cfg.initial_cash, runs=mc_runs, seed=seed)
    ins, oos = metrics_of(r_in), metrics_of(r_out)
    checks = evaluate(full, stress, ins, oos, per, mc, th)
    warn = list(full.warnings)
    if th.is_placeholder:
        warn.append("Thresholds are the assistant's placeholders, not set by the owner.")
    if ledger and ledger.warning():
        warn.append(ledger.warning())
    if mc and mc["p95"] > full.max_drawdown_pct * 1.5 and mc["p95"] > th.max_drawdown_pct:
        warn.append(f"The same trades in a different order would have had a 95th-percentile drawdown of {mc['p95']:.1%} "
                    f"(this run saw {full.max_drawdown_pct:.1%}).")
    return StrategyValidation(cfg.strategy, cfg.version, full, stress, ins, oos, per, mc, th, checks, warn, results)


def compare(ds: Dataset, cfgs: Sequence[ReplayConfig], th: Thresholds = Thresholds(), **kw) -> list[StrategyValidation]:
    """The same data, costs and checks for every strategy. Returns them in the order given: it ranks nothing and promotes nothing.
    The number of variants compared is attached to every result."""
    ledger = VariantLedger()
    for c in cfgs:
        ledger.record(f"{c.strategy}|{c.version}|{c.timeframe}|{c.sim}")
    return [validate_strategy(ds, c, th, ledger=ledger, **kw) for c in cfgs]


def weaknesses(v: StrategyValidation) -> list[str]:
    """Plain-language list of everything that failed or looks fragile."""
    out = [f"FAILED: {c.name}: {c.detail}" for c in v.checks if not c.ok]
    out += v.warnings
    return out
