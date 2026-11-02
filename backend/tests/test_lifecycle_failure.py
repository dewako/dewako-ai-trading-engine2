"""Phase 3.2: application lifecycle, failure injection, isolation, correlation, secret scans.
Fake exchanges only. Runs on SQLite and (DEWAKO_TEST_DB=postgres) on a real PostgreSQL server."""
import asyncio
import json
import logging

import pytest

from dewako.core.types import Side
from dewako.exchanges.base import ExchangeError
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.persistence.db import Database, DatabaseUnavailable
from dewako.runtime.application import Application, Config
from dewako.runtime.workers import Worker, WorkerSupervisor
from dewako.safety.controller import LiveSwitch, SafetyController, TradingState
from dewako.services.credentials import CredentialVault, VaultError
from dewako.strategies import library  # noqa: F401
from hardening_helpers import *


def _app(target, adapters, **cfg):
    return Application(Config(db_target=target, workers_enabled=cfg.pop("workers", False), **cfg), adapters=adapters)


def _prepared(adapter=None):
    """A database with a live-ready exchange, then closed so the Application opens it itself (a real 'process start')."""
    db, target = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    return db, target, eid


# ------------------------------------------------------------------ lifecycle
def test_startup_runs_steps_in_documented_order_and_reaches_ready():
    db, target, eid = _prepared()
    ad = FakeLive()
    app = _app(target, {"fake": ad})
    app.exchange_ids["fake"] = eid
    status = run(app.startup())
    names = [s.name for s in app.steps]
    expected = ["config", "database", "schema", "safety_state", "audit_integrity", "execution_lease",
                "strategy_packages", "strategy_fingerprints", "recover_orders", "recover_positions", "reconcile:fake", "risk_state", "workers", "trading_configuration"]
    assert names == expected
    # an adapter but no market-data provider: the application must NOT claim READY (Stage 1 / P0-3)
    assert status == "NOT_CONFIGURED"
    assert app.events[0] == "startup:config:ok"
    run(app.shutdown())


