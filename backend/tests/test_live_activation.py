"""Phase 3.4: the single activation policy for live trading, tested at the service layer (runs here) and shared by every control path.
The HTTP regression tests for the same behaviour are in test_api_testclient.py (they need FastAPI)."""
import inspect
import os

import pytest

from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.stubs import BybitAdapter, OKXAdapter
from dewako.execution.engine import ExecutionEngine
from dewako.safety.controller import LiveSwitch, NotAuthorized, ReadinessError, SafetyController
from hardening_helpers import *


def _ready_but_off():
    """Everything the activation policy asks about is true, and live is OFF."""
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety)
    eng.exchange_ids["fake"] = eid
    safety.disable_live(ADMIN)
    return db, ad, eng, safety, eid


def _refused(safety, fragment):
    with pytest.raises(ReadinessError) as e:
        safety.enable_live(ADMIN, "cid")
    assert any(fragment in r for r in e.value.reasons), e.value.reasons
    assert safety.live_switch() is LiveSwitch.DISABLED                      # the switch did not move
    with safety.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='LIVE_ENABLED' AND result='REFUSED'").fetchone()[0] >= 1


def test_ready_system_can_be_enabled_by_an_admin_and_only_then():
    db, ad, eng, safety, eid = _ready_but_off()
    assert safety.activation_readiness().allowed
    safety.enable_live(ADMIN, "cid")
    assert safety.live_switch() is LiveSwitch.ENABLED


def test_empty_system_cannot_be_enabled_even_by_an_admin():
    db, _ = tmp_db()
    safety = SafetyController(db)
    with pytest.raises(ReadinessError) as e:
        safety.enable_live(ADMIN, "cid")
    assert any("no LIVE exchange" in r for r in e.value.reasons)
    assert any("execution engine is not attached" in r for r in e.value.reasons)
    assert any("workers are not running" in r for r in e.value.reasons)
    assert any("audit log integrity" in r for r in e.value.reasons)          # never verified: unknown is not OK
    assert safety.live_switch() is LiveSwitch.DISABLED


def test_each_failed_or_unverified_check_refuses_activation():
    # 1. audit chain not verified / broken
    db, ad, eng, safety, eid = _ready_but_off()
    safety._set("audit_integrity", "UNVERIFIED", "t"); _refused(safety, "audit log integrity")
    # 2. open incident
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("INSERT INTO incidents(kind,severity,scope,detail,status,created_at) VALUES ('X','HIGH','s','d','OPEN',?)", (now_iso(),))
    _refused(safety, "open incident")
    # 3. reconciliation not healthy
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("UPDATE reconciliation_state SET state='RECONCILIATION_REQUIRED' WHERE scope='exchange:fake'")
    _refused(safety, "reconciliation")
    # 4. key permissions allow withdrawal
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("UPDATE exchanges SET permissions=?", ('{"read": true, "trade": true, "withdraw": true}',))
    _refused(safety, "permissions")
    # 5. exchange never verified
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("UPDATE exchanges SET verified_at=NULL")
    _refused(safety, "never verified")
    # 6. no enabled LIVE exchange
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("UPDATE exchanges SET enabled=0")
    _refused(safety, "no LIVE exchange")
    # 7. workers unhealthy
    db, ad, eng, safety, eid = _ready_but_off()
    safety.workers_healthy = lambda: (False, "reconciliation worker stale")
    _refused(safety, "workers not healthy")
    # 8. no worker supervision at all
    db, ad, eng, safety, eid = _ready_but_off()
    safety.workers_healthy = None
    _refused(safety, "workers are not running")
    # 9. kill switch active
    db, ad, eng, safety, eid = _ready_but_off()
    safety.activate_kill_switch(ADMIN, "test stop", "cid")
    _refused(safety, "trading state")


def test_real_adapters_keep_activation_closed_because_the_engine_live_lock_would_refuse_every_order():
    for ad in (BinanceAdapter(api_key="k", api_secret="s"), BybitAdapter(), OKXAdapter()):
        db, _ = tmp_db()
        safety = SafetyController(db)
        eid = make_live_ready(db, safety, exchange=ad.name)
        safety.live_adapters_ok = None                                       # let the real engine register itself
        safety.disable_live(ADMIN)
        eng = ExecutionEngine(db, safety, {ad.name: ad}, exchange_ids={ad.name: eid})
        assert ad.environment == "LIVE"
        _refused(safety, "live lock")
        # and the engine independently refuses an order even if the switch were forced to ENABLED in the table
        safety._set("live_trading", "ENABLED", "forced-by-test")
        out = run(eng.execute(decision(f"x-{ad.name}"), bot_id="b", environment="LIVE", exchange=ad.name))
        assert out.outcome == "BLOCKED" and "live lock" in out.detail


