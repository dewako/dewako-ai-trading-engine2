"""Phase 3.3 process-level crash matrix.
A controller launches the execution worker (sim_worker.py) as a SEPARATE PROCESS, kills it with SIGKILL at a named boundary,
then starts a fresh worker process and checks the consequences. The fake exchange keeps its own persistent order/fill state in a
file; the database is SQLite or PostgreSQL (DEWAKO_TEST_DB). Every worker runs in its own process group with a hard timeout and is
killed in `finally`, so a failed test cannot leave a background process behind.
Not tested here: power loss, kernel panic, disk failure, a real exchange, a PostgreSQL server dying while connected."""
import json
import os
import signal
import subprocess
import sys
import tempfile

import pytest

from dewako.persistence.db import Database
from dewako.safety.controller import SafetyController, TradingState
from bounded import KILLED_RC, WorkerHung, bound_all, pid_alive, run_isolated, run_process, wait_until_dead, watchdog, watchdog_mode, TestTimeout
from hardening_helpers import *

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SIM_CLIENT_FILL_PRICE = 100.0


# ------------------------------------------------------------------ controller
def init_sim(mode="ok", **extra):
    path = os.path.join(tempfile.mkdtemp(), "sim.json")
    json.dump({"mode": mode, "orders": {}, "create_calls": 0, "duplicate_attempts": 0, **extra}, open(path, "w"))
    return path


def sim(path):
    return json.load(open(path))


def sim_edit(path, fn):
    d = json.load(open(path)); fn(d); json.dump(d, open(path, "w"))


def worker(target, xfile, eid, *cmd, crash=None, timeout=90):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([BACKEND, HERE, os.environ.get("PYTHONPATH", "")])}
    env.pop("CRASH_AT", None)
    if crash:
        env["CRASH_AT"] = crash
    rc, out, err = run_process([sys.executable, os.path.join(HERE, "sim_worker.py"), target, xfile, str(eid), *map(str, cmd)], env, timeout,
                               label=f"crash_at={crash} cmd={cmd}")

    class p:                                                   # keep the assertions below unchanged
        returncode = rc
    if crash:
        assert p.returncode == KILLED_RC, f"worker should have been killed at {crash}, rc={p.returncode}\nstdout:{out[-600:]}\nstderr:{err[-1200:]}"
        return None
    assert p.returncode == 0, f"worker failed rc={p.returncode}\n{err[-1200:]}"
    line = [l for l in out.splitlines() if l.startswith("RESULT ")]
    assert line, out + err
    return json.loads(line[-1][7:])


def env():
    db, target = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    return db, target, eid


def q(db, sql, *a):
    with db.read() as c:
        return c.execute(sql, a).fetchone()[0]


def rows(db, sql, *a):
    with db.read() as c:
        return [dict(r) for r in c.execute(sql, a)]


def check_invariants(db, xfile):
    """No duplicate external orders, no double-counted fills, no fabricated balances/positions, ledger consistent."""
    s = sim(xfile)
    assert s["duplicate_attempts"] == 0, "the worker tried to create an order whose client id already existed on the exchange"
    assert s["create_calls"] == len(s["orders"]), "number of exchange order creations differs from distinct client ids (duplicate order)"
    tr = rows(db, "SELECT order_id, exchange_trade_id, quantity, price FROM trades")
    assert len({(t["order_id"], t["exchange_trade_id"]) for t in tr}) == len(tr), "a fill was recorded twice"
    by_cid = {o["client_order_id"]: o for o in rows(db, "SELECT id, client_order_id, state, filled, quantity FROM orders")}
    local_qty = 0.0
    for cid, o in by_cid.items():
        local = sum(t["quantity"] for t in tr if t["order_id"] == o["id"])
        assert abs(local - o["filled"]) < 1e-9, f"trades ({local}) disagree with the order's filled quantity ({o['filled']})"
        if cid in s["orders"] and o["state"] in ("FILLED", "PARTIALLY_FILLED", "CANCELLED"):
            assert abs(local - s["orders"][cid]["filled"]) < 1e-9, "local fills differ from the exchange's fills"
        local_qty += local
    pos = q(db, "SELECT COALESCE(SUM(quantity),0) FROM positions WHERE status<>'CLOSED'")
    assert abs(pos - local_qty) < 1e-9, f"position quantity {pos} != traded quantity {local_qty}"
    assert q(db, "SELECT COALESCE(SUM(notional),0) FROM risk_reservations WHERE status='ACTIVE'") == q(
        db, "SELECT COALESCE(SUM(r.notional),0) FROM risk_reservations r JOIN orders o ON o.id=r.order_id WHERE o.state NOT IN "
            "('FILLED','REJECTED','FAILED','CANCELLED','EXPIRED')"), "reservation ledger does not match live orders"


