"""Phase 3.3: durable risk reservations under real concurrency.
Each racing worker has its OWN Database object (its own connection pool: separate PostgreSQL backends / SQLite connections)
and its own ExecutionEngine; they share only the database and one fake exchange. Runs on SQLite and, with
DEWAKO_TEST_DB=postgres, on a real PostgreSQL 16 server."""
import threading

import pytest

from dewako.execution.engine import ExecutionEngine
from dewako.execution import reservations as rsv
from dewako.persistence.db import Database, IntegrityError
from dewako.risk.engine import RiskLimits
from dewako.safety.controller import SafetyController
from hardening_helpers import *


def _env(mode="open", **limits):
    db, target = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    ad = FakeLive(mode)
    lim = RiskLimits(**{"max_position_pct": 100, "max_exposure_pct": 100, "max_open_positions": 50, **limits})
    return db, target, eid, ad, lim


def _engine(target, eid, ad, lim, **kw):
    d = Database(target)
    eng = ExecutionEngine(d, SafetyController(d), {"fake": ad}, exchange_ids={"fake": eid}, limits=lim,
                          quote_guard=TEST_QUOTE_GUARD, **kw)
    return d, eng


def _race(n, target, eid, ad, lim, qty, same_signal=False):
    outs, barrier, errs = [], threading.Barrier(n), []
    def worker(i):
        d, eng = _engine(target, eid, ad, lim)
        try:
            sid = "same" if same_signal else f"s{i}"
            bot = "bot" if same_signal else f"bot{i}"
            barrier.wait(30)
            outs.append(run(eng.execute(decision(sid, qty=qty), bot_id=bot, environment="LIVE", exchange="fake")))
        except Exception as e:      # an unexpected exception is a failure, not a pass
            errs.append(repr(e))
        finally:
            d.close()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join(120) for t in ts]
    assert not errs, errs
    assert len(outs) == n
    return outs


def _count(db, sql, *a):
    with db.read() as c:
        return c.execute(sql, a).fetchone()[0]


def test_concurrent_total_exposure_cannot_be_exceeded():
    db, target, eid, ad, lim = _env(max_position_pct=30, max_exposure_pct=70)        # equity 100000: 70000 total, 30000 per order
    outs = _race(8, target, eid, ad, lim, qty=250)                                   # each order is 25000 notional -> only 2 fit
    assert sorted(o.outcome for o in outs).count("EXECUTED") == 2, [o.outcome for o in outs]
    assert ad.create_calls == 2
    assert _count(db, "SELECT COALESCE(SUM(notional),0) FROM risk_reservations WHERE status='ACTIVE'") <= 70000
    assert _count(db, "SELECT COUNT(*) FROM orders") == 2


def test_concurrent_open_position_limit_cannot_be_exceeded():
    db, target, eid, ad, lim = _env(max_open_positions=3)
    outs = _race(8, target, eid, ad, lim, qty=10)
    assert [o.outcome for o in outs].count("EXECUTED") == 3
    assert ad.create_calls == 3 and _count(db, "SELECT COUNT(*) FROM risk_reservations WHERE status='ACTIVE'") == 3


def test_concurrent_requests_cannot_reserve_the_same_available_funds():
    db, target, eid, ad, lim = _env()                                                # balance 100000, orders of 40000
    outs = _race(5, target, eid, ad, lim, qty=400)
    assert [o.outcome for o in outs].count("EXECUTED") == 2
    assert ad.create_calls == 2
    assert _count(db, "SELECT COALESCE(SUM(notional),0) FROM risk_reservations WHERE status='ACTIVE'") <= 100000


def test_same_signal_submitted_by_many_workers_makes_one_order_and_one_reservation():
    db, target, eid, ad, lim = _env()
    outs = _race(6, target, eid, ad, lim, qty=10, same_signal=True)
    names = sorted(o.outcome for o in outs)
    assert names.count("EXECUTED") == 1 and names.count("DUPLICATE") == 5, names
    assert ad.create_calls == 1
    assert _count(db, "SELECT COUNT(*) FROM orders") == 1 and _count(db, "SELECT COUNT(*) FROM risk_reservations") == 1


