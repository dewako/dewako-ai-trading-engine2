"""Phase 3.2: real process kill, balance reconciliation lifecycle, and PostgreSQL-only concurrency/atomicity.
Fake exchanges only; no network. The PG tests are skipped (reported as skipped, not passed) on SQLite."""
import os
import subprocess
import sys
import threading
import unittest

import pytest

from dewako.persistence.audit import AuditLog
from dewako.reconciliation.service import NotAuthorized, ReconciliationService
from dewako.safety.controller import LiveSwitch, SafetyController
from bounded import KILLED_RC, bound_all, run_process
from hardening_helpers import *
from crash_worker import FileLive
from test_hardening_platform import _live_env

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)


def _pg_only():
    if backend() != "postgres":
        raise unittest.SkipTest("PostgreSQL-only test (set DEWAKO_TEST_DB=postgres)")


# ------------------------------------------------------------------ real process kill
def test_sigkill_after_exchange_accepts_then_recovery_does_not_resend():
    db, target = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    xfile = os.path.join(tempfile.mkdtemp(), "exchange.json")
    env = {**os.environ, "DEWAKO_CRASH": "1", "PYTHONPATH": os.pathsep.join([BACKEND, HERE, os.environ.get("PYTHONPATH", "")])}
    rc, out, err = run_process([sys.executable, os.path.join(HERE, "crash_worker.py"), target, xfile, str(eid)], env, 120, label="crash_worker")
    assert rc == KILLED_RC, (rc, out[-400:], err[-800:])                       # killed by SIGKILL, not a clean exit or a Python exception
    assert "survived" not in out
    with db.read() as c:
        row = c.execute("SELECT state FROM orders").fetchone()
    assert row["state"] == "SUBMITTING"                                  # the write-ahead state is what the dead process left behind
    # a new process: the exchange (file) has the order; DEWAKO must find it, not send again
    ad = FileLive(xfile)
    assert ad.create_calls == 1
    eng, _ = engine_for(db, ad)
    eng.exchange_ids["fake"] = eid
    run(eng.recover())
    with db.read() as c:
        assert c.execute("SELECT state FROM orders").fetchone()["state"] == "FILLED"
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert ad.create_calls == 1
    # the same signal arriving again after the restart is a duplicate, not a second order
    r = run(eng.execute(decision("crash1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "DUPLICATE" and ad.create_calls == 1


# ------------------------------------------------------------------ balance reconciliation lifecycle
def test_unexplained_balance_change_blocks_needs_admin_and_is_never_auto_fixed():
    db, path, ad, eng, safety, rec = _live_env()
    first = run(rec.run("fake"))
    assert first["state"] == "HEALTHY"                                   # baseline snapshot recorded
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM account_snapshots WHERE source='EXCHANGE'").fetchone()[0] >= 1
    ad.initial_usdt += 500.0                                             # someone deposited: DEWAKO did not cause this
    before_calls = (ad.create_calls, ad.cancel_calls)
    r = run(rec.run("fake"))
    assert r["state"] == "RECONCILIATION_REQUIRED" and any(f["type"] == "balance_unexplained" for f in r["findings"])
    assert safety.live_switch() is LiveSwitch.BLOCKED
    out = run(eng.execute(decision("after-deposit"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == before_calls[0]
    # a second run does not make it go away on its own: balance findings need a person
    r2 = run(rec.run("fake"))
    assert r2["state"] == "RECONCILIATION_REQUIRED"
    fid = rec.open_findings("exchange:fake")[0]["id"]
    for actor in ({"username": "bob", "role": "TRADER"}, {"username": "ops", "role": "ANALYST"}, {"username": "ai", "role": "ADMIN"}):
        with pytest.raises(NotAuthorized):
            rec.resolve_finding(fid, actor, "looks fine to me, deposit")
    with pytest.raises(ValueError):
        rec.resolve_finding(fid, ADMIN, "ok")                            # a real written reason is required
    assert ad.initial_usdt == 100500.0                                   # nothing adjusted the balance anywhere
    rec.resolve_finding(fid, ADMIN, "Deposit of 500 USDT made manually by the account owner")
    assert rec.state.current("exchange:fake") == "RESOLVED"
    assert run(rec.run("fake"))["state"] == "HEALTHY"                    # a clean run is still required afterwards
    with db.read() as c:
        acts = [r["action"] for r in c.execute("SELECT action, result FROM audit_log WHERE action='RECONCILIATION_RESOLVED'")]
        denied = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='RECONCILIATION_RESOLVED' AND result='DENIED'").fetchone()[0]
    assert acts and denied == 3
    assert (ad.create_calls, ad.cancel_calls) == before_calls


def test_balance_explained_by_our_own_fill_is_not_a_finding():
    db, path, ad, eng, safety, rec = _live_env()
    assert run(rec.run("fake"))["state"] == "HEALTHY"
    run(eng.execute(decision("own"), bot_id="b", environment="LIVE", exchange="fake"))
    r = run(rec.run("fake"))
    assert r["state"] == "HEALTHY" and not r["findings"]


def test_reconciliation_state_machine_rejects_illegal_jump():
    db, path, ad, eng, safety, rec = _live_env()
    rec.state.transition("orders", "RECONCILIATION_REQUIRED", "test", "t", "c")
    with pytest.raises(Exception):
        rec.state.transition("orders", "HEALTHY", "skipping the reconciler", "t", "c")   # must pass through RECONCILING/RESOLVED


# ------------------------------------------------------------------ PostgreSQL only
def test_pg_is_really_postgresql():
    _pg_only()
    db, _ = tmp_db()
    assert db.is_pg
    with db.read() as c:
        version = c.execute("SELECT version()").fetchone()[0]
        assert "PostgreSQL" in version
        want = os.environ.get("DEWAKO_EXPECT_PG_MAJOR")                   # CI pins 16: a different server must fail, not pass quietly
        if want:
            assert c.execute("SHOW server_version").fetchone()[0].split(".")[0] == want, version
        assert c.execute("SELECT current_schema()").fetchone()[0].startswith("t_")
        n = c.execute("SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema=current_schema() AND constraint_type IN ('UNIQUE','FOREIGN KEY','CHECK')").fetchone()[0]
    assert n >= 20                                                        # real constraints exist in the server, not just in Python


def test_pg_two_independent_connections_race_one_intent_one_order():
    _pg_only()
    db, target = tmp_db()
    eid = make_live_ready(db, SafetyController(db))
    ad = FakeLive()
    results, barrier = [], threading.Barrier(4)
    def worker():
        d2 = Database(target)                                             # its own pool: its own PostgreSQL backend processes
        eng, _ = engine_for(d2, ad); eng.exchange_ids["fake"] = eid
        barrier.wait()
        results.append(run(eng.execute(decision("race"), bot_id="b", environment="LIVE", exchange="fake")).outcome)
        d2.close()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join(60) for t in ts]
    assert sorted(results).count("EXECUTED") == 1 and len(results) == 4, results
    assert ad.create_calls == 1
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_fill_position_trade_are_atomic_and_recovery_completes_them():
    """Runs on both backends; on PostgreSQL this is a server-side rollback of an aborted transaction."""
    db, target = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    real = eng.positions.open_or_add
    def boom(c, **kw):
        real(c, **kw)                                                      # the position row and event are written inside the transaction...
        raise RuntimeError("crash between position and trade")            # ...and the trade insert never happens
    eng.positions.open_or_add = boom
    try:
        run(eng.execute(decision("atomic"), bot_id="b", environment="LIVE", exchange="fake"))
    except RuntimeError:
        pass
    eng.positions.open_or_add = real
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM position_events").fetchone()[0] == 0
        assert c.execute("SELECT state FROM orders").fetchone()["state"] != "FILLED"      # the state change rolled back with them
    assert ad.create_calls == 1
    run(eng.recover())                                                     # the exchange has the fill: all three records now appear together
    with db.read() as c:
        assert c.execute("SELECT state FROM orders").fetchone()["state"] == "FILLED"
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    assert ad.create_calls == 1


def test_pg_a_held_lock_fails_closed_within_a_bound_instead_of_hanging():
    # Phase 3.4: a peer (e.g. a worker whose connection is wedged) holding the audit-chain advisory lock must make the writer fail
    # with a clear error after lock_timeout, never block forever. Nothing may be half-written.
    _pg_only()
    import time
    from dewako.persistence import audit as audit_mod
    from dewako.persistence.db import Database
    old = os.environ.get("DEWAKO_PG_LOCK_TIMEOUT_MS")
    os.environ["DEWAKO_PG_LOCK_TIMEOUT_MS"] = "1500"
    try:
        db, target = tmp_db()
        log = AuditLog(db)
        log.record("BEFORE", "OK", "c0")
        other = Database(target)                                          # an independent connection plays the stuck peer
        with other.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(?)", (audit_mod._ADVISORY_KEY,))
            n_before = len(log.recent(100))
            t0 = time.monotonic()
            raised = None
            try:
                log.record("BLOCKED", "OK", "c1")
            except Exception as e:                                        # lock_timeout (SQLSTATE 55P03) surfaces as a database error
                raised = e
            elapsed = time.monotonic() - t0
        assert raised is not None, "the audit write succeeded while the chain lock was held elsewhere"
        assert elapsed < 15, f"waited {elapsed:.1f}s: not bounded"
        assert len(log.recent(100)) == n_before                            # no half-written row
        log.record("AFTER", "OK", "c2")                                    # and the system recovers once the lock is gone
        assert log.verify_chain()["ok"]
    finally:
        if old is None:
            os.environ.pop("DEWAKO_PG_LOCK_TIMEOUT_MS", None)
        else:
            os.environ["DEWAKO_PG_LOCK_TIMEOUT_MS"] = old


bound_all(globals())