# ------------------------------------------------------------------ the harness itself
def test_hung_worker_is_killed_by_the_controller_timeout():
    db, target, eid = env()
    x = init_sim()
    try:
        worker(target, x, eid, "hang", timeout=4)
        raise RuntimeError("the controller did not time out")
    except WorkerHung as e:                                        # an EXPECTED hang: reported as such, with diagnostics
        assert "hung" in str(e) and e.pid
        assert wait_until_dead([e.pid], 10) == [], "the hung worker is still running"          # cross-platform pid check (no pgrep)


# ------------------------------------------------------------------ 1-9
def test_1_crash_before_intent_commit():
    db, target, eid = env(); x = init_sim()
    worker(target, x, eid, "exec", "c1", 10, "bot", crash="before_intent_commit")
    assert q(db, "SELECT COUNT(*) FROM orders") == 0 and q(db, "SELECT COUNT(*) FROM risk_reservations") == 0
    assert sim(x)["create_calls"] == 0
    r = worker(target, x, eid, "exec", "c1", 10, "bot")                          # the same signal after restart
    assert r["outcome"] == "EXECUTED" and sim(x)["create_calls"] == 1
    check_invariants(db, x)


def test_2_crash_after_intent_commit_before_submission():
    db, target, eid = env(); x = init_sim()
    worker(target, x, eid, "exec", "c2", 10, "bot", crash="after_intent_commit")
    assert q(db, "SELECT state FROM orders") == "CREATED" and q(db, "SELECT status FROM risk_reservations") == "ACTIVE"
    assert sim(x)["create_calls"] == 0
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FAILED" and q(db, "SELECT status FROM risk_reservations") == "RELEASED"
    assert q(db, "SELECT event_type FROM order_events ORDER BY id DESC LIMIT 1") == "ABANDONED_AFTER_CRASH"
    assert sim(x)["create_calls"] == 0                                           # an unsent order is abandoned, not sent late
    assert worker(target, x, eid, "exec", "c2", 10, "bot")["outcome"] == "DUPLICATE"      # same signal never becomes a second order
    assert worker(target, x, eid, "exec", "c2b", 10, "bot")["outcome"] == "EXECUTED"      # a fresh signal proceeds
    check_invariants(db, x)


def test_3_crash_during_submission_request_never_reached_exchange():
    db, target, eid = env(); x = init_sim()
    worker(target, x, eid, "exec", "c3", 10, "bot", crash="during_submit")
    assert q(db, "SELECT state FROM orders") == "SUBMITTING" and sim(x)["create_calls"] == 0
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FAILED"                          # exchange confirmed it has no such order
    assert q(db, "SELECT status FROM risk_reservations") == "RELEASED"
    oid = q(db, "SELECT id FROM orders")
    r = worker(target, x, eid, "retry", oid)                                      # allowed only because absence was confirmed
    assert r["state"] == "FILLED" and sim(x)["create_calls"] == 1
    check_invariants(db, x)


def test_4_crash_after_exchange_accepts_before_local_ack():
    db, target, eid = env(); x = init_sim()
    worker(target, x, eid, "exec", "c4", 10, "bot", crash="after_exchange_accept")
    assert q(db, "SELECT state FROM orders") == "SUBMITTING" and sim(x)["create_calls"] == 1
    assert q(db, "SELECT COUNT(*) FROM trades") == 0
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FILLED" and q(db, "SELECT COUNT(*) FROM trades") == 1
    assert q(db, "SELECT quantity FROM positions") == 10
    assert worker(target, x, eid, "exec", "c4", 10, "bot")["outcome"] == "DUPLICATE"
    worker(target, x, eid, "recover")                                             # recovering twice changes nothing
    assert q(db, "SELECT COUNT(*) FROM trades") == 1 and sim(x)["create_calls"] == 1
    check_invariants(db, x)


