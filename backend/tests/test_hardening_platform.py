import asyncio
import json
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

import scenarios as S
from dewako.auth.service import (AuthError, AuthService, Forbidden, PERMISSIONS, RateLimited, RateLimiter, hash_password, verify_password)
from dewako.core.types import Side
from dewako.exchanges.base import ExchangeError
from dewako.exchanges.persistent_manager import PersistentExchangeManager
from dewako.exchanges.manager import ExchangeRejected
from dewako.exchanges.stubs import _StubAdapter
from dewako.execution.engine import ExecutionEngine
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database
from dewako.reconciliation.service import ReconciliationService
from dewako.risk.engine import RiskEngine
from dewako.risk.state import RiskStateStore
from dewako.runtime.bot_runner import BotConfig, BotRunner
from dewako.runtime.health import health_report
from dewako.runtime.startup import StrategyRegistryStore, startup
from dewako.runtime.workers import WorkerSupervisor
from dewako.safety.controller import LiveSwitch, SafetyController, TradingState
from dewako.services.credentials import CredentialVault, VaultError
from dewako.strategies import library  # noqa: F401
from dewako.strategies.base import all_strategies
from dewako.strategies.versions import StrategyVersionConflict
from hardening_helpers import *
import pytest

PW = "correct horse battery"
ADMIN_NOID = {"username": "alice", "role": "ADMIN"}


# ------------------------------------------------------------------ auth / RBAC
def _auth():
    db, path = tmp_db()
    a = AuthService(db)
    a.create_user(None, "root", PW, "ADMIN")
    return db, path, a


def test_password_hashing_is_salted_and_never_plaintext():
    h1, h2 = hash_password(PW), hash_password(PW)
    assert h1 != h2 and PW not in h1 and verify_password(PW, h1) and not verify_password("wrong", h1)
    db, path, a = _auth()
    with db.read() as c:
        assert PW not in c.execute("SELECT password_hash FROM users").fetchone()[0]


def test_first_user_bootstrap_then_only_admin_creates_users():
    db, path, a = _auth()
    tok = a.login("root", PW)
    admin = a.authenticate(tok)
    a.create_user(admin, "tina", PW + "x", "TRADER")
    trader = a.authenticate(a.login("tina", PW + "x"))
    with pytest.raises(Forbidden):
        a.create_user(trader, "evil", PW, "ADMIN")
    with pytest.raises(AuthError):
        a.create_user(None, "anon", PW, "ADMIN")
    with pytest.raises(ValueError):
        a.create_user(admin, "weak", "short", "VIEWER")


def test_login_failures_are_generic_and_lock_out():
    db, path, a = _auth()
    msgs = set()
    for u, p in (("nobody", "x" * 14), ("root", "wrong-password-1")):
        try:
            a.login(u, p)
        except AuthError as e:
            msgs.add(str(e))
    assert len(msgs) == 1
    for _ in range(5):
        with pytest.raises(AuthError):
            a.login("root", "bad-password-xyz")
    with pytest.raises(AuthError):  # now locked even with the right password
        a.login("root", PW)