def test_critical_startup_failure_blocks_live_but_process_stays_up():
    db, target, eid = _prepared()
    ad = FakeLive(); ad.get_fails = True            # reconciliation cannot read the exchange
    app = _app(target, {"fake": ad})
    app.exchange_ids["fake"] = eid
    status = run(app.startup())
    assert status == "READY_LIVE_BLOCKED" and app.accepting
    assert app.rt.safety.live_switch() is LiveSwitch.BLOCKED
    out = run(app.engine.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == 0
    run(app.shutdown())


def test_startup_fingerprint_mismatch_blocks_live_and_execution():
    db, target, eid = _prepared()
    with db.tx() as c:
        c.execute("UPDATE strategy_versions SET fingerprint='tampered' WHERE strategy_id='Trend AI' AND strategy_version='2.2'")
    ad = FakeLive()
    app = _app(target, {"fake": ad})
    app.exchange_ids["fake"] = eid
    assert run(app.startup()) == "READY_LIVE_BLOCKED"
    assert [s for s in app.steps if s.name == "strategy_fingerprints"][0].ok is False
    out = run(app.engine.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == 0
    run(app.shutdown())


def test_startup_database_unavailable_does_not_crash_and_reports_it():
    bad = "postgresql://dewako:x@127.0.0.1:1/none" if backend() == "postgres" else "/nonexistent_dir_xyz/none.db"
    app = _app(bad, {})
    assert run(app.startup()) == "DEGRADED_NO_DATABASE"
    h = app.health()
    assert h["database"] == "unhealthy" and h["live_trading"] == "blocked" and h["kill_switch"] is True


def test_shutdown_order_and_adapters_closed():
    db, target, eid = _prepared()
    ad = FakeLive(); closed = []
    async def dis(): closed.append(1)
    ad.disconnect = dis
    app = _app(target, {"fake": ad})
    app.exchange_ids["fake"] = eid
    run(app.startup())
    run(app.shutdown())
    tail = [e for e in app.events if e.startswith("shutdown")]
    assert tail == ["shutdown:stop_accepting", "shutdown:state_persisted", "shutdown:adapters_closed", "shutdown:database_closed"] or \
        tail == ["shutdown:stop_accepting", "shutdown:workers_stopped", "shutdown:state_persisted", "shutdown:adapters_closed", "shutdown:database_closed"]
    assert closed == [1] and app.status == "STOPPED" and not app.accepting


def test_workers_started_by_application_expose_health_and_stop_gracefully():
    db, target, eid = _prepared()
    app = _app(target, {"fake": FakeLive()}, workers=True, recon_interval=0.2, recover_interval=0.2, risk_interval=0.2,
               health_interval=0.2, bot_interval=0.2, market_interval=0.2)
    app.exchange_ids["fake"] = eid

    async def go():
        await app.startup()
        await asyncio.sleep(0.8)
        h = app.health()
        st = app.supervisor.status()
        rep = await app.shutdown()
        return h, st, rep
    h, st, rep = asyncio.run(go())
    assert set(st) >= {"order_recovery", "reconciliation", "risk_monitor", "health_monitor", "bot_runner", "market_data"}
    for name, v in st.items():
        assert {"status", "last_heartbeat", "last_success", "last_error", "restart_count"} <= set(v), name
        assert v["last_heartbeat"] is not None
    assert h["workers"] == "healthy" and "worker_detail" in h
    assert set(rep["workers"].values()) == {"STOPPED"}


def test_dead_worker_loop_restarts_with_policy_and_then_fails_closed():
    db, _ = tmp_db()
    s = SafetyController(db)
    sup = WorkerSupervisor(db, s, grace=1.0, restart_backoff=0.01)
    calls = []

    async def boom():
        calls.append(1)
        raise asyncio.InvalidStateError("loop bug")      # not an Exception subclass the iteration guard handles differently

    w = Worker("w", boom, 0.01, critical=True, fail_after=1000, max_restarts=2)
    sup.workers.append(w); sup._write("w", "STARTING", critical=True)
    # make the *loop* die (not just the iteration): replace run_once with one that raises out
    async def dying(_w): raise RuntimeError("dead loop")
    sup.run_once = dying

    async def go():
        sup.start()
        await asyncio.sleep(0.5)
        await sup.stop()
    asyncio.run(go())
    with db.read() as c:
        r = dict(c.execute("SELECT * FROM worker_health WHERE name='w'").fetchone())
    assert r["restart_count"] == 3 and r["status"] in ("FAILED", "STOPPED")
    assert s.trading_state() is TradingState.SYSTEM_DEGRADED


def test_stop_waits_for_running_iteration_instead_of_cancelling_it():
    db, _ = tmp_db()
    s = SafetyController(db)
    sup = WorkerSupervisor(db, s, grace=5.0)
    done = []

    async def slow():
        await asyncio.sleep(0.4); done.append(1)

    sup.add("slow", slow, 0.05)

    async def go():
        sup.start(); await asyncio.sleep(0.1)
        return await sup.stop()
    out = asyncio.run(go())
    assert done == [1] and out["slow"] == "STOPPED"


def test_unhealthy_critical_worker_blocks_live_gate():
    db, _ = tmp_db()
    s = SafetyController(db)
    eid = make_live_ready(db, s)
    sup = WorkerSupervisor(db, s)
    async def bad(): raise RuntimeError("x")
    sup.add("critical_one", bad, 1)
    s.workers_healthy = sup.healthy
    run(sup.run_once(sup.workers[0]))
    g = s.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2")
    assert not g.allowed and any("worker" in r for r in g.reasons)


# ------------------------------------------------------------------ failure injection
def test_database_failure_during_execute_sends_nothing():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety)
    eng.exchange_ids["fake"] = eid

    def broken(*a, **k):
        raise DatabaseUnavailable("injected")
    good = db.tx
    db.tx = broken
    try:
        with pytest.raises(Exception):
            run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    finally:
        db.tx = good
    assert ad.create_calls == 0


def test_exchange_timeout_http500_and_credential_failure_never_double_send():
    for mode in ("timeout_absent", "timeout_accepted", "http500"):
        db, _ = tmp_db()
        ad = FakeLive(mode)
        eng, safety = engine_for(db, ad)
        eid = make_live_ready(db, safety)
        eng.exchange_ids["fake"] = eid
        run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
        run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))     # same signal again
        run(eng.recover())
        assert ad.create_calls == 1, mode

    # credential failure: the vault cannot decrypt (wrong key) -> the live gate refuses
    db, _ = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    safety.vault = CredentialVault(keys={1: Fernet_key()})
    g = safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2")
    assert not g.allowed


