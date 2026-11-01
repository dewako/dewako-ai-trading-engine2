"""Phase 3.3: production configuration never falls back, HTTP policy helpers, and schema upgrade from the previous version.
Framework-free tests: they run anywhere. (The same policies are exercised over real HTTP in test_api_testclient.py, which needs FastAPI.)"""
import os
import tempfile

import pytest

import dewako.persistence.db as dbmod
from dewako.api.http_policy import SECURITY_HEADERS, cors_origins
from dewako.persistence.db import ConfigError, Database, DatabaseUnavailable, database_target_from_env
from dewako.persistence.migrations import MIGRATIONS
from dewako.runtime.application import Application, Config
from hardening_helpers import *


# ------------------------------------------------------------------ production never silently falls back
def test_production_requires_a_postgres_url_and_never_falls_back_to_sqlite():
    for env in ({"DEWAKO_ENV": "production"},
                {"DEWAKO_ENV": "production", "DEWAKO_DATABASE_URL": "/tmp/x.db"},
                {"DEWAKO_ENV": "production", "DEWAKO_TRADING_DB": "/tmp/x.db"},
                {"DEWAKO_ENV": "PRODUCTION", "DEWAKO_DATABASE_URL": "sqlite:///x.db"}):
        with pytest.raises(ConfigError):
            database_target_from_env(env)
    assert database_target_from_env({"DEWAKO_ENV": "production", "DEWAKO_DATABASE_URL": "postgresql://u:p@h/db"}).startswith("postgresql://")
    assert database_target_from_env({}).endswith(".db")                          # development default is allowed
    with pytest.raises(ConfigError):
        os.environ["DEWAKO_ENV"] = "production"
        try:
            Config.from_env()
        finally:
            os.environ.pop("DEWAKO_ENV", None)


def test_unreachable_postgres_makes_startup_fail_closed_not_fall_back():
    app = Application(Config(db_target="postgresql://nobody:nothing@127.0.0.1:1/none", workers_enabled=False))
    status = run(app.startup())
    assert not any(s.ok for s in app.steps if s.name == "database")
    assert status == "DEGRADED_NO_DATABASE"
    h = app.health()
    assert h["database"] == "unhealthy" and h["live_trading"] == "blocked" and h["kill_switch"] is True


# ------------------------------------------------------------------ http policy
def test_security_headers_and_cors_policy():
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff" and SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert "no-store" in SECURITY_HEADERS["Cache-Control"] and "default-src 'none'" in SECURITY_HEADERS["Content-Security-Policy"]
    assert cors_origins({}) == []                                                # default: same-origin only
    assert cors_origins({"DEWAKO_CORS_ORIGINS": "https://a.example/, http://localhost:5173"}) == ["https://a.example", "http://localhost:5173"]
    for bad in ("*", "https://a.example,*", "javascript:alert(1)", "a.example"):
        with pytest.raises(ValueError):
            cors_origins({"DEWAKO_CORS_ORIGINS": bad})


# ------------------------------------------------------------------ upgrade from the previous schema version
def test_upgrade_from_previous_schema_keeps_data_and_adds_new_tables():
    latest = MIGRATIONS[-1][0]
    assert latest >= 6
    old = [m for m in MIGRATIONS if m[0] < 5]                                    # the release before the audit hash chain and worker leases (schema 4)
    real = dbmod.MIGRATIONS
    dbmod.MIGRATIONS = old                                                       # a database as the previous release left it
    try:
        db, target = tmp_db()
        from dewako.execution.order_store import OrderStore
        with db.tx() as c:                                                       # data written by the previous release's schema
            OrderStore(db).create(bot_id="b", signal_id="old-sig", intent="ENTRY", environment="LIVE", exchange="fake", symbol="BTC/USDT",
                                  side="BUY", order_type="MARKET", quantity=1, price=100, correlation_id="old-cid", conn=c)
            c.execute("INSERT INTO audit_log(created_at,user_id,username,role,action,resource,result,correlation_id,metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                      (dbmod.now_iso(), None, "system", "SYSTEM", "LEGACY_EVENT", "", "OK", "old-cid", "{}"))
        assert "worker_leases" not in db.table_names()
        with db.read() as c:
            assert "row_hash" not in [r[1] for r in c.execute("PRAGMA table_info(audit_log)")] if not db.is_pg else True
            before = (c.execute("SELECT COUNT(*) FROM orders").fetchone()[0], c.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                      c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
        assert before[3] == old[-1][0]
        db.close()
    finally:
        dbmod.MIGRATIONS = real
    db2 = Database(target)                                                       # the new release opens the old database
    assert {"worker_leases", "risk_reservations", "incidents", "strategy_packages", "ai_settings"} <= set(db2.table_names())
    with db2.read() as c:
        after = (c.execute("SELECT COUNT(*) FROM orders").fetchone()[0], c.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                 c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    assert after[:3] == before[:3] and after[3] == latest                        # data intact, version advanced
    assert db2.migrate() == []                                                   # idempotent: nothing left to apply
    # the legacy audit row survived, is reported as legacy (unchained), and the chain that starts after the upgrade verifies
    safety = SafetyController(db2)
    eid = make_live_ready(db2, safety)
    res = safety.audit.verify_chain()
    assert res["ok"] and res["legacy_rows"] == 1 and res["rows"] >= 1
    with pytest.raises(Exception):                                               # and the old rows are now immutable too
        with db2.tx() as c:
            c.execute("UPDATE audit_log SET action='X' WHERE action='LEGACY_EVENT'")
    # the upgraded database still works end to end: a fresh signal executes and reserves capacity
    eng2, _ = engine_for(db2, FakeLive(), exchange_id=eid)
    assert run(eng2.execute(decision("post-upgrade"), bot_id="b2", environment="LIVE", exchange="fake")).outcome == "EXECUTED"


def test_upgrade_from_schema_5_adds_later_tables_and_keeps_data():
    real = dbmod.MIGRATIONS
    dbmod.MIGRATIONS = [m for m in MIGRATIONS if m[0] <= 5]
    try:
        db, target = tmp_db()
        assert "strategy_packages" not in db.table_names()
        eid_db = db
        safety = SafetyController(db)
        make_live_ready(db, safety)
        with db.read() as c:
            before = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        db.close()
    finally:
        dbmod.MIGRATIONS = real
    db2 = Database(target)
    assert {"strategy_packages", "ai_settings", "ai_credentials"} <= set(db2.table_names())
    with db2.read() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= before
        assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == MIGRATIONS[-1][0]
    assert db2.migrate() == [] and SafetyController(db2).audit.verify_chain()["ok"]


def test_clean_database_applies_every_migration_in_order_once():
    db, _ = tmp_db()
    with db.read() as c:
        versions = [r[0] for r in c.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == [m[0] for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
