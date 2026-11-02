"""Phase 4.4 backend: activity read model, redaction, pagination, stream, status. Every event comes from a real stored record."""
import asyncio
import io
import json
import pathlib
import re
import random
import zipfile

import pytest

from dewako.activity import events as E
from dewako.activity.service import ActivityService, BadRequest, Filters, decode_cursor, encode_cursor
from dewako.activity.stream import StreamLimiter, event_stream, frame
from dewako.strategies import base as sbase
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, now_iso
from dewako.strategy_packages.manager import PackageManager
from dewako.strategy_packages.archive import PackageRejected
from hardening_helpers import *
from test_strategy_packages import build, raw_zip, _unreg


def svc(db):
    return ActivityService(db)


def all_events(db, **kw):
    out, cur = [], None
    for _ in range(200):
        r = svc(db).query(Filters(**kw), limit=200, cursor=cur)
        out += r["events"]
        cur = r["next_cursor"]
        if not r["has_more"]:
            return out
    raise AssertionError("did not terminate")


def live(mode="ok", **kw):
    db, _ = tmp_db()
    ad = FakeLive(mode)
    for k, v in kw.items():
        setattr(ad, k, v)
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    return db, eng, ad, safety


# ------------------------------------------------------------------ real instrumentation
def test_events_come_from_real_operations_and_a_fill_is_shown_only_when_the_engine_recorded_one():
    db, eng, ad, _ = live()
    r = run(eng.execute(decision("a1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "EXECUTED"
    ev = all_events(db)
    orders = [e for e in ev if e["category"] == "order"]
    assert orders and {e["status"] for e in orders} >= {"FILLED"}
    assert any(e["type"] == "RISK_APPROVED" and e["strategy"] == "Trend AI" for e in ev)
    cid = orders[0]["correlation_id"]
    assert cid and all(e["correlation_id"] == cid for e in orders)
    with db.read() as c:
        n_oe = c.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
    assert len([e for e in ev if e["source"] == "order_events"]) == n_oe                   # one feed event per stored state change, no extras
    assert not any(e["type"] in ("ORDER_FILLED", "ORDER_SUBMITTED") for e in ev if e["source"] == "audit")   # audit duplicates are not double counted
    o = [e for e in orders if e["status"] == "FILLED"][0]
    assert o["strategy_fingerprint"] and o["environment"] == "LIVE" and o["instrument"] == "BTC/USDT" and o["adapter"] == "fake"
    r2 = run(eng.execute(decision("a1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r2.outcome == "DUPLICATE" and len(all_events(db)) == len(ev)                       # a refused duplicate creates no new order or risk event


def test_a_blocked_order_is_an_authorization_event_and_creates_no_order_event():
    db, eng, ad, safety = live()
    safety.activate_kill_switch(ADMIN, "stop")
    assert run(eng.execute(decision("k1"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"
    ev = all_events(db)
    blocked = [e for e in ev if e["type"] == "ORDER_BLOCKED"]
    assert blocked and blocked[0]["status"] == "BLOCKED" and blocked[0]["severity"] == "WARNING"
    assert not [e for e in ev if e["source"] == "order_events"]
    assert any(e["type"] == "KILL_SWITCH_ENABLED" and e["severity"] == "CRITICAL" for e in ev)


def test_unknown_order_outcome_is_shown_as_unknown_and_reconciliation_required_never_filled_or_failed():
    db, eng, ad, _ = live("timeout_absent", get_fails=True)
    run(eng.execute(decision("u1"), bot_id="b", environment="LIVE", exchange="fake"))
    with db.read() as c:
        assert c.execute("SELECT state FROM orders").fetchone()["state"] in ("UNKNOWN", "RECONCILING")
    ev = [e for e in all_events(db) if e["category"] == "order"]
    last = ev[0]                                                                                # newest first
    assert last["status"] == "UNKNOWN" and last["severity"] == "ERROR" and last["reconciliation_required"] is True
    assert "RECONCILIATION REQUIRED" in last["summary"]
    assert not any(e["status"] in ("FILLED", "PARTIALLY_FILLED") for e in ev)                  # never claimed as filled
    assert ad.create_calls == 1                                                                 # and never blindly resent
    st = svc(db).status()
    assert st["level"] == "ERROR" and st["unresolved_orders"] == 1 and any("reconciliation required" in r for r in st["reasons"])


def test_package_import_and_rejection_are_correlated_events_without_file_contents():
    db, _ = tmp_db()
    m = PackageManager(db, AuditLog(db))
    try:
        m.import_package(build("Gold Feed A"), ADMIN, "cid-import-1")
        marker = "TOPSECRET_MARKER_CONTENT_12345"
        evil = build("Gold Feed B", extra={"tests/x.json": json.dumps({"note": marker})}, manifest_over={"runtime": "python"})
        with pytest.raises(PackageRejected):
            m.import_package(evil, ADMIN, "cid-import-2")
        ev = [e for e in all_events(db) if e["category"] == "strategy_package"]
        ok = [e for e in ev if e["correlation_id"] == "cid-import-1" and e["type"] == "STRATEGY_PACKAGE_IMPORT"][0]
        assert ok["status"] == "OK" and ok["strategy"] == "Gold Feed A" and ok["strategy_version"] == "1.0" and len(ok["strategy_fingerprint"]) == 64
        assert "Gold Feed A v1.0" in ok["summary"]
        rej = [e for e in ev if e["correlation_id"] == "cid-import-2"][0]
        assert rej["status"] == "REJECTED" and rej["severity"] == "WARNING" and rej["details"]["code"] == "UNSUPPORTED_RUNTIME"
        assert marker not in json.dumps(ev)
        assert any(e["type"] == "STRATEGY_PACKAGE_STATE" and e["details"]["to"] == "VALIDATED" for e in ev)
    finally:
        _unreg(m)


def test_engine_startup_and_ai_requests_appear_as_system_and_ai_events():
    db, _ = tmp_db()
    log = AuditLog(db)
    log.record("ENGINE_STARTUP", "DEGRADED", "startup", metadata={"status": "READY_LIVE_BLOCKED", "failed_steps": ["workers"]})
    log.record("AI_REQUEST", "UNAVAILABLE", "c9", resource="ai:market_commentary", metadata={"provider": None, "attempts": [{"provider": "local1", "outcome": "TIMEOUT"}]})
    ev = all_events(db)
    s = [e for e in ev if e["type"] == "ENGINE_STARTUP"][0]
    assert s["category"] == "system" and s["status"] == "DEGRADED" and s["severity"] == "WARNING"
    a = [e for e in ev if e["type"] == "AI_REQUEST"][0]
    assert a["category"] == "ai" and a["severity"] == "WARNING" and a["details"]["attempts"][0]["outcome"] == "TIMEOUT"


def test_events_persist_across_restart_and_the_service_never_writes():
    db, target = tmp_db()
    eng, safety = engine_for(db, FakeLive())
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    run(eng.execute(decision("p1"), bot_id="b", environment="LIVE", exchange="fake"))
    before = [e["event_id"] for e in all_events(db)]
    with db.read() as c:
        counts = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_log", "order_events", "reconciliation_events", "orders")]
    db.close()
    db2 = Database(target)
    s = svc(db2)
    after = [e["event_id"] for e in all_events(db2)]
    s.status(); s.query(Filters(), limit=5); s.poll(s.watermarks_now())
    with db2.read() as c:
        assert counts == [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_log", "order_events", "reconciliation_events", "orders")]
    assert after == before and len(before) > 3


# ------------------------------------------------------------------ filtering, pagination, ordering
def seed(db, n=120):
    log = AuditLog(db)
    rnd = random.Random(7)
    acts = [("LOGIN", "OK", "auth"), ("LOGIN", "DENIED", "auth"), ("SIGNAL_CREATED", "OK", "signal"), ("RISK_REJECTED", "REJECTED", "risk"),
            ("STRATEGY_PACKAGE_STATE", "OK", "strategy_package"), ("AI_REQUEST", "OK", "ai"), ("ALERT", "OK", "safety")]
    for i in range(n):
        a, res, _ = rnd.choice(acts)
        log.record(a, res, f"c{i%9}", actor={"username": rnd.choice(["amy", "bob"]), "role": "ADMIN"}, resource=f"r:{i}",
                   metadata={"name": rnd.choice(["Alpha One", "Beta Two"]), "environment": rnd.choice(["PAPER", "LIVE"]), "i": i})


def test_pagination_matches_a_full_scan_for_any_page_size_and_filter_with_no_gaps_or_duplicates():
    db, eng, ad, _ = live()
    for i in range(6):
        run(eng.execute(decision(f"s{i}"), bot_id="b", environment="LIVE", exchange="fake"))
    seed(db, 130)
    full = all_events(db)
    assert [e["event_id"] for e in full] == [
        e["event_id"] for e in sorted(full, key=lambda e: (e["ts"], e["event_id"]), reverse=True)
    ]
    keys = [(e["ts"], e["event_id"]) for e in full]
    assert keys == sorted(keys, reverse=True) and len({e["event_id"] for e in full}) == len(full)
    for flt in ({}, {"category": "auth"}, {"severity": "WARNING"}, {"strategy": "Alpha One"}, {"q": "login"}, {"environment": "LIVE", "category": "order"},
                {"status": "DENIED"}, {"source": "order_events"}, {"correlation_id": "c3"}):
        want = [e["event_id"] for e in all_events(db, **flt)]
        for size in (1, 7, 33, 200):
            got, cur = [], None
            for _ in range(2000):
                r = svc(db).query(Filters(**flt), limit=size, cursor=cur)
                assert len(r["events"]) <= size
                got += [e["event_id"] for e in r["events"]]
                cur = r["next_cursor"]
                if not r["has_more"]:
                    break
            assert got == want, (flt, size)
        assert all(Filters(**flt).match(e) for e in all_events(db, **flt))


def test_time_range_instrument_and_text_filters_and_bad_filters():
    db, _ = tmp_db()
    log = AuditLog(db)
    log.record("LOGIN", "OK", "x1", actor={"username": "amy", "role": "ADMIN"})
    mid = now_iso()
    log.record("SIGNAL_CREATED", "OK", "x2", actor={"username": "amy", "role": "ADMIN"}, resource="signal:S9")
    assert [e["type"] for e in all_events(db, since=mid)] == ["SIGNAL_CREATED"]
    assert [e["type"] for e in all_events(db, until=mid)] == ["LOGIN"]
    assert [e["type"] for e in all_events(db, q="S9")] == ["SIGNAL_CREATED"]
    for bad in ({"category": "nope"}, {"severity": "LOUD"}, {"source": "x"}, {"since": "yesterday"}, {"until": "'; DROP TABLE audit_log;--"}):
        with pytest.raises(BadRequest):
            Filters(**bad)
    assert all_events(db, q="'; DROP TABLE audit_log;--") == []                       # text is matched in Python, never put in SQL
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2


def test_limits_are_bounded_and_cursors_are_validated():
    db, _ = tmp_db()
    seed(db, 30)
    assert len(svc(db).query(Filters(), limit=10_000)["events"]) <= 200
    assert len(svc(db).query(Filters(), limit=0)["events"]) == 1
    for bad in ("not-base64!!", encode_cursor({"a": -1}), encode_cursor({"x": 1}), encode_cursor({"a": "1"}), encode_cursor({"a": True}), "A" * 300,
                encode_cursor({"a": 10**30})):
        with pytest.raises(BadRequest):
            decode_cursor(bad)
    assert decode_cursor(encode_cursor({"a": 5, "o": 2})) == {"a": 5, "o": 2} and decode_cursor(None) == {}


# ------------------------------------------------------------------ redaction and malformed data
def test_secrets_and_control_characters_never_reach_an_event():
    db, _ = tmp_db()
    log = AuditLog(db)
    key = "sk-LIVEKEYLIVEKEYLIVEKEY1234567890"
    log.record("CREDENTIAL_CREATED", "OK", "c1", actor={"username": "amy", "role": "ADMIN"},
               metadata={"api_key": "abc12345", "nested": {"password": "pw", "ok": 1, "note": f"key {key} end\x07\x1b[31m"}, "blob": "x", "last4": "1234"})
    e = [x for x in all_events(db) if x["type"] == "CREDENTIAL_CREATED"][0]
    text = json.dumps(e)
    assert key not in text and "abc12345" not in text and '"pw"' not in text and "\x07" not in text and "\x1b" not in text
    assert e["details"]["nested"]["ok"] == 1 and e["details"]["last4"] == "1234"


def test_malformed_stored_data_is_contained_not_invented():
    d = E.sanitize_details("{not json")
    assert d == {"_malformed": True} and E.sanitize_details(None) == {} and E.sanitize_details([1, 2]) == {"_malformed": True}
    big = E.sanitize_details({f"k{i}": "word " * 58 for i in range(30)})
    assert big.get("_truncated") is True
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert "[truncated]" in json.dumps(E.sanitize_details(deep))
    ev = E.from_audit({"id": 1, "created_at": "2026-01-01T00:00:00+00:00", "username": None, "role": None, "action": "ALERT", "resource": None, "result": "OK",
                       "correlation_id": "c", "metadata": "{broken"})
    assert ev["details"] == {"_malformed": True} and ev["actor"] == "system"
    db, _ = tmp_db()
    with db.tx() as c:
        c.execute("INSERT INTO audit_log(created_at,action,result,correlation_id,metadata) VALUES (?,?,?,?,?)", (now_iso(), "WEIRD_THING", "OK", "c", "[1,2"))
    assert [e["type"] for e in all_events(db)] == ["WEIRD_THING"]                      # a malformed row still shows, as itself
    assert E.utc("2026-01-01 00:00:00") .startswith("2026-01-01T00:00:00") and E.utc("garbage") == "garbage"


def test_summary_cannot_carry_secrets_or_control_characters():
    s = E.from_audit({"id": 2, "created_at": now_iso(), "username": "amy\x00", "role": "ADMIN", "action": "LOGIN", "resource": "sk-ABCDEFGHIJKLMNOPQRSTUV1234",
                      "result": "OK", "correlation_id": "c", "metadata": "{}"})
    assert "sk-ABCDEF" not in s["summary"] and "\x00" not in s["summary"] and len(s["summary"]) <= E.MAX_SUMMARY


# ------------------------------------------------------------------ stream
class Clk:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def mkclock(step_hook=None):
    """A fake clock whose sleep advances it, so an idle stream always reaches its heartbeat and duration limits (a frozen clock never would)."""
    clk = Clk()

    async def sleep(x):
        clk.t += x
        if step_hook:
            step_hook()
    return clk, sleep


async def drain(gen, n=None, on_frame=None, limit=500):
    out = []
    async for f in gen:
        out.append(f)
        if on_frame:
            on_frame(len(out), f)
        if (n and len(out) >= n) or len(out) >= limit:
            break
    return out


def parse(frames):
    evs = []
    for f in frames:
        if re.search(r"(?m)^event: activity$", f):
            evs.append(json.loads(f.split("data: ", 1)[1].strip()))
    return evs


def ids(frames):
    return [re.search(r"^id: (\S+)", f, re.M).group(1) for f in frames if f.startswith("id:") or "\nid:" in f]


def test_stream_delivers_new_events_once_in_order_and_resumes_after_reconnect_without_gap_or_replay():
    db, _ = tmp_db()
    log = AuditLog(db)
    log.record("LOGIN", "OK", "old")                                          # history before the stream starts is not replayed
    s = svc(db)
    added = []

    def hook():
        if len(added) < 5:
            log.record("SIGNAL_CREATED", "OK", f"n{len(added)}", resource=f"signal:{len(added)}")
            added.append(1)
    clk, sleep = mkclock(hook)
    frames = run(drain(event_stream(s, sleep=sleep, clock=clk, max_duration=8), limit=100))
    assert frames[0].startswith("retry: 3000") and "event: hello" in frames[0] and frames[-1].startswith("event: closed")
    got = parse(frames)
    assert [e["correlation_id"] for e in got] == [f"n{i}" for i in range(5)] and len({e["event_id"] for e in got}) == 5
    assert not any(e["correlation_id"] == "old" for e in got)
    assert ids(frames)


def test_resume_cursor_is_exact_and_replay_is_deduplicable():
    db, _ = tmp_db()
    log = AuditLog(db)
    s = svc(db)
    for i in range(4):
        log.record("SIGNAL_CREATED", "OK", f"e{i}", resource=f"signal:{i}")
    marks0 = {"a": 0, "o": 0, "r": 0}
    clk, sleep = mkclock()
    allf = run(drain(event_stream(s, marks=marks0, sleep=sleep, clock=clk, max_duration=2), limit=50))
    evs = parse(allf)
    assert len(evs) == 4
    cur_after_all = decode_cursor(ids(allf)[-1])
    log.record("SIGNAL_CREATED", "OK", "e4", resource="signal:4")
    clk, sleep = mkclock()
    nxt = parse(run(drain(event_stream(s, marks=cur_after_all, sleep=sleep, clock=clk, max_duration=2), limit=50)))
    assert [e["correlation_id"] for e in nxt] == ["e4"]                                    # exactly the new one: no gap, no replay
    clk, sleep = mkclock()
    replay = parse(run(drain(event_stream(s, marks=marks0, sleep=sleep, clock=clk, max_duration=2), limit=50)))
    seen = {}
    for e in evs + replay:                                                                    # a client that dedupes by event_id ends with one copy each
        seen[e["event_id"]] = e
    assert len(seen) == 5 and len(evs + replay) > len(seen)


def test_stream_heartbeat_authorization_recheck_duration_errors_and_limits():
    db, _ = tmp_db()
    log = AuditLog(db)
    s = svc(db)
    clk, sleep = mkclock()
    fr = run(drain(event_stream(s, sleep=sleep, clock=clk, heartbeat=3, max_duration=10), limit=100))
    assert any(f.startswith(": keepalive") for f in fr) and fr[-1].startswith("event: closed") and '"max_duration"' in fr[-1]
    # authorization revoked while events are flowing: the busy stream must still stop
    ok = {"v": True}
    n = {"i": 0}

    def busy_hook():
        n["i"] += 1
        log.record("SIGNAL_CREATED", "OK", f"b{n['i']}")
        if n["i"] == 4:
            ok["v"] = False
    clk, busy = mkclock(busy_hook)
    fr = run(drain(event_stream(s, sleep=busy, clock=clk, heartbeat=2, max_duration=100, is_authorized=lambda: ok["v"]), limit=200))
    assert '"unauthorized"' in fr[-1] and len(parse(fr)) < 10
    # backend failure: no exception text, bounded retries, then closed
    class Boom(ActivityService):
        def poll(self, *a, **k): raise RuntimeError("password=hunter2 secret stack trace")
    clk, sleep = mkclock()
    fr = run(drain(event_stream(Boom(db), marks={"a": 0, "o": 0, "r": 0}, sleep=sleep, clock=clk), limit=50))
    text = "".join(fr)
    assert "hunter2" not in text and "Traceback" not in text and text.count("event: error") == 5 and fr[-1].startswith("event: closed")
    lim = StreamLimiter(2)
    assert lim.acquire() and lim.acquire() and not lim.acquire()
    lim.release(); assert lim.acquire()


def test_frames_cannot_be_injected_into_and_polls_are_bounded():
    f = frame("activity", {"summary": "line1\nevent: closed\ndata: x\n\n"}, id_="ab\ncd")
    assert f.count("\n\n") == 1 and len(re.findall(r"(?m)^event:", f)) == 1 and len(re.findall(r"(?m)^data:", f)) == 1 and "id: abcd\n" in f and f.startswith("id: abcd\nevent: activity\ndata: {")
    db, _ = tmp_db()
    seed(db, 450)
    evs, new = svc(db).poll({"a": 0, "o": 0, "r": 0})
    assert len(evs) <= 200 and new["a"] <= 200 + 5
    evs2, new2 = svc(db).poll(new)
    assert evs2 and {e["event_id"] for e in evs}.isdisjoint({e["event_id"] for e in evs2})
    ev3, new3 = svc(db).poll(svc(db).watermarks_now())
    assert ev3 == []


def test_filtered_poll_still_advances_the_watermark():
    db, _ = tmp_db()
    log = AuditLog(db)
    for i in range(5):
        log.record("LOGIN", "OK", f"l{i}")
    evs, new = svc(db).poll({"a": 0, "o": 0, "r": 0}, Filters(category="order"))
    assert evs == [] and new["a"] >= 5


# ------------------------------------------------------------------ status
def test_status_is_derived_from_stored_state_only():
    db, _ = tmp_db()
    s = svc(db)
    assert s.status()["level"] == "IDLE"
    ts = now_iso()
    with db.tx() as c:
        c.execute("INSERT INTO worker_health(name,critical,status,last_heartbeat,updated_at) VALUES ('w1',1,'HEALTHY',?,?)", (ts, ts))
    assert s.status()["level"] == "RUNNING"
    with db.tx() as c:
        c.execute("UPDATE worker_health SET status='FAILED' WHERE name='w1'")
    st = s.status()
    assert st["level"] == "DEGRADED" and "w1" in st["reasons"][0]
    with db.tx() as c:
        c.execute("UPDATE worker_health SET status='HEALTHY' WHERE name='w1'")
        c.execute("INSERT INTO incidents(kind,severity,detail,created_at) VALUES ('X','CRITICAL','bad',?)", (ts,))
    assert s.status()["level"] == "ERROR"
    with db.tx() as c:
        c.execute("UPDATE incidents SET status='RESOLVED'")
    assert s.status()["level"] == "RUNNING"
    assert s.status({"audit_log": "TAMPERED"})["level"] == "ERROR" and s.status({"database": "unhealthy"})["level"] == "ERROR"
    assert s.status({"kill_switch": True, "live_trading": "disabled"})["kill_switch"] is True


def test_in_flight_submitting_is_not_an_error_but_a_stale_one_is():
    db, eng, ad, _ = live()
    run(eng.execute(decision("f1"), bot_id="b", environment="LIVE", exchange="fake"))
    with db.tx() as c:
        c.execute("UPDATE orders SET state='SUBMITTING', updated_at=?", (now_iso(),))
    assert svc(db).status()["unresolved_orders"] == 0
    with db.tx() as c:
        c.execute("UPDATE orders SET updated_at='2020-01-01T00:00:00+00:00'")
    assert svc(db).status()["unresolved_orders"] == 1


# ------------------------------------------------------------------ routes (source-level: FastAPI is not installable in this environment)
def test_activity_routes_are_read_only_authenticated_and_the_ui_csp_is_scoped():
    import ast
    src = (pathlib.Path(__file__).resolve().parents[1] / "dewako/api/main.py").read_text()
    found = {}
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and d.args and ast.unparse(d.args[0]).startswith("'/activity"):
                    found[n.name] = (d.func.attr, ast.unparse(n))
    assert set(found) == {"activity_history", "activity_status", "activity_stream"}
    for name, (method, text) in found.items():
        assert method == "get" and "permission('read')" in text, name
    assert "POST" not in src[src.index("Live Activity (Phase 4.4). Read-only"):].split("def ui_index")[0]
    from dewako.api.http_policy import UI_CSP as csp, UI_ASSETS
    directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp and "http:" not in csp and "https:" not in csp
    assert "'unsafe-eval'" not in csp and "*" not in csp
    assert directives["script-src"] == ["'self'"]                                   # no inline script, no eval
    assert directives["style-src"] == ["'self'"] and directives["style-src-attr"] == ["'unsafe-inline'"]   # the one scoped exception: style attributes
    assert set(UI_ASSETS) == {"activity.js", "packages.js", "app.js", "app.css"}


# ------------------------------------------------------------------ Phase 4.4 package-import integration
def test_import_is_atomic_when_the_audit_write_fails_no_row_and_no_event():
    m, db = None, None
    db, _ = tmp_db()
    audit = AuditLog(db)
    m = PackageManager(db, audit)
    real = audit.record
    def boom(*a, **k):
        raise RuntimeError("disk full")
    audit.record = boom
    try:
        m.import_package(build("Atomic Pkg A"), ADMIN, "c-atomic")
        raise AssertionError("import must fail when its audit record cannot be written")
    except RuntimeError:
        pass
    finally:
        audit.record = real
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM strategy_packages").fetchone()[0] == 0
    assert not [e for e in all_events(db) if "Atomic Pkg A" in json.dumps(e)]
    _unreg(m)


def test_import_status_persists_after_restart_and_never_activates_or_orders():
    db, target = tmp_db()
    m = PackageManager(db, AuditLog(db))
    r = m.import_package(build("Persist Pkg A"), ADMIN, "c-p")
    assert r["state"] == "VALIDATED"
    db2 = Database(target)
    m2 = PackageManager(db2, AuditLog(db2))
    got = m2.get(r["id"])
    assert got["state"] == "VALIDATED" and got["fingerprint"] == r["fingerprint"]
    ev = [e for e in all_events(db2) if e["category"] == "strategy_package"]
    assert ev
    with db2.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert ("Persist Pkg A", "1.0") not in sbase._REGISTRY
    _unreg(m); _unreg(m2)


def test_malicious_package_creates_a_rejection_event_without_file_contents():
    db, _ = tmp_db()
    m = PackageManager(db, AuditLog(db))
    secret_marker = "TOP-SECRET-FILE-CONTENT-12345"
    try:
        m.import_package(raw_zip([("../../evil.py", secret_marker.encode()), ("manifest.json", b"{}")]), ADMIN, "c-evil")
        raise AssertionError("must be rejected")
    except PackageRejected:
        pass
    ev = all_events(db)
    rej = [e for e in ev if e["category"] == "strategy_package" and e["severity"] in ("WARNING", "ERROR")]
    assert rej, [e["type"] for e in ev]
    assert secret_marker not in json.dumps(ev)


def test_viewer_cannot_transition_a_package_and_it_is_refused_not_silently_ignored():
    db, _ = tmp_db()
    m = PackageManager(db, AuditLog(db))
    r = m.import_package(build("Perm Pkg A"), ADMIN, "c1")
    viewer = {"user_id": 9, "username": "v", "role": "VIEWER"}
    from dewako.strategy_packages.manager import TransitionRefused
    try:
        m.transition(r["id"], "BACKTEST_ELIGIBLE", viewer, "x", "c2")
        raise AssertionError("viewer must be refused")
    except TransitionRefused:
        pass
    assert m.get(r["id"])["state"] == "VALIDATED"
    _unreg(m)
