"""Phase 4.4/4.5 HTTP tests against the REAL FastAPI app: TestClient for request/response behavior, and a real Uvicorn server + httpx for the
SSE stream (TestClient cannot consume an endless stream).

STATUS: WRITTEN BUT NOT EXECUTED where this was built (FastAPI/httpx/uvicorn could not be installed: package index blocked).
Never counts as verification until it has run. With DEWAKO_REQUIRE_HTTP_TESTS=1 (CI) missing dependencies FAIL the test; otherwise it skips.
Treat the first real run as a debugging session.
"""
import base64
import json
import os
import threading
import time
import unittest

from test_api_testclient import _need, _world, _client, _login, PW  # noqa: F401  (shared real-app helpers)
from test_strategy_packages import build, raw_zip

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None


def _seed(rt, kind="ok"):
    """Real engine, scripted fake exchange (a test double: no network, no real exchange, live is not enabled by the product)."""
    from hardening_helpers import FakeLive, decision, engine_for, make_live_ready, run
    st = getattr(rt, "_seed_state", None)
    if st is None:
        ad = FakeLive("ok")
        eng, safety = engine_for(rt.db, ad)
        eng.exchange_ids["fake"] = make_live_ready(rt.db, safety)
        st = rt._seed_state = {"ad": ad, "eng": eng, "n": 0}
    st["n"] += 1
    st["ad"].mode = "timeout_absent" if kind == "unknown" else "ok"
    st["ad"].get_fails = kind == "unknown"
    return run(st["eng"].execute(decision(f"api{st['n']}-{int(time.time()*1000)}"), bot_id="b", environment="LIVE", exchange="fake")).outcome


def _b64(b):
    return {"archive_base64": base64.b64encode(b).decode()}


# ------------------------------------------------------------------ /auth/me
def test_auth_me_returns_identity_and_permission_names_and_requires_a_session():
    c, h = _world()
    assert c.get("/auth/me").status_code == 401
    assert c.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    me = c.get("/auth/me", headers=h["viewer"]).json()
    assert me["username"] == "viewer" and me["role"] == "VIEWER" and me["permissions"] == ["read"]
    admin = c.get("/auth/me", headers=h["admin"]).json()
    assert admin["role"] == "ADMIN" and "live_trading" in admin["permissions"]
    assert set(me) == {"username", "role", "permissions"}                       # nothing else (no token, hash, id)
    assert c.post("/auth/logout", headers=h["viewer"]).status_code == 204
    assert c.get("/auth/me", headers=h["viewer"]).status_code == 401           # a revoked session stops working immediately