def test_sessions_expire_revoke_and_store_only_hash():
    db, path, a = _auth()
    tok = a.login("root", PW)
    with db.read() as c:
        assert tok not in c.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert a.authenticate(tok)["role"] == "ADMIN"
    a.logout(tok)
    with pytest.raises(AuthError):
        a.authenticate(tok)
    tok2 = a.login("root", PW)
    with db.tx() as c:
        c.execute("UPDATE sessions SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    with pytest.raises(AuthError):
        a.authenticate(tok2)
    with pytest.raises(AuthError):
        a.authenticate("")


def test_role_matrix_enforced_server_side():
    db, path, a = _auth()
    admin = a.authenticate(a.login("root", PW))
    for name, role in (("v", "VIEWER"), ("an", "ANALYST"), ("t", "TRADER")):
        a.create_user(admin, name, PW + name, role)
    who = {r: a.authenticate(a.login(n, PW + n)) for n, r in (("v", "VIEWER"), ("an", "ANALYST"), ("t", "TRADER"))}
    who["ADMIN"] = admin
    admin_only = ["exchange_credentials", "risk_settings", "live_trading", "kill_switch_release", "user_management"]
    for perm in admin_only:
        for r in ("VIEWER", "ANALYST", "TRADER"):
            with pytest.raises(Forbidden):
                a.require(who[r], perm)
        a.require(who["ADMIN"], perm)
    with pytest.raises(Forbidden):
        a.require(who["VIEWER"], "market_data")
    a.require(who["ANALYST"], "backtests")
    with pytest.raises(Forbidden):
        a.require(who["ANALYST"], "paper_trading")
    a.require(who["TRADER"], "paper_trading")
    assert "live_trading" not in PERMISSIONS["TRADER"]
    assert any(r["action"] == "ACCESS_DENIED" for r in AuditLog(db).recent(100))


def test_role_change_revokes_sessions_and_admin_cannot_self_demote():
    db, path, a = _auth()
    admin = a.authenticate(a.login("root", PW))
    a.create_user(admin, "t", PW + "t", "ADMIN")
    tok = a.login("t", PW + "t"); t = a.authenticate(tok)
    a.set_role(admin, t["user_id"], "VIEWER")
    with pytest.raises(AuthError):
        a.authenticate(tok)
    with pytest.raises(Forbidden):
        a.set_role(admin, admin["user_id"], "VIEWER")


def test_rate_limiter():
    t = [0.0]
    rl = RateLimiter(3, 10, clock=lambda: t[0])
    for _ in range(3):
        rl.check("k")
    with pytest.raises(RateLimited):
        rl.check("k")
    t[0] = 11
    rl.check("k")


def test_only_trader_or_admin_can_trigger_kill_switch_and_only_admin_release():
    assert "kill_switch_trigger" in PERMISSIONS["TRADER"] and "kill_switch_trigger" not in PERMISSIONS["VIEWER"]
    assert "kill_switch_release" not in PERMISSIONS["TRADER"]


# ------------------------------------------------------------------ vault + persistent exchanges
K1, K2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()


class PermAdapter(_StubAdapter):
    name = "binance"
    perms = {"read": True, "trade": True, "withdraw": False}

    def __init__(self, **kw): pass
    async def connect(self): return None
    async def get_api_permissions(self):
        if isinstance(PermAdapter.perms, Exception):
            raise PermAdapter.perms
        return PermAdapter.perms


def _pm(db, vault=None):
    return PersistentExchangeManager(db, vault or CredentialVault(keys={1: K1}), factory=lambda ex, creds: PermAdapter())


def test_vault_key_versions_and_rotation_and_no_leak():
    v1 = CredentialVault(keys={1: K1})
    blob = v1.encrypt({"api_key": "KEYVALUE1234"})
    v2 = CredentialVault(keys={1: K1, 2: K2})
    assert v2.current_version == 2 and v2.decrypt(blob, 1)["api_key"] == "KEYVALUE1234"
    new, ver = v2.rotate(blob, 1)
    assert ver == 2 and CredentialVault(keys={2: K2}).decrypt(new)["api_key"] == "KEYVALUE1234"
    with pytest.raises(VaultError) as e:
        CredentialVault(keys={3: Fernet.generate_key().decode()}).decrypt(blob)
    assert "KEYVALUE" not in str(e.value) and K1 not in str(e.value)
    with pytest.raises(VaultError) as e:
        CredentialVault(keys={1: "not-a-key"})
    assert "not-a-key" not in str(e.value)


def test_persistent_exchange_survives_restart_encrypts_and_never_returns_secrets():
    db, path = tmp_db()
    m = _pm(db)
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    out = asyncio.run(m.add_exchange("binance", "main", "AKIA12345678", "SUPERSECRETVALUE", actor_info=ADMIN_NOID))
    assert "SUPERSECRETVALUE" not in json.dumps(out) and out["enabled"] is False
    db2 = Database(path)
    m2 = _pm(db2)
    listed = m2.list()
    assert len(listed) == 1 and "SUPERSECRETVALUE" not in json.dumps(listed) and listed[0]["api_key"].endswith("5678")
    with db2.read() as c:
        assert b"SUPERSECRETVALUE" not in c.execute("SELECT encrypted_blob FROM credentials").fetchone()[0]
        dump = json.dumps([dict(r) for r in c.execute("SELECT * FROM audit_log")])
    assert "SUPERSECRETVALUE" not in dump and "AKIA12345678" not in dump
    assert m2.credentials_for_adapter(1)["api_secret"] == "SUPERSECRETVALUE"


def test_withdraw_key_rejected_and_enable_rechecks_the_exchange():
    db, path = tmp_db()
    m = _pm(db)
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": True}
    with pytest.raises(ExchangeRejected):
        asyncio.run(m.add_exchange("binance", "x", "AKIA12345678", "SECRETSECRET", actor_info=ADMIN_NOID))
    assert m.list() == []
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    asyncio.run(m.add_exchange("binance", "x", "AKIA12345678", "SECRETSECRET", actor_info=ADMIN_NOID))
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": True}  # user later turned Withdraw on at the exchange
    with pytest.raises(ExchangeRejected):
        asyncio.run(m.enable_for_live(1, ADMIN_NOID))
    assert m.list()[0]["enabled"] is False
    with db.read() as c:
        assert c.execute("SELECT withdrawal_permission FROM credentials").fetchone()[0] == 1
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}


