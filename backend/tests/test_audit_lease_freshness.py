"""Phase 3.3: audit-log integrity (hash chain + append-only), worker lease fencing, market-data freshness in the live gate.
Fake exchanges only. Runs on SQLite and, with DEWAKO_TEST_DB=postgres, on a real PostgreSQL 16 server."""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import pytest

from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database
from dewako.core.types import Candle, MarketState
from dewako.market_data.timeframes import floor, seconds
from dewako.runtime.application import Application, Config
from dewako.runtime.lease import WorkerLease
from dewako.safety.controller import SafetyController
from hardening_helpers import *


def _pg_only():
    if backend() != "postgres":
        raise unittest.SkipTest("PostgreSQL-only test (set DEWAKO_TEST_DB=postgres)")


def _disable_triggers(db):
    """What an attacker with schema ownership would do first."""
    with db.tx() as c:
        if db.is_pg:
            c.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        else:
            c.execute("DROP TRIGGER audit_log_no_update")
            c.execute("DROP TRIGGER audit_log_no_delete")


def _seed(db, n=6):
    a = AuditLog(db)
    for i in range(n):
        a.record(f"EVENT_{i}", "OK", f"cid{i}", actor={"user_id": i, "username": "u", "role": "ADMIN"}, metadata={"i": i, "password": "hunter2"})
    return a


# ------------------------------------------------------------------ audit integrity
def test_chain_links_every_row_and_verifies():
    db, _ = tmp_db()
    a = _seed(db)
    r = a.verify_chain()
    assert r["ok"] and r["rows"] == 6 and r["legacy_rows"] == 0 and r["head"] == a.head()
    rows = sorted(a.recent(50), key=lambda x: x["id"])
    assert rows[0]["prev_hash"] == "0" * 64
    assert all(rows[i]["prev_hash"] == rows[i - 1]["row_hash"] for i in range(1, len(rows)))


def test_database_refuses_update_and_delete_of_audit_rows():
    db, _ = tmp_db()
    _seed(db, 3)
    for sql in ("UPDATE audit_log SET result='FORGED'", "DELETE FROM audit_log"):
        with pytest.raises(Exception):
            with db.tx() as c:
                c.execute(sql)
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE result='FORGED'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 3


def test_altering_a_row_is_detected_even_after_triggers_are_removed():
    db, _ = tmp_db()
    a = _seed(db)
    _disable_triggers(db)
    with db.tx() as c:
        c.execute("UPDATE audit_log SET result='FORGED' WHERE action='EVENT_2'")
    r = a.verify_chain()
    assert not r["ok"] and r["reason"] == "row content was altered"
    with db.read() as c:
        assert r["first_bad_id"] == c.execute("SELECT id FROM audit_log WHERE action='EVENT_2'").fetchone()["id"]


def test_deleting_a_middle_row_is_detected():
    db, _ = tmp_db()
    a = _seed(db)
    _disable_triggers(db)
    with db.tx() as c:
        c.execute("DELETE FROM audit_log WHERE action='EVENT_3'")
    r = a.verify_chain()
    assert not r["ok"] and "chain link broken" in r["reason"]


def test_secrets_are_still_scrubbed_inside_the_chain():
    db, _ = tmp_db()
    _seed(db, 2)
    with db.read() as c:
        blob = " ".join(str(r["metadata"]) for r in c.execute("SELECT metadata FROM audit_log"))
    assert "hunter2" not in blob and "[redacted]" in blob


def test_rolled_back_transaction_leaves_no_gap_in_the_chain():
    db, _ = tmp_db()
    a = _seed(db, 2)
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            a.record("WILL_ROLL_BACK", "OK", "c", conn=c)
            raise RuntimeError("boom")
    a.record("AFTER", "OK", "c")
    r = a.verify_chain()
    assert r["ok"] and r["rows"] == 3


def test_concurrent_writers_never_fork_the_chain():
    db, target = tmp_db()
    n_threads, per = 6, 15
    errors = []
    barrier = threading.Barrier(n_threads)
    def w(k):
        try:
            d = Database(target); a = AuditLog(d); barrier.wait()
            for i in range(per):
                a.record(f"T{k}", "OK", f"c{k}-{i}")
            d.close()
        except Exception as e:      # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=w, args=(k,)) for k in range(n_threads)]
    [t.start() for t in ts]; [t.join(120) for t in ts]
    assert not errors, errors[:2]
    r = AuditLog(db).verify_chain()
    assert r["ok"] and r["rows"] == n_threads * per