# ------------------------------------------------------------------ /ui
def test_ui_routes_serve_only_allowlisted_assets_with_a_strict_policy():
    c = _client()
    from dewako.api import main as _m
    if not (_m._ui_dir() / "index.html").is_file():
        raise unittest.SkipTest("dashboard files not present (backend-only image); set DEWAKO_UI_DIR to run")
    from dewako.api.http_policy import UI_ASSETS
    r = c.get("/ui", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"
    page = c.get("/ui/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-eval'" not in csp and "script-src 'self' 'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "*" not in csp
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["x-frame-options"] == "DENY" and "no-store" in page.headers["cache-control"]
    assert "<script>" not in page.text and "onclick=" not in page.text           # no inline script: the policy would block it
    for name, ctype in UI_ASSETS.items():
        a = c.get("/ui/" + name)
        assert a.status_code == 200 and a.headers["content-type"].startswith(ctype.split(";")[0]) and a.headers["x-content-type-options"] == "nosniff"
    for bad in ("/ui/index.html.bak", "/ui/secret.js", "/ui/..%2f..%2fbackend%2fdewako%2fapi%2fmain.py", "/ui/%2e%2e/%2e%2e/etc/passwd", "/ui/app.js/x", "/ui/.env"):
        r = c.get(bad)
        assert r.status_code == 404 and "def " not in r.text and "root:" not in r.text, bad
        assert r.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------ /activity and /activity/status
def test_activity_history_requires_auth_has_the_documented_schema_and_paginates_without_duplicates():
    c, h = _world()
    assert c.get("/activity").status_code == 401 and c.get("/activity/status").status_code == 401
    for _ in range(3):
        _seed(c.rt)
    r = c.get("/activity?limit=5", headers=h["viewer"])
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"events", "next_cursor", "has_more"}
    ev0 = body["events"][0]
    for k in ("event_id", "ts", "type", "category", "severity", "environment", "correlation_id", "summary", "details", "status", "source", "reconciliation_required"):
        assert k in ev0, k
    seen, cur, pages = [], None, 0
    while True:
        q = "/activity?limit=7" + (f"&cursor={cur}" if cur else "")
        b = c.get(q, headers=h["viewer"]).json()
        seen += [e["event_id"] for e in b["events"]]
        pages += 1
        cur = b["next_cursor"]
        if not b["has_more"] or pages > 100:
            break
    assert len(seen) == len(set(seen)) and len(seen) > 7 and pages > 1
    assert c.get("/activity?cursor=%%%bad", headers=h["viewer"]).status_code == 422
    assert c.get("/activity?severity=NOPE", headers=h["viewer"]).status_code == 422
    assert c.get("/activity?since=not-a-date", headers=h["viewer"]).status_code == 422
    assert c.get("/activity?limit=100000", headers=h["viewer"]).status_code == 200      # clamped, never unbounded
    flt = c.get("/activity?category=order", headers=h["viewer"]).json()["events"]
    assert flt and all(e["category"] == "order" for e in flt)
    assert c.get("/activity?q=" + "x" * 5000, headers=h["viewer"]).status_code in (200, 422)


def test_activity_status_and_an_unknown_order_stays_unknown_across_reads():
    c, h = _world()
    assert c.get("/activity/status", headers=h["viewer"]).json()["unresolved_orders"] == 0
    assert _seed(c.rt, "unknown") != "EXECUTED"
    st = c.get("/activity/status", headers=h["viewer"]).json()
    assert st["level"] == "ERROR" and st["unresolved_orders"] >= 1 and set(st) >= {"level", "reasons", "unresolved_orders", "kill_switch"}
    with c.rt.db.read() as cn:
        before = [r["state"] for r in cn.execute("SELECT state FROM orders ORDER BY id").fetchall()]
    assert before and before[-1] in ("UNKNOWN", "RECONCILING")
    for _ in range(3):
        ev = c.get("/activity?status=UNKNOWN", headers=h["viewer"]).json()["events"]
        assert ev and all(e["reconciliation_required"] is True and e["severity"] in ("ERROR", "CRITICAL") for e in ev)
        assert not [e for e in c.get("/activity?limit=200", headers=h["viewer"]).json()["events"] if e["type"] == "ORDER_FILLED" and e["status"] == "FILLED"
                    and e["related_id"] == ev[0]["related_id"]]
    with c.rt.db.read() as cn:
        assert [r["state"] for r in cn.execute("SELECT state FROM orders ORDER BY id").fetchall()] == before   # reading never changes an order
    for path in ("/activity/retry", "/activity/orders/1/cancel", "/orders/1/retry"):
        assert c.post(path, json={}, headers=h["admin"]).status_code in (404, 405)


# ------------------------------------------------------------------ strategy package routes
def test_package_upload_routes_over_http_validate_authorize_and_never_activate():
    c, h = _world()
    good = build("Http Pkg A")
    assert c.post("/strategy-packages", json=_b64(good)).status_code == 401
    assert c.post("/strategy-packages", json=_b64(good), headers=h["viewer"]).status_code == 403
    r = c.post("/strategy-packages", json=_b64(good), headers=h["analyst"])
    assert r.status_code == 201, r.text
    p = r.json()
    import hashlib
    assert p["state"] == "VALIDATED" and p["name"] == "Http Pkg A" and len(p["fingerprint"]) == 64
    assert p.get("package_sha256") == hashlib.sha256(good).hexdigest()           # the SERVER computed it
    assert c.post("/strategy-packages", json=_b64(good), headers=h["analyst"]).status_code in (409, 422)   # duplicate
    assert c.post("/strategy-packages", json={"archive_base64": "!!notbase64!!"}, headers=h["analyst"]).status_code == 422
    assert c.post("/strategy-packages", json={"archive_base64": "A" * 3_000_000}, headers=h["analyst"]).status_code == 422   # over the size bound
    evil = c.post("/strategy-packages", json=_b64(raw_zip([("../../x.py", b"import os"), ("manifest.json", b"{}")])), headers=h["analyst"])
    assert evil.status_code == 422 and "Traceback" not in evil.text and "import os" not in evil.text
    lst = c.get("/strategy-packages", headers=h["analyst"]).json()
    assert [x["name"] for x in lst].count("Http Pkg A") == 1                      # exactly one record: duplicate and malicious left nothing behind
    assert c.get("/strategy-packages", headers=h["viewer"]).status_code == 403
    assert c.get(f"/strategy-packages/{p['id']}", headers=h["analyst"]).status_code == 200
    assert c.get("/strategy-packages/99999", headers=h["analyst"]).status_code == 404
    assert c.post(f"/strategy-packages/{p['id']}/transition", json={"to": "BACKTEST_ELIGIBLE", "reason": "x"}, headers=h["viewer"]).status_code == 403
    assert c.get(f"/strategy-packages/{p['id']}", headers=h["analyst"]).json()["state"] == "VALIDATED"
    feed = c.get("/activity?category=strategy_package&limit=50", headers=h["viewer"]).json()["events"]
    assert any("Http Pkg A" in e["summary"] for e in feed) and "import os" not in json.dumps(feed)
    with c.rt.db.read() as cn:
        assert cn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0           # import never submits an order
    from dewako.strategies import base as sbase
    assert ("Http Pkg A", "1.0") not in sbase._REGISTRY                               # and never registers/activates a strategy


# ------------------------------------------------------------------ real Uvicorn: the SSE stream
def _serve(rt):
    _need()
    import socket
    import uvicorn
    from dewako.api import main
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started, "uvicorn did not start"
    return srv, t, f"http://127.0.0.1:{port}"


def _stop(srv, t):
    srv.should_exit = True
    t.join(15)


def _frames(url, headers, stop, seconds=12):
    """Read SSE frames until stop(frames) is true or the deadline passes. Returns the list of (event, id, data|None) and comment lines."""
    out, cur, deadline = [], {}, time.monotonic() + seconds
    with httpx.stream("GET", url, headers=headers, timeout=httpx.Timeout(seconds, connect=5)) as r:
        out.append(("HTTP", r.status_code, None))
        if r.status_code != 200:
            return out
        try:
            for line in r.iter_lines():
                if line.startswith(":"):
                    out.append(("comment", None, line))
                elif line == "":
                    if cur.get("event"):
                        out.append((cur["event"], cur.get("id"), json.loads(cur["data"]) if "data" in cur else None))
                    cur = {}
                elif ":" in line:
                    k, v = line.split(":", 1)
                    cur[k] = v.strip()
                if stop(out) or time.monotonic() > deadline:
                    break
        except httpx.ReadTimeout:
            pass
    return out


def _with_fast_stream(fn):
    from dewako.activity import stream as S
    old = (S.POLL_INTERVAL, S.HEARTBEAT)
    S.POLL_INTERVAL, S.HEARTBEAT = 0.1, 0.8
    try:
        return fn()
    finally:
        S.POLL_INTERVAL, S.HEARTBEAT = old


def test_sse_stream_over_real_http_hello_events_heartbeat_resume_and_no_duplicates():
    c, h = _world()
    srv, t, base = _serve(c.rt)
    try:
        def body():
            url = base + "/activity/stream"
            assert httpx.get(url, timeout=5).status_code == 401                              # unauthenticated
            frames = []
            def producer():
                time.sleep(1.0); _seed(c.rt); _seed(c.rt)
            th = threading.Thread(target=producer); th.start()
            def checkpoint_received(f):
                activities = [x for x in f if x[0] == "activity"]
                return len(activities) >= 3 and bool(activities[-1][1]) and any(x[0] == "comment" for x in f)
            frames = _frames(url, h["viewer"], checkpoint_received, seconds=15)
            th.join()
            assert frames[0] == ("HTTP", 200, None)
            kinds = [x[0] for x in frames]
            assert kinds[1] == "hello" and "activity" in kinds and "comment" in kinds           # hello first, events delivered, heartbeat seen
            activity_frames = [x for x in frames if x[0] == "activity"]
            first_ids = [x[2]["event_id"] for x in activity_frames]
            assert len(first_ids) == len(set(first_ids))
            assert activity_frames[-1][1], "disconnect checkpoint must follow the last delivered activity frame"
            last_cursor = activity_frames[-1][1]
            from dewako.activity.service import SRC_KEYS, decode_cursor
            marks = decode_cursor(last_cursor)
            for event_id in first_ids:
                source, row_id = event_id.rsplit(":", 1)
                assert marks[SRC_KEYS[source]] >= int(row_id), f"cursor does not cover delivered event {event_id}"
            # disconnect happened (we left the context). More events occur while disconnected:
            _seed(c.rt)
            again = _frames(url, dict(h["viewer"], **{"Last-Event-ID": last_cursor}), lambda f: sum(1 for x in f if x[0] == "activity") >= 1, seconds=10)
            new_ids = [x[2]["event_id"] for x in again if x[0] == "activity"]
            assert new_ids and not (set(new_ids) & set(first_ids)), "resume replayed events the client already had"
            history = c.get("/activity?limit=200", headers=h["viewer"]).json()
            assert not history["has_more"]
            allids = {e["event_id"] for e in history["events"]}
            produced = set(first_ids) | set(new_ids)
            assert produced <= allids                                                          # only real stored events were streamed
            bad = _frames(url + "?cursor=%%%", h["viewer"], lambda f: True, seconds=5)
            assert bad[0][1] == 422
        _with_fast_stream(body)
    finally:
        _stop(srv, t)


def test_sse_stream_closes_when_the_session_is_revoked_and_caps_concurrent_streams():
    c, h = _world()
    srv, t, base = _serve(c.rt)
    from dewako.api import main
    from dewako.activity.stream import StreamLimiter
    try:
        def body():
            url = base + "/activity/stream"
            tok = h["trader"]
            def revoke():
                time.sleep(1.2); httpx.post(base + "/auth/logout", headers=tok, timeout=5)
            th = threading.Thread(target=revoke); th.start()
            frames = _frames(url, tok, lambda f: any(x[0] == "closed" for x in f), seconds=15)
            th.join()
            closed = [x for x in frames if x[0] == "closed"]
            assert closed and closed[0][2]["reason"] == "unauthorized", frames
            main._stream_limiter = StreamLimiter(1)
            held = threading.Event(); release = threading.Event()
            def hold():
                with httpx.stream("GET", url, headers=h["viewer"], timeout=20) as r:
                    held.set(); release.wait(10)
            ht = threading.Thread(target=hold, daemon=True); ht.start()
            assert held.wait(10)
            time.sleep(0.5)
            assert httpx.get(url, headers=h["admin"], timeout=5).status_code == 429          # bounded concurrency
            release.set(); ht.join(10)
        _with_fast_stream(body)
    finally:
        main._stream_limiter = None
        _stop(srv, t)


def test_sse_frames_never_leak_exception_text_when_the_backend_fails():
    c, h = _world()
    srv, t, base = _serve(c.rt)
    from dewako.activity import service as SV
    real = SV.ActivityService.poll
    def boom(self, *a, **k):
        raise RuntimeError("SECRET-DB-PASSWORD hunter2 /srv/path")
    try:
        def body():
            SV.ActivityService.poll = boom
            frames = _frames(base + "/activity/stream", h["viewer"], lambda f: any(x[0] == "closed" for x in f), seconds=20)
            txt = json.dumps(frames)
            assert any(x[0] == "error" for x in frames) and "hunter2" not in txt and "SECRET" not in txt and "Traceback" not in txt
        _with_fast_stream(body)
    finally:
        SV.ActivityService.poll = real
        _stop(srv, t)
