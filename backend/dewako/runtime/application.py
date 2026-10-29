"""Application lifecycle: ordered startup, supervised workers, graceful shutdown.

Startup (each step recorded; a critical failure BLOCKS live trading but the process stays up for inspection/admin work):
  config -> database -> schema -> safety state -> strategy fingerprints -> recover orders -> recover positions
  -> reconcile live accounts -> validate risk state -> start workers -> health monitor -> READY
Shutdown: stop accepting work -> stop workers (never mid-transition) -> persist -> close exchange connections -> close database.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from dewako.api.runtime_state import Runtime
from dewako.core.types import MarketState
from dewako.exchanges.base import ExchangeAdapter
from dewako.execution.engine import ExecutionEngine
from dewako.execution.validation import QuoteGuardConfig
from dewako.market_data.freshness import validate_candles
from dewako.execution.positions import PositionStore
from dewako.persistence.db import Database, DatabaseUnavailable
from dewako.persistence.migrations import MIGRATIONS
from dewako.reconciliation.service import ReconciliationService
from dewako.risk.engine import RiskEngine
from dewako.risk.state import RiskStateStore
from dewako.runtime.bot_runner import BotConfig, BotRunner
from dewako.runtime.health import health_report
from dewako.runtime.startup import StrategyRegistryStore
from dewako.runtime.lease import WorkerLease
from dewako.runtime.workers import WorkerSupervisor
from dewako.strategies.base import all_strategies
from dewako.strategies.versions import StrategyVersionConflict

log = logging.getLogger("dewako.app")

REQUIRED_TABLES = {"users", "sessions", "exchanges", "credentials", "strategy_versions", "signals", "orders", "order_events", "positions",
                   "position_events", "trades", "safety_state", "kill_switch_events", "risk_state", "reconciliation_state",
                   "account_snapshots", "audit_log", "worker_health", "bots", "risk_decisions", "worker_leases", "strategy_packages"}


@dataclass
class Config:
    db_target: str
    workers_enabled: bool = True
    recon_interval: float = 60.0
    recover_interval: float = 15.0
    bot_interval: float = 30.0
    risk_interval: float = 30.0
    health_interval: float = 15.0
    market_interval: float = 60.0
    lease_ttl: float = 30.0
    audit_verify_interval: float = 300.0

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        from dewako.persistence.db import database_target_from_env
        return cls(db_target=database_target_from_env(e),
                   workers_enabled=e.get("DEWAKO_WORKERS", "1") == "1",
                   recon_interval=float(e.get("DEWAKO_RECON_INTERVAL", "60")), bot_interval=float(e.get("DEWAKO_BOT_INTERVAL", "30")))


@dataclass
class StartupStep:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


@dataclass
class Application:
    config: Config
    adapters: dict[str, ExchangeAdapter] = field(default_factory=dict)
    exchange_ids: dict[str, int] = field(default_factory=dict)
    state_provider: Optional[Callable[[str, str], Awaitable[MarketState]]] = None
    vault: object = None
    rt: Optional[Runtime] = None
    engine: Optional[ExecutionEngine] = None
    recon: Optional[ReconciliationService] = None
    supervisor: Optional[WorkerSupervisor] = None
    steps: list[StartupStep] = field(default_factory=list)
    status: str = "STOPPED"        # STOPPED | STARTING | READY | READY_LIVE_BLOCKED | DEGRADED_NO_DATABASE | STOPPING
    accepting: bool = False
    market_ok: Optional[bool] = None
    events: list[str] = field(default_factory=list)   # ordered lifecycle log (tests assert the sequence)

    def _step(self, name: str, ok: bool, detail: str = "", critical: bool = True) -> bool:
        self.steps.append(StartupStep(name, ok, detail, critical))
        self.events.append(f"startup:{name}:{'ok' if ok else 'FAILED'}")
        if not ok:
            log.error("startup step %s failed: %s", name, detail)
        return ok

    # ------------------------------------------------------------------ startup
    async def startup(self) -> str:
        self.status, self.steps, self.events = "STARTING", [], []
        # 1 configuration
        if not self._step("config", bool(self.config.db_target), "no database configured"):
            self.status = "DEGRADED_NO_DATABASE"
            return self.status
        # 2 database
        try:
            self.rt = self.rt or Runtime(self.config.db_target)
            db = self.rt.db
            with db.read() as c:
                c.execute("SELECT 1").fetchone()
            self._step("database", True)
        except (DatabaseUnavailable, Exception) as e:
            self._step("database", False, type(e).__name__)
            self.status = "DEGRADED_NO_DATABASE"
            self.accepting = True   # health/inspection routes can still answer
            return self.status
        safety = self.rt.safety
        safety.vault = self.vault
        # 3 schema
        try:
            with db.read() as c:
                ver = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            missing = REQUIRED_TABLES - db.table_names()
            self._step("schema", ver == max(v for v, _, _ in MIGRATIONS) and not missing, f"version={ver} missing={sorted(missing)}")
        except Exception as e:
            self._step("schema", False, type(e).__name__)
        # 4 safety state
        ok = safety.baseline_intact()
        self._step("safety_state", ok, "" if ok else "baseline safety records missing: trading blocked")
        # engine + services
        self.engine = ExecutionEngine(db, safety, self.adapters, audit=self.rt.audit, exchange_ids=self.exchange_ids,
                                      quote_guard=QuoteGuardConfig.from_env())
        self.recon = ReconciliationService(db, safety, self.engine, self.rt.audit)
        self.lease = WorkerLease(db, "execution", ttl=self.config.lease_ttl)
        self.engine.submit_authorized = self.lease.check            # checked against the database immediately before every LIVE send
        self.rt.engine, self.rt.recon, self.rt.application = self.engine, self.recon, self
        # 4b audit-log hash chain (a broken chain closes the live gate and opens a CRITICAL incident)
        try:
            res = safety.verify_audit("startup")
            self._step("audit_integrity", res["ok"], "" if res["ok"] else f"{res['reason']} (row {res['first_bad_id']})")
        except Exception as e:
            self._step("audit_integrity", False, type(e).__name__)
        # 4c execution lease (only one process may submit live orders)
        try:
            got = self.lease.acquire()
            self._step("execution_lease", got, "" if got else "another worker holds the execution lease")
        except Exception as e:
            self._step("execution_lease", False, type(e).__name__)
        # 4d imported strategy packages: re-register eligible ones after verifying their fingerprints (tampered ones are quarantined)
        try:
            problems = self.rt.packages.sync_registry()
            self._step("strategy_packages", not problems, "; ".join(problems)[:300], critical=False)
        except Exception as e:
            self._step("strategy_packages", False, type(e).__name__, critical=False)
        # 5 strategy fingerprints
        try:
            StrategyRegistryStore(db).verify(all_strategies())
            self._step("strategy_fingerprints", True)
        except StrategyVersionConflict as e:
            self._step("strategy_fingerprints", False, str(e))
        # 6 recover incomplete orders (queries the exchange; never blindly resends)
        try:
            await self.engine.recover()
            with db.read() as c:
                left = c.execute("SELECT COUNT(*) FROM orders WHERE state IN ('SUBMITTING','UNKNOWN','RECONCILING')").fetchone()[0]
            self._step("recover_orders", left == 0, f"{left} order(s) still unresolved" if left else "")
        except Exception as e:
            self._step("recover_orders", False, type(e).__name__)
        # 7 recover positions
        try:
            n = PositionStore(db).mark_incomplete_for_reconciliation()
            self._step("recover_positions", True, f"{n} position(s) queued for reconciliation" if n else "")
        except Exception as e:
            self._step("recover_positions", False, type(e).__name__)
        # 8 reconcile live accounts
        for ex in sorted(n for n, a in self.adapters.items() if a.environment == "LIVE"):
            try:
                r = await self.recon.run(ex)
                self._step(f"reconcile:{ex}", r["state"] == "HEALTHY", r["state"])
            except Exception as e:
                self._step(f"reconcile:{ex}", False, type(e).__name__)
        # 9 risk state
        bad = self._validate_risk_state(db)
        if bad:
            safety.set_risk_healthy(False, by="startup")
        self._step("risk_state", not bad, "; ".join(bad))
        # 10/11 workers + health monitoring
        if self.config.workers_enabled:
            self._build_workers()
            self.supervisor.start()
            self._step("workers", True, critical=False)
            self.events.append("startup:health_monitor:started")
        else:
            # no reconciliation/heartbeat loop is running, so live orders must not be allowed: fail closed at the gate
            safety.workers_healthy = lambda: (False, "workers disabled by configuration")
            self._step("workers", False, "workers disabled by configuration (live execution stays blocked)", critical=False)
        # outcome: any failed critical step blocks live
        crit_ok = all(s.ok for s in self.steps if s.critical)
        safety.on_startup(crit_ok)
        if not crit_ok:
            safety.block_live("startup verification failed")
        self.status = "READY" if crit_ok else "READY_LIVE_BLOCKED"
        try:      # persisted so the Live Activity feed can show engine start-ups (and a degraded one) after a restart
            self.rt.audit.record("ENGINE_STARTUP", "OK" if crit_ok else "DEGRADED", "startup",
                                 metadata={"status": self.status, "failed_steps": [s.name for s in self.steps if not s.ok][:20]})
        except Exception:
            pass
        self.accepting = True
        self.events.append(f"startup:{self.status}")
        return self.status

    @staticmethod
    def _validate_risk_state(db) -> list[str]:
        bad = []
        with db.read() as c:
            for r in c.execute("SELECT * FROM risk_state"):
                vals = [r["day_start_equity"], r["peak_equity"], r["current_equity"], r["daily_realized_pnl"]]
                if any((not isinstance(v, (int, float))) or not math.isfinite(v) for v in vals) or r["current_equity"] <= 0 or r["peak_equity"] < r["current_equity"] - 1e-9 \
                        or r["consecutive_losses"] < 0:
                    bad.append(f"risk scope {r['scope']} has invalid values")
        return bad

    # ------------------------------------------------------------------ workers
    def _build_workers(self) -> None:
        cfg, rt, eng = self.config, self.rt, self.engine
        sup = self.supervisor = WorkerSupervisor(rt.db, rt.safety)
        live = lambda: sorted(n for n, a in self.adapters.items() if a.environment == "LIVE")

        async def order_recovery():
            await eng.recover()

        async def reconcile():
            for ex in live():
                await self.recon.run(ex)

        async def risk_monitor():
            rs = RiskStateStore(rt.db)
            for bad in self._validate_risk_state(rt.db):
                rt.safety.set_risk_healthy(False, by="risk_monitor")
                rt.safety.alert(bad)

        async def health_monitor():
            sup.recompute_degraded()

        async def market_data():
            if self.state_provider is None:
                self.market_ok = None
                return
            with rt.db.read() as c:
                wanted = {r["symbol"]: r["timeframe"] for r in c.execute("SELECT DISTINCT symbol, timeframe FROM bots WHERE enabled=1")}
            wanted.setdefault("BTC/USDT", "15m")
            for sym, tf in sorted(wanted.items()):
                state = await self.state_provider(sym, tf)
                if state.symbol != sym:
                    raise ValueError(f"market state symbol does not match configured symbol {sym}")
                validate_candles(state.candles, tf, datetime.now(timezone.utc))
                rt.safety.mark_market_data_fresh(sym, state.candles[-1].t, tf)
            self.market_ok = True

        async def lease_renew():
            if not self.lease.renew():
                raise RuntimeError("lost the execution lease")

        async def audit_verify():
            if not rt.safety.verify_audit()["ok"]:
                raise RuntimeError("audit log integrity check failed")

        async def bots():
            if self.state_provider is None:
                return
            runner = BotRunner(eng, RiskEngine(kill_switch_active=rt.safety.kill_switch_active), RiskStateStore(rt.db), self.state_provider, rt.audit)
            with rt.db.read() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM bots WHERE enabled=1")]
            for b in rows:
                await runner.tick(BotConfig(b["bot_id"], b["strategy_id"], b["strategy_version"], b["symbol"], b["timeframe"],
                                            b["environment"], b["exchange"], b["risk_scope"]))

        sup.add("order_recovery", order_recovery, cfg.recover_interval)
        sup.add("reconciliation", reconcile, cfg.recon_interval)       # covers orders, positions and balances per live exchange
        sup.add("risk_monitor", risk_monitor, cfg.risk_interval)
        sup.add("health_monitor", health_monitor, cfg.health_interval)
        sup.add("bot_runner", bots, cfg.bot_interval)
        sup.add("market_data", market_data, cfg.market_interval, critical=False)
        sup.add("execution_lease", lease_renew, max(cfg.lease_ttl / 3, 0.05))
        sup.add("audit_integrity", audit_verify, cfg.audit_verify_interval, critical=False)
        rt.safety.workers_healthy = sup.healthy
        rt.supervisor = sup

    # ------------------------------------------------------------------ shutdown
    async def shutdown(self) -> dict:
        self.status, self.accepting = "STOPPING", False          # 1. stop accepting new work
        self.events.append("shutdown:stop_accepting")
        report: dict = {}
        if self.supervisor is not None:                           # 2/3. stop workers (waits for running iterations)
            report["workers"] = await self.supervisor.stop()
            self.events.append("shutdown:workers_stopped")
        if getattr(self, "lease", None) is not None and self.rt is not None:
            try:
                self.lease.release()                              # let another worker take over immediately
            except Exception:
                pass
        self.events.append("shutdown:state_persisted")            # 4/5. state is persisted per transaction; nothing buffered in memory
        for name, a in self.adapters.items():                     # 6. close exchange connections
            try:
                await a.disconnect()
            except Exception:
                report.setdefault("adapter_errors", []).append(name)
        self.events.append("shutdown:adapters_closed")
        if self.rt is not None:
            try:
                self.rt.audit.record("ENGINE_SHUTDOWN", "OK", "shutdown", metadata={"adapter_errors": report.get("adapter_errors", [])})
            except Exception:
                pass
        if self.rt is not None:                                   # 7. close the database
            self.rt.db.close()
        self.events.append("shutdown:database_closed")
        self.status = "STOPPED"
        return report

    def health(self) -> dict:
        if self.rt is None:
            return {"database": "unhealthy", "live_trading": "blocked", "kill_switch": True}
        h = health_report(self.rt.db, self.rt.safety, self.supervisor, self.vault, self.market_ok,
                          startup={"status": self.status, "steps": [{"name": s.name, "ok": s.ok} for s in self.steps]})
        return h