def test_instant_fills_never_let_the_total_exceed_the_cap():
    """With immediate fills the balance and the reservations move at the same time; the check may be stricter than needed, never looser."""
    db, target, eid, ad, lim = _env("ok", max_position_pct=30, max_exposure_pct=70)
    outs = _race(8, target, eid, ad, lim, qty=250)
    n = [o.outcome for o in outs].count("EXECUTED")
    assert 1 <= n <= 2 and ad.create_calls == n
    assert _count(db, "SELECT COALESCE(SUM(quantity*average_price),0) FROM positions WHERE status<>'CLOSED'") <= 70000 + 1e-6


def test_order_above_per_order_limit_is_blocked_and_leaves_no_order_or_reservation():
    db, target, eid, ad, lim = _env(max_position_pct=30)
    d, eng = _engine(target, eid, ad, lim)
    r = run(eng.execute(decision("big", qty=500), bot_id="b", environment="LIVE", exchange="fake"))      # 50000 > 30% of 100000
    assert r.outcome == "BLOCKED" and "exceeds" in r.detail and ad.create_calls == 0
    assert _count(db, "SELECT COUNT(*) FROM orders") == 0 and _count(db, "SELECT COUNT(*) FROM risk_reservations") == 0
    assert _count(db, "SELECT status FROM signals WHERE signal_id='big'") == "REJECTED"
    d.close()


def test_rollback_after_reserving_releases_the_reservation():
    db, target, eid, ad, lim = _env()
    d, eng = _engine(target, eid, ad, lim)
    real = rsv.reserve
    def boom(c, **kw):
        real(c, **kw)
        raise RuntimeError("crash right after reserving")
    rsv.reserve = boom
    try:
        with pytest.raises(RuntimeError):
            run(eng.execute(decision("rb", qty=900), bot_id="b", environment="LIVE", exchange="fake"))     # 90000 of 100000
    finally:
        rsv.reserve = real
    assert _count(db, "SELECT COUNT(*) FROM risk_reservations") == 0 and _count(db, "SELECT COUNT(*) FROM orders") == 0
    r = run(eng.execute(decision("rb2", qty=900), bot_id="b2", environment="LIVE", exchange="fake"))    # the capacity was not leaked
    assert r.outcome == "EXECUTED"
    d.close()


def test_committed_reservations_survive_a_restart():
    db, target, eid, ad, lim = _env(max_position_pct=60, max_exposure_pct=70)
    d1, e1 = _engine(target, eid, ad, lim)
    assert run(e1.execute(decision("r1", qty=500), bot_id="a", environment="LIVE", exchange="fake")).outcome == "EXECUTED"     # 50000 reserved
    d1.close()                                                                        # "restart": new connections, new engine
    d2, e2 = _engine(target, eid, ad, lim)
    r = run(e2.execute(decision("r2", qty=300), bot_id="b", environment="LIVE", exchange="fake"))                              # 50000+30000 > 70000
    assert r.outcome == "BLOCKED" and "total exposure" in r.detail
    d2.close()


