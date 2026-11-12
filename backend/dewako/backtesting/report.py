"""Read-only report data for the dashboard: plain dictionaries made from stored run records. No run can be started, edited or promoted here."""
from __future__ import annotations

from dewako.backtesting.gate import OFFICIAL_THRESHOLDS, judge_run

LIMITS = (
    "SIMULATED: every number comes from replaying recorded candles through a simulated exchange. No order was ever sent anywhere.",
    "The cost model (spread, slippage, impact, fees) is an assumption, not calibrated to a real exchange.",
    "A strategy that passes is allowed to be paper-traded. It is not shown to be profitable, and live trading stays locked.",
    "Only the newest run for a strategy counts toward promotion, and only if the strategy definition has not changed since.",
)


def summary(run: dict) -> dict:
    full = run["metrics"]["full"]
    val = run["validation"]
    return {"id": run["id"], "created_at": run["created_at"], "created_by": run["created_by"], "strategy": run["strategy_name"],
            "version": run["strategy_version"], "symbol": run["symbol"], "timeframe": run["timeframe"], "verdict": run["verdict"],
            "simulated": bool(run["simulated"]), "dataset_start": run["dataset"].get("start"), "dataset_end": run["dataset"].get("end"),
            "dataset_hash": run["dataset_hash"], "digest": run["run_digest"], "code_version": run["code_version"],
            "trades": full["trades"], "net_pnl": full["net_pnl"], "total_return_pct": full["total_return_pct"],
            "max_drawdown_pct": full["max_drawdown_pct"], "win_rate": full["win_rate"], "profit_factor": full["profit_factor"],
            "expectancy": full["expectancy"], "variants_tried": val.get("variants_tried", 1)}


def detail(run: dict, *, current: bool | None = None) -> dict:
    """`current`: whether this is the newest run for its strategy (the only one that counts); the caller knows, this module does not query."""
    g = judge_run(run, OFFICIAL_THRESHOLDS)
    m, val = run["metrics"], run["validation"]
    out = summary(run)
    out.update({
        "config": run["config"], "dataset": run["dataset"], "thresholds": val.get("thresholds"),
        "stored_checks": val.get("checks", []), "warnings": val.get("warnings", []),
        "official_gate": {"ok": g.ok, "reasons": g.reasons, "notes": g.notes},
        "stress": {k: {"net_pnl": v["net_pnl"], "trades": v["trades"]} for k, v in m["stress"].items()},
        "in_sample": {"expectancy": m["in_sample"]["expectancy"], "trades": m["in_sample"]["trades"]},
        "out_of_sample": {"expectancy": m["out_of_sample"]["expectancy"], "trades": m["out_of_sample"]["trades"]},
        "periods": [{"net_pnl": p["net_pnl"], "trades": p["trades"]} for p in m["periods"]],
        "monte_carlo": m.get("monte_carlo"), "metrics": m["full"], "counts_for_promotion": current,
        "limits": list(LIMITS),
        "weaknesses": [f"FAILED: {r}" for r in g.reasons] + list(val.get("warnings", [])) + list(m["full"].get("warnings", [])),
    })
    return out