def test_unverifiable_key_rejected_and_nothing_stored():
    db, _ = tmp_db()
    m = _pm(db)
    PermAdapter.perms = ExchangeError("unreachable")
    with pytest.raises(ExchangeRejected):
        asyncio.run(m.add_exchange("binance", "x", "AKIA12345678", "SECRETSECRET", actor_info=ADMIN_NOID))
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0


def test_credential_rotation_and_reencrypt_all():
    db, path = tmp_db()
    PermAdapter.perms = {"read": True, "trade": True, "withdraw": False}
    m = _pm(db)
    asyncio.run(m.add_exchange("binance", "x", "AKIA12345678", "SECRETSECRET", actor_info=ADMIN_NOID))
    m2 = _pm(db, CredentialVault(keys={1: K1, 2: K2}))
    assert m2.reencrypt_all(ADMIN_NOID) == 1 and m2.list()[0]["key_version"] == 2
    assert m2.credentials_for_adapter(1)["api_secret"] == "SECRETSECRET"
    asyncio.run(m2.rotate_credentials(1, "AKIANEWKEY999", "NEWSECRET9999", "", ADMIN_NOID))
    assert m2.credentials_for_adapter(1)["api_secret"] == "NEWSECRET9999"
    acts = {r["action"] for r in AuditLog(db).recent(50)}
    assert {"CREDENTIAL_CREATED", "CREDENTIAL_ROTATED"} <= acts


# ------------------------------------------------------------------ strategy registry persistence
def test_strategy_fingerprint_change_marks_invalid_and_fails_startup():
    db, _ = tmp_db()
    reg = StrategyRegistryStore(db)
    reg.verify(all_strategies())
    with db.read() as c:
        st = {(r["strategy_id"], r["strategy_version"]): r["release_status"] for r in c.execute("SELECT * FROM strategy_versions")}
    assert st[("AI Strategy", "0.3")] == "DRAFT" and st[("Trend AI", "2.2")] == "RELEASED"
    with db.tx() as c:
        c.execute("UPDATE strategy_versions SET fingerprint='tampered' WHERE strategy_id='Trend AI' AND strategy_version='2.2'")
    with pytest.raises(StrategyVersionConflict):
        reg.verify(all_strategies())
    with db.read() as c:
        r = c.execute("SELECT release_status, enabled FROM strategy_versions WHERE strategy_id='Trend AI' AND strategy_version='2.2'").fetchone()
    assert r["release_status"] == "INVALID" and r["enabled"] == 0
    reg.verify([c for c in all_strategies() if c.name != "Trend AI"])  # still invalid afterwards
    with db.read() as c:
        assert c.execute("SELECT release_status FROM strategy_versions WHERE strategy_id='Trend AI' AND strategy_version='2.2'").fetchone()[0] == "INVALID"


# ------------------------------------------------------------------ reconciliation + startup
def _live_env():
    db, path = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    rec = ReconciliationService(db, safety, eng)
    return db, path, ad, eng, safety, rec


def test_clean_reconciliation_after_trade():
    db, path, ad, eng, safety, rec = _live_env()
    run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    assert run(rec.run("fake"))["result"] == "OK"
    assert safety.trading_state() is TradingState.SYSTEM_HEALTHY


