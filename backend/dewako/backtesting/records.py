"""Stored backtest run records: the evidence a promotion is judged on.

A record holds everything needed to identify and re-check a result: the strategy (name, version, definition fingerprint), the exact data
(content hash and span), the full replay settings, the metrics of every stress/hold-out/period run, the thresholds in force, the result
digest and the code version. Records are append-only (database triggers refuse UPDATE and DELETE) and each save is audited in the same
transaction. Records come only from `RunStore.save`, which takes a finished `StrategyValidation`: no HTTP route may accept metrics from a client.
"""
from __future__ import annotations

import dataclasses
import json
import os
from typing import TYPE_CHECKING, Any, Optional

from dewako.auth.service import PERMISSIONS
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, now_iso

if TYPE_CHECKING:                       # the record store must not load the replay / execution stack
    from dewako.backtesting.datasets import Dataset
    from dewako.backtesting.replay import ReplayConfig
    from dewako.backtesting.validation import StrategyValidation


class RunRefused(Exception):
    pass


def code_version() -> str:
    return os.environ.get("DEWAKO_GIT_COMMIT", "unknown")[:64]


def metrics_blob(v: "StrategyValidation") -> dict:
    return {"full": v.full.as_dict(), "stress": {f"{k:g}": m.as_dict() for k, m in v.stress.items()},
            "in_sample": v.in_sample.as_dict(), "out_of_sample": v.out_of_sample.as_dict(),
            "periods": [m.as_dict() for m in v.periods], "monte_carlo": v.monte_carlo}


def validation_blob(v: "StrategyValidation") -> dict:
    return {"thresholds": dataclasses.asdict(v.thresholds), "placeholder_thresholds": v.thresholds.is_placeholder,
            "checks": [dataclasses.asdict(c) for c in v.checks], "warnings": list(v.warnings),
            "holdout_fraction": v.holdout_fraction, "periods": len(v.periods), "cost_factors": list(v.cost_factors),
            "variants_tried": v.variants_tried}


class RunStore:
    def __init__(self, db: Database, audit: AuditLog):
        self.db, self.audit = db, audit

    def save(self, v: "StrategyValidation", ds: "Dataset", cfg: "ReplayConfig", fingerprint: str, actor: dict, *,
             version_of_code: Optional[str] = None, correlation_id: str = "") -> int:
        if "backtests" not in PERMISSIONS.get(actor.get("role", ""), set()):
            self.audit.record("BACKTEST_RUN_RECORDED", "DENIED", correlation_id, actor=actor, resource=f"strategy:{v.strategy}")
            raise RunRefused("requires permission backtests")
        if (v.strategy, v.version, ds.symbol, ds.timeframe) != (cfg.strategy, cfg.version, cfg.symbol, cfg.timeframe):
            raise RunRefused("the validation, dataset and config describe different things")
        if "costs_1x" not in v.results:
            raise RunRefused("the validation has no unstressed run to record a digest from")
        if not fingerprint:
            raise RunRefused("a run record needs the strategy definition's fingerprint")
        verdict = "PASSED" if v.passed else "FAILED"
        digest = v.results["costs_1x"].digest
        with self.db.tx() as c:
            c.execute("INSERT INTO backtest_runs(created_at,created_by,strategy_name,strategy_version,strategy_fingerprint,symbol,timeframe,"
                      "dataset_hash,dataset,config,run_digest,code_version,metrics,validation,verdict,simulated) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                      (now_iso(), actor.get("username", "system"), v.strategy, v.version, fingerprint, ds.symbol, ds.timeframe,
                       ds.content_hash, json.dumps(ds.record()), json.dumps(cfg.record(), default=str), digest,
                       version_of_code or code_version(), json.dumps(metrics_blob(v), default=str),
                       json.dumps(validation_blob(v), default=str), verdict))
            rid = c.execute("SELECT MAX(id) FROM backtest_runs").fetchone()[0]
            self.audit.record("BACKTEST_RUN_RECORDED", "OK", correlation_id, actor=actor, resource=f"backtest_run:{rid}",
                              metadata={"strategy": v.strategy, "version": v.version, "verdict": verdict, "dataset_hash": ds.content_hash,
                                        "digest": digest}, conn=c)
        return rid

    def get(self, run_id: int) -> dict[str, Any]:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise KeyError(run_id)
        return self._open(dict(r))

    def latest(self, name: str, version: str) -> Optional[dict[str, Any]]:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM backtest_runs WHERE strategy_name=? AND strategy_version=? ORDER BY id DESC LIMIT 1",
                          (name, version)).fetchone()
        return self._open(dict(r)) if r else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]
        return [self._open(r) for r in rows]

    @staticmethod
    def _open(r: dict) -> dict:
        for k in ("dataset", "config", "metrics", "validation"):
            r[k] = json.loads(r[k])
        return r