def test_reservation_lifecycle_follows_order_state():
    db, target, eid, ad, lim = _env("ok")
    d, eng = _engine(target, eid, ad, lim)
    run(eng.execute(decision("f", qty=10), bot_id="a", environment="LIVE", exchange="fake"))
    assert _count(db, "SELECT status FROM risk_reservations") == "CONSUMED"                         # filled -> the position carries it
    ad.mode = "open"
    r = run(eng.execute(decision("o", qty=10), bot_id="b", environment="LIVE", exchange="fake"))
    assert _count(db, "SELECT status FROM risk_reservations WHERE order_id=?", r.order_id) == "ACTIVE"
    run(eng.cancel(r.order_id))
    assert _count(db, "SELECT status FROM risk_reservations WHERE order_id=?", r.order_id) == "RELEASED"
    ad.mode = "timeout_absent"
    r = run(eng.execute(decision("u", qty=10), bot_id="c", environment="LIVE", exchange="fake"))
    assert _count(db, "SELECT status FROM risk_reservations WHERE order_id=?", r.order_id) == "RELEASED"      # confirmed absent -> FAILED -> released
    # invariant: ACTIVE notional equals the notional of orders that are not in a terminal state
    assert _count(db, "SELECT COALESCE(SUM(notional),0) FROM risk_reservations WHERE status='ACTIVE'") == _count(
        db, "SELECT COALESCE(SUM(r.notional),0) FROM risk_reservations r JOIN orders o ON o.id=r.order_id WHERE o.state NOT IN ('FILLED','REJECTED','FAILED','CANCELLED','EXPIRED')")
    d.close()


def test_unknown_outcome_keeps_the_capacity_reserved():
    db, target, eid, ad, lim = _env("timeout_accepted")
    d, eng = _engine(target, eid, ad, lim)
    ad.get_fails = True                                                                # the exchange cannot be asked, so the order stays unresolved
    r = run(eng.execute(decision("k", qty=10), bot_id="a", environment="LIVE", exchange="fake"))
    assert r.outcome == "UNRESOLVED"
    assert _count(db, "SELECT status FROM risk_reservations") == "ACTIVE"
    d.close()


def test_worker_that_lost_authorization_cannot_send_a_created_order():
    db, target, eid, ad, lim = _env()
    d, eng = _engine(target, eid, ad, lim, submit_authorized=lambda: (False, "worker stopped"))
    r = run(eng.execute(decision("w", qty=10), bot_id="a", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "worker stopped" in r.detail and ad.create_calls == 0
    assert _count(db, "SELECT state FROM orders") == "FAILED"
    assert _count(db, "SELECT status FROM risk_reservations") == "RELEASED"
    d.close()


def test_kill_switch_activated_between_creation_and_send_stops_the_send():
    db, target, eid, ad, lim = _env()
    d, eng = _engine(target, eid, ad, lim)
    real = eng._send
    async def kill_then_send(*a, **kw):                    # the order exists (CREATED, reserved); now the kill switch is pulled
        SafetyController(Database(target)).activate_kill_switch(ADMIN, "test", "c")
        return await real(*a, **kw)
    eng._send = kill_then_send
    r = run(eng.execute(decision("ks", qty=10), bot_id="a", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "kill switch" in r.detail and ad.create_calls == 0
    assert _count(db, "SELECT status FROM risk_reservations") == "RELEASED"
    d.close()


def test_duplicate_client_order_id_is_rejected_by_the_database():
    db, target, eid, ad, lim = _env()
    d, eng = _engine(target, eid, ad, lim)
    r = run(eng.execute(decision("c1", qty=10), bot_id="a", environment="LIVE", exchange="fake"))
    cid = eng.orders.get(r.order_id)["client_order_id"]
    with pytest.raises(IntegrityError):
        with db.tx() as c:
            c.execute("INSERT INTO execution_intents(bot_id,signal_id,intent,created_at) VALUES ('z','zz','ENTRY','2026-01-01')")
            iid = c.execute("SELECT id FROM execution_intents WHERE signal_id='zz'").fetchone()[0]
            c.execute("INSERT INTO orders(intent_id,client_order_id,environment,exchange,market_type,bot_id,symbol,side,order_type,quantity,price,state,"
                      "attempt,correlation_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (iid, cid, "LIVE", "fake", "SPOT", "z", "BTC/USDT", "BUY", "MARKET", 1, 1, "CREATED", 1, "c", "t", "t"))
    d.close()
