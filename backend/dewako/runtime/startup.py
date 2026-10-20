"""Startup sequence. Live execution only becomes possible if EVERY step passes.

DB -> safety state -> strategy fingerprints -> recover unresolved orders -> reconcile each live exchange -> READY
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dewako.persistence.db import Database, now_iso
from dewako.strategies.base import Strategy
from dewako.strategies.versions import StrategyVersionConflict, fingerprint


class StrategyRegistryStore:
    """Persists strategy fingerprints. A released version whose fingerprint changed is marked INVALID and disabled."""

    def __init__(self, db: Database):
        self.db = db

    def verify(self, strategies: list[type[Strategy]]) -> list[str]:
        bad = []
        ts = now_iso()
        with self.db.tx() as c:
            for cls in strategies:
                fp = fingerprint(cls)
                row = c.execute("SELECT fingerprint, release_status FROM strategy_versions WHERE strategy_id=? AND strategy_version=?",
                                (cls.name, cls.version)).fetchone()
                if row is None:
                    status = "DRAFT" if cls.experimental else "RELEASED"  # experimental strategies never become live-eligible
                    c.execute("INSERT INTO strategy_versions(strategy_id,strategy_version,fingerprint,release_status,enabled,created_at,released_at) VALUES (?,?,?,?,?,?,?)",
                              (cls.name, cls.version, fp, status, 1, ts, ts if status == "RELEASED" else None))
                elif row["fingerprint"] != fp:
                    c.execute("UPDATE strategy_versions SET release_status='INVALID', enabled=0 WHERE strategy_id=? AND strategy_version=?", (cls.name, cls.version))
                    bad.append(f"{cls.name} v{cls.version}")
        if bad:
            raise StrategyVersionConflict("released strategy changed behaviour: " + ", ".join(bad) + ". Marked INVALID; publish as a new version.")
        return bad

    def set_enabled(self, name: str, version: str, enabled: bool) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE strategy_versions SET enabled=? WHERE strategy_id=? AND strategy_version=? AND release_status<>'INVALID'",
                      (1 if enabled else 0, name, version))


@dataclass
class StartupReport:
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))


async def startup(db: Database, safety, engine, recon, strategies: list[type[Strategy]], live_exchanges: list[str]) -> StartupReport:
    r = StartupReport()
    try:
        db.migrate()
        with db.read() as c:
            c.execute("SELECT 1").fetchone()
        r.add("database", True)
    except Exception as e:
        r.add("database", False, type(e).__name__)
        return r  # nothing else can be trusted
    r.add("safety_state", safety.baseline_intact(), "" if safety.baseline_intact() else "baseline records missing: trading blocked")
    try:
        StrategyRegistryStore(db).verify(strategies)
        r.add("strategy_fingerprints", True)
    except StrategyVersionConflict as e:
        r.add("strategy_fingerprints", False, str(e))
    try:
        await engine.recover("LIVE")
        r.add("order_recovery", True)
    except Exception as e:
        r.add("order_recovery", False, type(e).__name__)
    for ex in live_exchanges:
        try:
            res = await recon.run(ex)
            r.add(f"reconcile:{ex}", res["result"] == "OK", res["result"])
        except Exception as e:
            r.add(f"reconcile:{ex}", False, type(e).__name__)
    safety.on_startup(r.ok)
    return r
