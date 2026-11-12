"""Is a strategy's backtest result believable? Cost stress, hold-out, period consistency, trade-order Monte Carlo, and a human-set checklist.

Plain statement of what this does NOT do: no parameter is fitted anywhere in this system yet (the runner uses each strategy's fixed
defaults), so "in-sample vs out-of-sample" here is a consistency check on one fixed strategy, not an over-fitting test. The over-fitting
risk lives in how many variants a person tried: `VariantLedger` counts them and the report says so. Nothing in this module promotes
anything: `evaluate` returns checks for a human to read.

The threshold values were proposed by the assistant and ACCEPTED by the owner on 2026-11-05 (`approved_by`). Only a human may change
them (a code change under review); a Thresholds object with no `approved_by` reports itself as a placeholder in every result.
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
from dewako.backtesting.thresholds import Check, Thresholds, evaluate  # noqa: F401  (re-exported)
from dewako.market_data.timeframes import seconds as timeframe_seconds


def scale_costs(sim: SimConfig, k: float) -> SimConfig:
    """Every cost that is a rate (fee, spread, slippage, impact) multiplied by k; limits, latency and faults untouched."""
    if not (k > 0):
        raise ValueError("cost factor must be positive")
    return dataclasses.replace(sim, fee_rate=sim.fee_rate * k, min_half_spread=sim.min_half_spread * k,
                               spread_range_factor=sim.spread_range_factor * k, slippage_base=sim.slippage_base * k,
                               slippage_range_factor=sim.slippage_range_factor * k, impact_k=sim.impact_k * k)


def stress_config(cfg: ReplayConfig, k: float) -> ReplayConfig:
    """The config for a cost stress at factor k. The quote guard's spread ceiling is raised with it (never lowered): without that, doubled
    costs push the simulated spread past the live guard, an exit-protection incident latches, new entries stay blocked for the rest of the
    run, and the "2x costs" result would describe a halted run (2 trades instead of 53), not the same trading at higher cost."""
    guard = dataclasses.replace(cfg.quote_guard, max_spread_pct=cfg.quote_guard.max_spread_pct * max(1.0, k))
    return dataclasses.replace(cfg, sim=scale_costs(cfg.sim, k), quote_guard=guard)


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
    holdout_fraction: float = 0.25
    cost_factors: tuple = (1.0, 2.0, 3.0)
    variants_tried: int = 1
    results: dict = field(default_factory=dict, repr=False)      # the raw replay results, keyed by label (for run records / reports)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


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
        r = replay(ds, stress_config(cfg, k))
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
    hw = halt_warning(results["costs_1x"], cfg)
    if hw:
        warn.append(hw)
    if th.is_placeholder:
        warn.append("Thresholds have no human approval recorded: treat them as placeholders.")
    if ledger and ledger.warning():
        warn.append(ledger.warning())
    if mc and mc["p95"] > full.max_drawdown_pct * 1.5 and mc["p95"] > th.max_drawdown_pct:
        warn.append(f"The same trades in a different order would have had a 95th-percentile drawdown of {mc['p95']:.1%} "
                    f"(this run saw {full.max_drawdown_pct:.1%}).")
    return StrategyValidation(cfg.strategy, cfg.version, full, stress, ins, oos, per, mc, th, checks, warn,
                              holdout_fraction=holdout_fraction, cost_factors=tuple(cost_factors),
                              variants_tried=ledger.count if ledger else 1, results=results)


def compare(ds: Dataset, cfgs: Sequence[ReplayConfig], th: Thresholds = Thresholds(), **kw) -> list[StrategyValidation]:
    """The same data, costs and checks for every strategy. Returns them in the order given: it ranks nothing and promotes nothing.
    The number of variants compared is attached to every result."""
    ledger = VariantLedger()
    for c in cfgs:
        ledger.record(f"{c.strategy}|{c.version}|{c.timeframe}|{c.sim}")
    return [validate_strategy(ds, c, th, ledger=ledger, **kw) for c in cfgs]


def halt_warning(result, cfg) -> Optional[str]:
    """A latched halt (the consecutive-loss limit, or an open exit-protection incident that blocks new entries) rejected a large share of the
    run: the numbers then describe only the part that was traded."""
    ticks = sum(result.ticks.values()) - result.ticks.get("WARMUP", 0)
    kinds = {"consecutive-loss limit": lambda o, d: o == "RISK_REJECTED" and "consecutive losses" in d,
             "open exit-protection incident": lambda o, d: o == "BLOCKED" and "new entries are blocked" in d}
    found = {name: sum(1 for _t, o, d in result.notes if f(o, d)) for name, f in kinds.items()}
    halted = sum(found.values())
    if ticks and halted / ticks > 0.10:
        by = ", ".join(f"{n}: {c}" for n, c in found.items() if c)
        return (f"A latched halt rejected {halted} of {ticks} ticks ({halted / ticks:.0%}) [{by}] with streak_reset={cfg.streak_reset!r}: "
                f"much of the period was not traded, so these numbers describe only the first part of the data.")
    return None


def weaknesses(v: StrategyValidation) -> list[str]:
    """Plain-language list of everything that failed or looks fragile."""
    out = [f"FAILED: {c.name}: {c.detail}" for c in v.checks if not c.ok]
    out += v.warnings
    return out
