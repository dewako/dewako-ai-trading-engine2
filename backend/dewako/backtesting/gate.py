"""The promotion gate: may this strategy move up the ladder, judged on recorded evidence and the owner-approved limits?

Rules that keep it honest:
  * Only the NEWEST recorded run for the strategy counts. A later failing run replaces an earlier passing one; evidence cannot be picked.
  * The run must be for the strategy's CURRENT definition (fingerprint). Changing the rules invalidates the evidence.
  * The gate re-evaluates the stored metrics against the official thresholds itself; it does not trust the verdict stored with the run, so
    a run recorded under lax thresholds proves nothing.
  * The data must be long enough and a large enough share of its end held out.
  * Nothing here promotes anything. It only answers yes/no with reasons; a human with the right permission makes the move.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from dewako.backtesting.metrics import Metrics
from dewako.backtesting.records import RunStore
from dewako.backtesting.thresholds import Thresholds, evaluate

OFFICIAL_THRESHOLDS = Thresholds()


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]
    run_id: Optional[int] = None
    notes: list[str] = field(default_factory=list)


def check(store: RunStore, name: str, version: str, fingerprint: str, th: Thresholds = OFFICIAL_THRESHOLDS) -> GateResult:
    run = store.latest(name, version)
    if run is None:
        return GateResult(False, ["no backtest evidence has been recorded for this strategy"])
    if run["strategy_fingerprint"] != fingerprint:
        return GateResult(False, ["the newest recorded backtest is for a different definition of this strategy (its rules changed since)"], run["id"])
    return judge_run(run, th)


def judge_run(run: dict, th: Thresholds = OFFICIAL_THRESHOLDS) -> GateResult:
    """Apply the official limits to one stored run (its own stored verdict is ignored)."""
    why: list[str] = []
    m, val, ds = run["metrics"], run["validation"], run["dataset"]
    try:
        stress = {float(k): Metrics.from_dict(v) for k, v in m["stress"].items()}
        checks = evaluate(Metrics.from_dict(m["full"]), stress, Metrics.from_dict(m["in_sample"]), Metrics.from_dict(m["out_of_sample"]),
                          [Metrics.from_dict(x) for x in m["periods"]], m.get("monte_carlo"), th)
        why += [f"{c.name}: {c.detail}" for c in checks if not c.ok]
        fee = run["config"]["sim"]["fee_rate"]
        if fee < th.min_fee_rate:
            why.append(f"the run charged a fee of {fee:.4%} per side, below the required {th.min_fee_rate:.2%}")
        if run["config"]["state_source"] != "market_state":            # a run on a simplified state is plumbing, not evidence
            why.append(f"the run used a simplified market state ({run['config']['state_source']}), not the real state builder")
        days = (datetime.fromisoformat(ds["end"]) - datetime.fromisoformat(ds["start"])).total_seconds() / 86400
        if days < th.min_history_days:
            why.append(f"history too short: {days:.0f} days (need {th.min_history_days})")
        if float(val.get("holdout_fraction", 0)) < th.min_holdout_fraction:
            why.append(f"hold-out too small: {val.get('holdout_fraction')} (need {th.min_holdout_fraction})")
    except (KeyError, TypeError, ValueError) as e:                       # a record that cannot be read is not evidence
        return GateResult(False, [f"the recorded evidence is unreadable ({type(e).__name__})"], run["id"])
    notes = []
    if int(val.get("variants_tried", 1)) > 1:
        notes.append(f"{val['variants_tried']} variants were tried on this data; the numbers are optimistic.")
    return GateResult(not why, why, run["id"], notes)