def test_nothing_the_caller_supplies_can_influence_the_decision():
    params = inspect.signature(SafetyController.enable_live).parameters
    assert list(params) == ["self", "actor", "correlation_id"]               # no 'force', 'confirm', 'skip_checks', readiness flags
    db, ad, eng, safety, eid = _ready_but_off()
    safety._set("audit_integrity", "BROKEN", "t")
    for k in ("DEWAKO_LIVE", "DEWAKO_ALLOW_LIVE", "DEWAKO_LIVE_TRADING", "LIVE_TRADING", "FORCE_LIVE"):
        os.environ[k] = "1"
    try:
        _refused(safety, "audit log integrity")
    finally:
        for k in ("DEWAKO_LIVE", "DEWAKO_ALLOW_LIVE", "DEWAKO_LIVE_TRADING", "LIVE_TRADING", "FORCE_LIVE"):
            os.environ.pop(k, None)
    for actor in ({"username": "bob", "role": "TRADER"}, {"username": "ai", "role": "ADMIN"}, {"username": "x", "role": "ANALYST"}):
        with pytest.raises(NotAuthorized):
            safety.enable_live(actor, "cid")


def test_a_readiness_evaluation_error_refuses_instead_of_enabling():
    db, ad, eng, safety, eid = _ready_but_off()
    def boom():
        raise RuntimeError("cannot tell")
    safety.workers_healthy = boom
    _refused(safety, "readiness could not be evaluated")


def test_refusal_is_audited_with_reasons_and_never_with_secrets():
    db, _ = tmp_db()
    safety = SafetyController(db)
    with pytest.raises(ReadinessError):
        safety.enable_live(ADMIN, "cid-activation")
    with db.read() as c:
        row = c.execute("SELECT result, metadata FROM audit_log WHERE action='LIVE_ENABLED' AND correlation_id='cid-activation'").fetchone()
    assert row["result"] == "REFUSED" and "reasons" in row["metadata"]


def test_testnet_evidence_is_required_attested_by_a_human_admin_and_never_automatic():
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("DELETE FROM safety_state WHERE key='evidence:TESTNET_VERIFIED'")
    r = safety.activation_readiness()
    assert not r.allowed and any("TESTNET_VERIFIED" in w for w in r.reasons)
    for bad in ({"username": "bob", "role": "TRADER"}, {"username": "bot", "role": "ADMIN"}, {"username": "ana", "role": "ANALYST"}):
        with pytest.raises(NotAuthorized):
            safety.record_evidence("TESTNET_VERIFIED", bad, "x" * 30)
    with pytest.raises(ValueError):
        safety.record_evidence("TESTNET_VERIFIED", ADMIN, "short")
    with pytest.raises(ValueError):
        safety.record_evidence("LIVE_ENABLED", ADMIN, "x" * 30)
    assert not safety.activation_readiness().allowed
    safety.record_evidence("TESTNET_VERIFIED", ADMIN, "ran the order lifecycle on the exchange testnet, see run 123")
    assert safety.activation_readiness().allowed
    assert safety.live_switch().value != "ENABLED"                        # evidence never turns live on by itself


def test_readiness_states_and_there_is_no_automatic_live_state():
    db, ad, eng, safety, eid = _ready_but_off()
    with db.tx() as c:
        c.execute("DELETE FROM safety_state WHERE key LIKE 'evidence:%'")
    assert safety.readiness_state()["state"] == "LOCKED"
    safety.record_evidence("PAPER_VERIFIED", ADMIN, "paper trading ran for two weeks, report attached")
    assert safety.readiness_state()["state"] == "PAPER_VERIFIED"
    safety.record_evidence("TESTNET_VERIFIED", ADMIN, "ran the order lifecycle on the exchange testnet, see run 123")
    s = safety.readiness_state()
    assert s["state"] == "LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION" and s["live_switch"] != "ENABLED"
    safety.activate_kill_switch(ADMIN, "test stop")
    assert safety.readiness_state()["state"] == "NOT_READY"               # a hard fault overrides all recorded evidence
    states = {"LOCKED", "NOT_READY", "PAPER_VERIFIED", "TESTNET_VERIFIED", "LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION"}
    assert safety.readiness_state()["state"] in states and "LIVE_ENABLED" not in states