def test_5_crash_after_partial_fill():
    db, target, eid = env(); x = init_sim("partial")
    worker(target, x, eid, "exec", "c5", 10, "bot", crash="after_partial_fill_commit")
    assert q(db, "SELECT state FROM orders") == "PARTIALLY_FILLED" and q(db, "SELECT SUM(quantity) FROM trades") == 5
    worker(target, x, eid, "recover")                                             # exchange has not progressed: nothing new may appear
    assert q(db, "SELECT COUNT(*) FROM trades") == 1 and q(db, "SELECT quantity FROM positions") == 5
    def finish(d):
        for o in d["orders"].values():
            o["filled"], o["status"] = o["quantity"], "FILLED"
    sim_edit(x, finish)                                                           # the exchange fills the rest while the worker is down
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FILLED"
    assert q(db, "SELECT COUNT(*) FROM trades") == 2 and q(db, "SELECT SUM(quantity) FROM trades") == 10
    assert q(db, "SELECT quantity FROM positions") == 10 and q(db, "SELECT status FROM risk_reservations") == "CONSUMED"
    worker(target, x, eid, "recover")
    assert q(db, "SELECT COUNT(*) FROM trades") == 2                              # not counted twice
    check_invariants(db, x)


def test_6_crash_after_fill_received_before_local_transaction_commits():
    db, target, eid = env(); x = init_sim("ok")
    worker(target, x, eid, "exec", "c6", 10, "bot", crash="during_fill_tx")
    assert sim(x)["orders"] and list(sim(x)["orders"].values())[0]["filled"] == 10   # the exchange filled it
    assert q(db, "SELECT COUNT(*) FROM trades") == 0 and q(db, "SELECT COUNT(*) FROM positions") == 0     # the fill was NOT lost nor half-written
    assert q(db, "SELECT state FROM orders") == "SUBMITTED"
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FILLED" and q(db, "SELECT COUNT(*) FROM trades") == 1 and q(db, "SELECT quantity FROM positions") == 10
    worker(target, x, eid, "recover")
    assert q(db, "SELECT COUNT(*) FROM trades") == 1 and q(db, "SELECT quantity FROM positions") == 10
    check_invariants(db, x)


def test_7_crash_during_cancellation():
    db, target, eid = env(); x = init_sim("open")
    r = worker(target, x, eid, "exec", "c7", 10, "bot")
    assert r["state"] == "OPEN"
    worker(target, x, eid, "cancel", r["order_id"], crash="during_cancel")
    assert q(db, "SELECT state FROM orders") == "CANCEL_REQUESTED"                # NOT reported cancelled
    assert list(sim(x)["orders"].values())[0]["status"] == "CANCELLED"            # the exchange did cancel it
    assert q(db, "SELECT status FROM risk_reservations") == "ACTIVE"              # capacity stays reserved until confirmed
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "CANCELLED" and q(db, "SELECT status FROM risk_reservations") == "RELEASED"
    assert q(db, "SELECT COUNT(*) FROM trades") == 0 and q(db, "SELECT COUNT(*) FROM positions") == 0
    check_invariants(db, x)