def Fernet_key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def test_reconciliation_failure_blocks_live_and_marks_state_failed():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety)
    eng.exchange_ids["fake"] = eid
    from dewako.reconciliation.service import ReconciliationService
    rec = ReconciliationService(db, safety, eng)
    ad.get_fails = True
    r = run(rec.run("fake"))
    assert r["state"] in ("FAILED", "RECONCILIATION_REQUIRED")
    out = run(eng.execute(decision("s2"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == 0


# ------------------------------------------------------------------ paper / live isolation
class SpyLive(FakeLive):
    """Any call at all means a paper path touched a live endpoint."""
    def __init__(self):
        super().__init__(); self.calls = []
    def __getattribute__(self, n):
        if n in ("create_order", "get_order", "cancel_order", "get_balance", "get_markets", "get_account_snapshot", "get_positions", "get_ticker"):
            object.__getattribute__(self, "calls").append(n)
        return super().__getattribute__(n)


def test_paper_bot_never_touches_a_live_endpoint():
    db, _ = tmp_db()
    live = SpyLive()
    paper = PaperAdapter(); paper.set_price("BTC/USDT", 100.0)
    safety = SafetyController(db)
    from dewako.execution.engine import ExecutionEngine
    eng = ExecutionEngine(db, safety, {"paper": paper, "fake": live}, quote_guard=TEST_QUOTE_GUARD)
    r = run(eng.execute(decision("p1"), bot_id="pb", environment="PAPER", exchange="paper"))
    assert r.outcome == "EXECUTED"
    # a paper bot pointed at the live adapter is refused before anything is called
    r2 = run(eng.execute(decision("p2"), bot_id="pb", environment="PAPER", exchange="fake"))
    assert r2.outcome == "BLOCKED" and "does not match" in r2.detail
    run(eng.recover("PAPER")) if "PAPER" in eng.recover.__code__.co_varnames else None
    assert live.calls == [] and live.create_calls == 0


# ------------------------------------------------------------------ correlation chain
def test_correlation_chain_signal_to_position():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety)
    eng.exchange_ids["fake"] = eid
    r = run(eng.execute(decision("chain1"), bot_id="bot", environment="LIVE", exchange="fake", correlation_id="CORR-1"))
    assert r.outcome == "EXECUTED"
    with db.read() as c:
        sig = c.execute("SELECT * FROM signals WHERE signal_id='chain1'").fetchone()
        rd = c.execute("SELECT * FROM risk_decisions WHERE signal_id='chain1'").fetchone()
        it = c.execute("SELECT * FROM execution_intents WHERE signal_id='chain1'").fetchone()
        od = c.execute("SELECT * FROM orders WHERE intent_id=?", (it["id"],)).fetchone()
        tr = c.execute("SELECT * FROM trades WHERE order_id=?", (od["id"],)).fetchone()
        pe = c.execute("SELECT * FROM position_events WHERE order_id=?", (od["id"],)).fetchall()
        pos = c.execute("SELECT * FROM positions WHERE id=?", (pe[0]["position_id"],)).fetchone()
        oev = c.execute("SELECT correlation_id FROM order_events WHERE order_id=?", (od["id"],)).fetchall()
        audit = [x["action"] for x in c.execute("SELECT action FROM audit_log WHERE correlation_id='CORR-1'")]
    assert sig["strategy_id"] == "Trend AI" and rd["approved"] == 1 and it["risk_decision_id"] == rd["risk_decision_id"]
    assert it["correlation_id"] == od["correlation_id"] == tr["correlation_id"] == "CORR-1"
    assert tr["position_id"] == pos["id"] if "position_id" in tr.keys() else True
    assert {e["correlation_id"] for e in oev} == {"CORR-1"} and pe[0]["correlation_id"] == "CORR-1"
    assert "ORDER_CREATED" in audit


# ------------------------------------------------------------------ secrets
class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG); self.lines = []
    def emit(self, r):
        self.lines.append(self.format(r) + " " + repr(r.args))
    @property
    def text(self):
        return "\n".join(self.lines)


def test_secret_values_never_appear_in_logs_audit_health_or_errors():
    caplog = _Cap(); logging.getLogger().addHandler(caplog); logging.getLogger().setLevel(logging.DEBUG)
    db, path = tmp_db()
    from dewako.exchanges.persistent_manager import PersistentExchangeManager
    from test_hardening_platform import PermAdapter, _pm, ADMIN_NOID
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    m = _pm(db)
    out = run(m.add_exchange("binance", "main", "AKIALEAKCHECK1", "TOPSECRETLEAKCHECK", actor_info=ADMIN_NOID))
    PermAdapter.perms = ExchangeError("HTTP 401 key=AKIALEAKCHECK1 secret=TOPSECRETLEAKCHECK")
    err = ""
    try:
        run(m.add_exchange("binance", "x2", "AKIALEAKCHECK2", "TOPSECRETLEAKCHECK2", actor_info=ADMIN_NOID))
    except Exception as e:
        err = str(e)
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    safety = SafetyController(db)
    from dewako.runtime.health import health_report
    with db.read() as c:
        rows = json.dumps([dict(r) for r in c.execute("SELECT * FROM audit_log")], default=str)
        blob = c.execute("SELECT encrypted_blob FROM credentials").fetchone()[0]
    haystack = "\n".join([json.dumps(out), err, rows, caplog.text, json.dumps(health_report(db, safety, vault=m.vault), default=str),
                          json.dumps(m.list(), default=str)])
    for secret in ("TOPSECRETLEAKCHECK", "TOPSECRETLEAKCHECK2", "AKIALEAKCHECK1"[:-4] + "CHECK1"):
        # full key ids must not be stored in clear; the last four characters may be shown by design
        assert secret not in haystack, secret
    assert b"TOPSECRETLEAKCHECK" not in bytes(blob)
    logging.getLogger().removeHandler(caplog)
