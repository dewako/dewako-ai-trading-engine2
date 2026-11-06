"""Command line: validate one strategy on stored candles and record the evidence.

    python -m dewako.backtesting.cli --candles candles.db --symbol BTC/USDT --timeframe 5m --source binance_public \\
        --start 2025-01-01 --end 2026-01-01 --strategy "Trend AI" --version 2.2

It reads recorded candles (it never downloads anything and has no credentials), refuses data that fails validation, replays on the
simulated exchange, runs the cost-stress / hold-out / period / Monte-Carlo checks, and saves one append-only run record. Nothing here
promotes a strategy; the dashboard shows the record and a human decides. The cost settings below are assumptions; they are saved with the run.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from dewako.backtesting.datasets import DatasetRejected, build_dataset
from dewako.backtesting.records import RunRefused, RunStore
from dewako.backtesting.replay import ReplayConfig
from dewako.backtesting.sim_adapter import SimConfig
from dewako.backtesting.validation import Thresholds, validate_strategy
from dewako.market_data.store import CandleStore
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, database_target_from_env
from dewako.strategies import base, library  # noqa: F401
from dewako.strategies.versions import fingerprint
from dewako.strategy_packages.manager import PackageManager


def _t(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dewako.backtesting.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--candles", required=True, help="path of the candle store (SQLite) holding recorded candles")
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument("--source", required=True, help="candle source label as stored, e.g. binance_public")
    p.add_argument("--start", required=True, type=_t, help="inclusive, ISO date or time (UTC if no zone)")
    p.add_argument("--end", required=True, type=_t, help="exclusive")
    p.add_argument("--strategy", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--lookback", type=int, default=300)
    p.add_argument("--holdout", type=float, default=0.25)
    p.add_argument("--periods", type=int, default=4)
    p.add_argument("--mc-runs", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--streak-reset", choices=("never", "daily"), default="never",
                   help="risk engine's max-consecutive-losses latch: 'never' = as the platform is, 'daily' = a person is assumed to resume at the next UTC midnight")
    p.add_argument("--max-hold-seconds", type=float, default=None)
    p.add_argument("--fee-rate", type=float, default=SimConfig().fee_rate)
    p.add_argument("--allow-gap", action="append", default=[], type=_t, metavar="TIME", help="a missing candle a human has explained (repeatable)")
    p.add_argument("--ack-spike", action="append", default=[], type=_t, metavar="TIME", help="a price spike a human has checked and accepts (repeatable)")
    p.add_argument("--variants-tried", type=int, default=1, help="how many strategy/parameter variants were tried on this data before this one")
    p.add_argument("--dry-run", action="store_true", help="run everything but do not save a record")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[list[str]] = None, *, db: Optional[Database] = None) -> int:
    a = parser().parse_args(argv)
    try:
        ds = build_dataset(CandleStore(a.candles), a.symbol, a.timeframe, a.source, a.start, a.end,
                           allowed_gaps=a.allow_gap, acknowledged_spikes=a.ack_spike)
    except DatasetRejected as e:
        print("DATA REJECTED: " + "; ".join(f"{f.kind}: {f.detail}" for f in e.report.errors[:10]), file=sys.stderr)
        return 2
    database = db or Database(database_target_from_env())
    audit = AuditLog(database)
    PackageManager(database, audit).sync_registry()                          # makes imported packages visible to the replay
    try:
        cls = base.get(a.strategy, a.version)
    except KeyError:
        print(f"unknown strategy {a.strategy} v{a.version}", file=sys.stderr)
        return 2
    cfg = ReplayConfig(strategy=a.strategy, version=a.version, symbol=a.symbol, timeframe=a.timeframe, initial_cash=a.cash,
                       lookback=a.lookback, sim=SimConfig(fee_rate=a.fee_rate), max_hold_seconds=a.max_hold_seconds, streak_reset=a.streak_reset)
    from dewako.backtesting.validation import VariantLedger
    ledger = VariantLedger(tried=[f"variant-{i}" for i in range(max(1, a.variants_tried))])
    v = validate_strategy(ds, cfg, Thresholds(), holdout_fraction=a.holdout, periods=a.periods, mc_runs=a.mc_runs, seed=a.seed, ledger=ledger)
    actor = {"user_id": None, "username": f"cli:{getpass.getuser()}", "role": "ANALYST"}
    rid = None
    if not a.dry_run:
        try:
            rid = RunStore(database, audit).save(v, ds, cfg, fingerprint(cls), actor)
        except RunRefused as e:
            print(f"not saved: {e}", file=sys.stderr)
            return 2
    summary = {"run_id": rid, "verdict": "PASSED" if v.passed else "FAILED", "simulated": True, "trades": v.full.trades,
               "net_pnl": v.full.net_pnl, "max_drawdown_pct": v.full.max_drawdown_pct, "dataset_hash": ds.content_hash,
               "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks], "warnings": v.warnings}
    if a.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{'(dry run, not saved) ' if a.dry_run else f'run #{rid} saved: '}{summary['verdict']} (SIMULATED)")
        for c in v.checks:
            print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        for w in v.warnings:
            print(f"  note: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