def test_8_crash_during_reconciliation():
    db, target, eid = env(); x = init_sim("ok")
    assert worker(target, x, eid, "exec", "c8", 10, "bot")["state"] == "FILLED"
    assert worker(target, x, eid, "reconcile")["state"] == "HEALTHY"
    def external(d):                                                              # someone traded on the exchange outside DEWAKO
        o = dict(list(d["orders"].values())[0]); o["client_order_id"] = "manual-1"; o["exchange_order_id"] = "XM1"
        d["orders"]["manual-1"] = o; d["create_calls"] += 1
    sim_edit(x, external)
    worker(target, x, eid, "reconcile", crash="during_reconcile")
    assert q(db, "SELECT COUNT(*) FROM reconciliation_findings") == 0             # nothing half-recorded
    assert q(db, "SELECT state FROM reconciliation_state WHERE scope='exchange:fake'") == "RECONCILING"   # and the scope is NOT healthy
    assert SafetyController(db).live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2").allowed is False
    r = worker(target, x, eid, "reconcile")
    assert r["state"] == "RECONCILIATION_REQUIRED" and {"balance_unexplained", "position_quantity_mismatch"} <= set(r["findings"])
    n = q(db, "SELECT COUNT(*) FROM reconciliation_findings WHERE status='OPEN'")
    worker(target, x, eid, "reconcile")
    assert q(db, "SELECT COUNT(*) FROM reconciliation_findings WHERE status='OPEN'") == n      # findings are not duplicated
    assert q(db, "SELECT COUNT(*) FROM orders") == 1                                # no remote order was invented locally
    assert worker(target, x, eid, "exec", "c8b", 10, "bot")["outcome"] == "BLOCKED"
    assert SafetyController(db).live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2").allowed is False


def test_9a_exchange_timeouts_block_new_orders_then_escalate_to_an_incident():
    db, target, eid = env(); x = init_sim("timeout_accepted", unreachable=True)
    r = worker(target, x, eid, "exec", "c9", 10, "bot")
    assert r["outcome"] == "UNRESOLVED" and q(db, "SELECT state FROM orders") == "RECONCILING"
    assert SafetyController(db).trading_state() is TradingState.RECONCILIATION_REQUIRED
    worker(target, x, eid, "recover")                                              # a restarted worker still cannot learn the truth
    assert worker(target, x, eid, "exec", "c9b", 10, "bot")["outcome"] == "BLOCKED"        # unknown state blocks risk-increasing orders
    assert sim(x)["create_calls"] == 1
    worker(target, x, eid, "recover")
    assert q(db, "SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND kind='ORDER_UNRESOLVED'") == 1
    g = SafetyController(db).live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2")
    assert not g.allowed and any("incident" in w for w in g.reasons)
    sim_edit(x, lambda d: d.update(unreachable=False))                             # the network comes back
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "FILLED" and sim(x)["create_calls"] == 1
    assert q(db, "SELECT COUNT(*) FROM incidents WHERE status='OPEN'") == 1        # the incident still needs a human
    check_invariants(db, x)


def test_9b_database_outage_fails_closed_and_sends_nothing():
    db, target, eid = env(); x = init_sim("ok")
    bad = "postgresql://dewako:x@127.0.0.1:1/none" if backend() == "postgres" else "/nonexistent_dir_xyz/none.db"
    r = worker(bad, x, eid, "startup_db_outage")
    assert r["status"] == "DEGRADED_NO_DATABASE" and r["creates"] == 0
    assert sim(x)["create_calls"] == 0


def test_10_unrecoverable_ambiguity_creates_a_durable_incident_and_blocks_live():
    db, target, eid = env(); x = init_sim("open")
    r = worker(target, x, eid, "exec", "c10", 10, "bot")
    sim_edit(x, lambda d: d["orders"].clear())                                      # the exchange has no record of an order we believed open
    worker(target, x, eid, "recover")
    assert q(db, "SELECT state FROM orders") == "UNKNOWN"
    assert q(db, "SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND kind='ACTIVE_ORDER_MISSING_ON_EXCHANGE'") == 1
    assert q(db, "SELECT status FROM risk_reservations") == "ACTIVE"               # nothing is released on a guess
    safety = SafetyController(db)
    assert not safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2").allowed
    worker(target, x, eid, "recover")                                               # a second restart does not duplicate the incident
    assert q(db, "SELECT COUNT(*) FROM incidents WHERE status='OPEN'") == 1
    with pytest.raises(Exception):
        safety.resolve_incident(1, {"username": "bob", "role": "TRADER"}, "I looked at it, fine")
    safety.resolve_incident(1, ADMIN, "Verified on the exchange UI that the order never existed")
    assert q(db, "SELECT status FROM incidents") == "RESOLVED"


bound_all(globals())          # every test here is also bounded in-process (DEWAKO_TEST_TIMEOUT, default 240s)