def test_broken_chain_opens_incident_and_closes_the_live_gate_and_blocks_orders():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    assert run(eng.execute(decision("before"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"
    _disable_triggers(db)
    with db.tx() as c:
        c.execute("UPDATE audit_log SET result='FORGED' WHERE id=(SELECT MIN(id) FROM audit_log WHERE row_hash IS NOT NULL)")
    res = safety.verify_audit()
    assert not res["ok"]
    with db.read() as c:
        inc = c.execute("SELECT kind, severity, status FROM incidents WHERE kind='AUDIT_CHAIN_BROKEN'").fetchone()
    assert inc and inc["severity"] == "CRITICAL" and inc["status"] == "OPEN"
    out = run(eng.execute(decision("after"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and "audit log integrity" in out.detail and ad.create_calls == 1


def test_live_gate_requires_audit_integrity_to_have_been_verified():
    db, _ = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)
    safety._set("audit_integrity", "UNVERIFIED", "t")
    g = safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2", symbol="BTC/USDT")
    assert not g.allowed and any("audit log integrity" in r for r in g.reasons)


# ------------------------------------------------------------------ worker lease
def test_only_one_of_two_workers_gets_the_lease_and_expiry_hands_it_over_with_a_new_epoch():
    db, _ = tmp_db()
    a, b = WorkerLease(db, ttl=0.4), WorkerLease(db, ttl=0.4)
    assert a.acquire() and not b.acquire()
    assert a.check() == (True, "") and not b.check()[0]
    first_epoch = a.epoch
    time.sleep(0.6)                                                       # a stops renewing (crash/hang): lease expires
    assert b.acquire() and b.epoch == first_epoch + 1
    ok, why = a.check()
    assert not ok and ("another worker" in why or "expired" in why)
    assert a.acquire() is False                                           # the old owner cannot take it back while b holds it


def test_release_allows_immediate_takeover():
    db, _ = tmp_db()
    a, b = WorkerLease(db, ttl=60), WorkerLease(db, ttl=60)
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire() and not a.check()[0]


def test_renewing_keeps_the_epoch():
    db, _ = tmp_db()
    a = WorkerLease(db, ttl=60)
    assert a.acquire(); e = a.epoch
    assert a.renew() and a.epoch == e


def test_worker_that_cannot_renew_stops_by_itself_even_if_the_database_still_says_it_owns_the_lease():
    db, _ = tmp_db()
    a = WorkerLease(db, ttl=60)
    assert a.acquire()
    a._deadline = time.monotonic() - 1                                    # it has not managed to renew for longer than ttl
    ok, why = a.check()
    assert not ok and "expired locally" in why


def test_stale_worker_cannot_send_a_live_order_and_the_new_owner_can_with_no_duplicate():
    db, _ = tmp_db()
    ad = FakeLive()
    old_eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety)
    old_eng.exchange_ids["fake"] = eid
    old = WorkerLease(db, ttl=0.4); old_eng.submit_authorized = old.check
    new = WorkerLease(db, ttl=60)
    assert old.acquire()
    assert run(old_eng.execute(decision("s1"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"
    time.sleep(0.6)                                                       # old worker is paused past its ttl, then another takes over
    assert new.acquire()
    out = run(old_eng.execute(decision("s2"), bot_id="b", environment="LIVE", exchange="fake"))    # old worker wakes up and tries to send
    assert out.outcome == "BLOCKED" and "may not submit" in out.detail and ad.create_calls == 1
    with db.read() as c:
        assert c.execute("SELECT state FROM orders WHERE id=2").fetchone()["state"] == "FAILED"       # the refused order is recorded, never sent
    new_eng, _ = engine_for(db, ad, exchange_id=eid); new_eng.submit_authorized = new.check
    assert run(new_eng.execute(decision("s3"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"
    assert ad.create_calls == 2
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_lease_does_not_block_paper_orders():
    db, _ = tmp_db()
    paper = PaperAdapter(); paper.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, paper, exchange="paper")
    eng.submit_authorized = lambda: (False, "no lease")
    assert run(eng.execute(decision("p"), bot_id="pb", environment="PAPER", exchange="paper")).outcome == "EXECUTED"


def test_concurrent_acquire_has_exactly_one_winner():
    db, target = tmp_db()
    n = 8
    wins, barrier = [], threading.Barrier(n)
    def w():
        d = Database(target); l = WorkerLease(d, ttl=60); barrier.wait()
        wins.append(l.acquire()); d.close()
    ts = [threading.Thread(target=w) for _ in range(n)]
    [t.start() for t in ts]; [t.join(60) for t in ts]
    assert len(wins) == n and wins.count(True) == 1
    with db.read() as c:
        assert c.execute("SELECT epoch FROM worker_leases WHERE name='execution'").fetchone()["epoch"] == 1


def test_application_acquires_lease_blocks_second_process_and_releases_on_shutdown():
    db, target = tmp_db()
    safety = SafetyController(db); eid = make_live_ready(db, safety)
    mk = lambda: Application(Config(db_target=target, workers_enabled=False, lease_ttl=60), adapters={"fake": FakeLive()})
    a1, a2 = mk(), mk()
    a1.exchange_ids["fake"] = a2.exchange_ids["fake"] = eid
    run(a1.startup())
    s2 = run(a2.startup())
    assert any(s.name == "execution_lease" and not s.ok for s in a2.steps) and s2 == "READY_LIVE_BLOCKED"
    out = run(a2.engine.execute(decision("x"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED"
    run(a1.shutdown())
    assert WorkerLease(Database(target), ttl=60).acquire()                # released at graceful shutdown


# ------------------------------------------------------------------ market-data freshness
def test_gate_blocks_when_freshness_was_never_established_and_when_stale_and_is_per_symbol():
    db, _ = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)                                     # marks BTC/USDT fresh
    gate = lambda sym: safety.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2", symbol=sym)
    assert gate("BTC/USDT").allowed
    g = gate("ETH/USDT")
    assert not g.allowed and any("never confirmed fresh" in r for r in g.reasons)
    stale_open = _candle_open("15m", age_intervals=10)
    safety._set("market_data_candle_at:BTC/USDT", stale_open.isoformat(), "t")
    safety._set("market_data_timeframe:BTC/USDT", "15m", "t")
    g = gate("BTC/USDT")
    assert not g.allowed and any("stale" in r for r in g.reasons)
    safety.mark_market_data_fresh("BTC/USDT", _candle_open("15m"), "15m")
    assert gate("BTC/USDT").allowed


def test_stale_market_data_blocks_a_live_order_and_nothing_is_created_or_sent():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    safety._set("market_data_candle_at:BTC/USDT", _candle_open("15m", age_intervals=10).isoformat(), "t")
    safety._set("market_data_timeframe:BTC/USDT", "15m", "t")
    out = run(eng.execute(decision("stale"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and "stale" in out.detail and ad.create_calls == 0
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_freshness_is_checked_again_immediately_before_sending():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    real = eng._send
    async def stale_then_send(order_id, adapter, scope):                  # data goes stale between order creation and the send
        safety._set("market_data_candle_at:BTC/USDT", _candle_open("15m", age_intervals=10).isoformat(), "t")
        safety._set("market_data_timeframe:BTC/USDT", "15m", "t")
        return await real(order_id, adapter, scope)
    eng._send = stale_then_send
    out = run(eng.execute(decision("late"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED" and ad.create_calls == 0


def test_market_worker_marks_symbols_fresh_and_a_failing_provider_does_not():
    db, target = tmp_db()
    safety = SafetyController(db); eid = make_live_ready(db, safety)
    safety._set("market_data_candle_at:BTC/USDT", _candle_open("15m", age_intervals=10).isoformat(), "t")
    safety._set("market_data_timeframe:BTC/USDT", "15m", "t")
    calls = {"fail": False}
    async def provider(sym, tf):
        if calls["fail"]:
            raise RuntimeError("provider down")
        step = timedelta(seconds=seconds(tf))
        end_open = _candle_open(tf)
        rows = [Candle(t=end_open - step * i, o=100, h=101, l=99, c=100, v=1)
                for i in reversed(range(60))]
        return MarketState(symbol=sym, as_of=end_open + step, candles=rows)
    gate = lambda: SafetyController(db).live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2", symbol="BTC/USDT")
    async def scenario():
        app = Application(Config(db_target=target, workers_enabled=True, lease_ttl=60), adapters={"fake": FakeLive()}, state_provider=provider)
        app.exchange_ids["fake"] = eid
        await app.startup()
        try:
            worker = next(w for w in app.supervisor.workers if w.name == "market_data")
            calls["fail"] = True
            await app.supervisor.run_once(worker)
            assert not gate().allowed and any("market data" in r for r in gate().reasons)
            calls["fail"] = False
            await app.supervisor.run_once(worker)
            assert not any("market data" in r for r in gate().reasons)
        finally:
            await app.shutdown()
    run(scenario())


def _candle_open(timeframe, age_intervals=0):
    step = timedelta(seconds=seconds(timeframe))
    return floor(datetime.now(timezone.utc), timeframe) - step * (1 + age_intervals)