def test_position_on_exchange_but_local_db_stale_blocks_trading():
    """Test C."""
    db, path, ad, eng, safety, rec = _live_env()
    run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    with db.tx() as c:  # local state lost / stale
        c.execute("UPDATE trades SET position_id=NULL")   # foreign keys now protect position history, so remove it explicitly
        c.execute("DELETE FROM position_events")
        c.execute("DELETE FROM positions")
    r = run(rec.run("fake"))
    assert r["result"] == "MISMATCH" and any(f["type"] == "position_unexpected" for f in r["findings"])
    assert safety.trading_state() is TradingState.RECONCILIATION_REQUIRED
    assert safety.live_switch() is LiveSwitch.BLOCKED
    out = run(eng.execute(decision("another"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == 1


def test_unexpected_and_missing_orders_detected():
    db, path, ad, eng, safety, rec = _live_env()
    ad.mode = "open"
    r = run(eng.execute(decision("a"), bot_id="b", environment="LIVE", exchange="fake"))
    cid = eng.orders.get(r.order_id)["client_order_id"]
    ad.store["manual-order"] = ad.store[cid].model_copy(update={"client_order_id": "manual-order"})
    res = run(rec.run("fake"))
    assert any(f["type"] == "order_only_remote" for f in res["findings"])
    del ad.store["manual-order"]; del ad.store[cid]
    res = run(rec.run("fake"))
    assert any(f["type"] == "order_only_local" for f in res["findings"])
    # quantity mismatch
    db2, _, ad2, eng2, s2, rec2 = _live_env()
    ad2.mode = "partial"
    r2 = run(eng2.execute(decision("p", qty=2.0), bot_id="b", environment="LIVE", exchange="fake"))
    c2 = eng2.orders.get(r2.order_id)["client_order_id"]
    ad2.store[c2] = ad2.store[c2].model_copy(update={"filled": 1.5})
    assert any(f["type"] == "order_quantity_mismatch" for f in run(rec2.run("fake"))["findings"])


def test_exchange_unreachable_is_error_and_blocks():
    db, path, ad, eng, safety, rec = _live_env()
    async def boom(*a, **k): raise ExchangeError("down")
    ad.get_open_orders = boom
    assert run(rec.run("fake"))["result"] == "ERROR"
    assert safety.trading_state() is TradingState.RECONCILIATION_REQUIRED


def test_reconciliation_recovers_after_fix_and_never_cancels():
    db, path, ad, eng, safety, rec = _live_env()
    ad.mode = "open"
    r = run(eng.execute(decision("a"), bot_id="b", environment="LIVE", exchange="fake"))
    ad.store["manual"] = next(iter(ad.store.values())).model_copy(update={"client_order_id": "manual"})
    assert run(rec.run("fake"))["result"] == "MISMATCH" and ad.cancel_calls == 0
    del ad.store["manual"]
    assert run(rec.run("fake"))["result"] == "OK"
    assert safety.trading_state() is TradingState.SYSTEM_HEALTHY


def test_startup_ok_keeps_live_and_failure_blocks_live():
    db, path, ad, eng, safety, rec = _live_env()
    rep = run(startup(db, safety, eng, rec, all_strategies(), ["fake"]))
    assert rep.ok and safety.live_switch() is LiveSwitch.ENABLED
    ad.get_fails = True
    async def boom(*a, **k): raise ExchangeError("down")
    ad.get_open_orders = boom
    rep = run(startup(db, safety, eng, rec, all_strategies(), ["fake"]))
    assert not rep.ok and safety.live_switch() is LiveSwitch.BLOCKED
    assert any(n == "reconcile:fake" and not ok for n, ok, _ in rep.steps)


def test_startup_resolves_crashed_order_before_trading():
    db, path, ad, eng, safety, rec = _live_env()
    ad.mode = "timeout_accepted"
    run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    ad.get_fails = True  # exchange unreachable at first
    # simulate a crash while unresolved: new process
    db2 = Database(path)
    s2 = SafetyController(db2)
    eng2 = ExecutionEngine(db2, s2, {"fake": ad}, exchange_ids=eng.exchange_ids)
    rec2 = ReconciliationService(db2, s2, eng2)
    rep = run(startup(db2, s2, eng2, rec2, all_strategies(), ["fake"]))
    assert s2.live_switch() in (LiveSwitch.ENABLED, LiveSwitch.BLOCKED)
    assert ad.create_calls == 1


# ------------------------------------------------------------------ bot runner, workers, health
def _paper_bot():
    db, path = tmp_db()
    pa = PaperAdapter(10_000.0); pa.set_price("BTC/USDT", 109.39)
    safety = SafetyController(db)
    eng = ExecutionEngine(db, safety, {"paper": pa})
    rs = RiskStateStore(db); rs.init_scope("PAPER", 10_000.0)
    async def provider(sym, tf): return S.state(S.steady_uptrend())
    runner = BotRunner(eng, RiskEngine(kill_switch_active=safety.kill_switch_active), rs, provider)
    bot = BotConfig("pb", "Trend AI", "2.2", "BTC/USDT", "15m", "PAPER", "paper")
    return db, path, pa, eng, runner, bot


def test_bot_tick_end_to_end_paper_and_is_idempotent():
    db, path, pa, eng, runner, bot = _paper_bot()
    r1 = run(runner.tick(bot))
    assert r1.outcome == "EXECUTED", r1
    r2 = run(runner.tick(bot))  # same bar again
    assert r2.outcome in ("NO_SIGNAL", "DUPLICATE")
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        s = c.execute("SELECT status, strategy_id FROM signals").fetchone()
    assert s["status"] == "EXECUTED"
    # restart: a brand-new runner (empty in-memory dedupe) still cannot create a second order for the same bar
    db2 = Database(path)
    safety2 = SafetyController(db2)
    eng2 = ExecutionEngine(db2, safety2, {"paper": pa})
    async def provider(sym, tf): return S.state(S.steady_uptrend())
    runner2 = BotRunner(eng2, RiskEngine(), RiskStateStore(db2), provider)
    run(runner2.tick(bot))
    with db2.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_bot_tick_risk_rejection_persisted_and_no_order():
    db, path, pa, eng, runner, bot = _paper_bot()
    RiskStateStore(db).record_trade_result("PAPER", -400.0, 9_600.0)  # 4% down: beyond the 3% daily limit
    r = run(runner.tick(bot))
    assert r.outcome == "RISK_REJECTED"
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert c.execute("SELECT status FROM signals").fetchone()[0] == "REJECTED"
    assert any(a["action"] == "RISK_REJECTED" for a in AuditLog(db).recent(20))


def test_bot_live_tick_halted_by_kill_switch_and_experimental_blocked():
    db, path, pa, eng, runner, bot = _paper_bot()
    live = BotConfig("lb", "Trend AI", "2.2", "BTC/USDT", "15m", "LIVE", "paper")
    assert run(runner.tick(live)).outcome == "BLOCKED"  # no persisted LIVE risk state, so nothing is sent
    eng.safety.activate_kill_switch(ADMIN, "x")
    assert run(runner.tick(live)).outcome == "HALTED"
    exp = BotConfig("lb", "AI Strategy", "0.3", "BTC/USDT", "15m", "LIVE", "paper")
    eng.safety.release_kill_switch(ADMIN)
    assert run(runner.tick(exp)).outcome in ("BLOCKED", "HALTED")


def test_workers_heartbeat_and_failure_degrades_system():
    db, _ = tmp_db()
    s = SafetyController(db)
    sup = WorkerSupervisor(db, s)
    hits = []
    async def ok(): hits.append(1)
    async def bad(): raise RuntimeError("boom")
    sup.add("ok", ok, 1); sup.add("bad", bad, 1)
    for w in sup.workers:
        run(sup.run_once(w))
    st = sup.status()
    assert st["ok"]["status"] == "HEALTHY" and st["bad"]["status"] in ("DEGRADED", "FAILED") and st["bad"]["last_error"] == "RuntimeError" and hits
    assert s.trading_state() is TradingState.SYSTEM_DEGRADED


def test_health_report_no_secrets_and_reflects_state():
    db, _ = tmp_db()
    s = SafetyController(db)
    h = health_report(db, s, vault=CredentialVault(keys={1: K1}))
    assert h["database"] == "healthy" and h["live_trading"] == "disabled" and h["kill_switch"] is False
    assert K1 not in json.dumps(h)
    s.activate_kill_switch(ADMIN, "x")
    assert health_report(db, s)["kill_switch"] is True
