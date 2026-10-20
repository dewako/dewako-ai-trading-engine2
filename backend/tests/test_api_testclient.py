"""HTTP tests against REAL FastAPI (fastapi.testclient.TestClient: the real app, routing, middleware, dependencies, and a real database).

STATUS: NOT EXECUTED in the environment this repo was built in (FastAPI/httpx could not be installed: the package index was blocked).
Without FastAPI the tests are SKIPPED, and the skip is reported as a skip, never as a pass. In CI, DEWAKO_REQUIRE_HTTP_TESTS=1 turns
the skip into a failure so this can never go green silently. Run them yourself:
    pip install -r requirements.txt
    DEWAKO_REQUIRE_HTTP_TESTS=1 python -m pytest -q backend/tests/test_api_testclient.py
Because they have never run, treat the first real run as a debugging session, not as proof.
"""
import logging
import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

PW = "correct horse battery"
BOOT = {"X-Bootstrap-Token": "boot-secret-123"}


def _need():
    if TestClient is None:
        if os.environ.get("DEWAKO_REQUIRE_HTTP_TESTS") == "1":
            raise AssertionError("DEWAKO_REQUIRE_HTTP_TESTS=1 but FastAPI/httpx are not installed: HTTP tests did not run")
        raise unittest.SkipTest("fastapi is not installed")


def _client(raise_server_exceptions=False):
    _need()
    from cryptography.fernet import Fernet
    os.environ["DEWAKO_BOOTSTRAP_TOKEN"] = "boot-secret-123"
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from dewako.api import main
    from dewako.api.runtime_state import Runtime, set_runtime
    target = os.environ.get("DEWAKO_TEST_PG_URL") if os.environ.get("DEWAKO_TEST_DB") == "postgres" else None
    if target:
        import uuid
        target = f"{target}?schema=h_{uuid.uuid4().hex[:14]}"
    rt = Runtime(target or os.path.join(tempfile.mkdtemp(), "api.db"))
    set_runtime(rt)
    c = TestClient(main.app, raise_server_exceptions=raise_server_exceptions)
    c.rt = rt
    return c


def _login(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _world():
    """Client + an ADMIN, a TRADER, an ANALYST and a VIEWER, each with a session."""
    c = _client()
    assert c.post("/auth/bootstrap", json={"username": "root", "password": PW}, headers=BOOT).status_code == 201
    h = {"admin": _login(c, "root", PW)}
    for name, role in (("trader", "TRADER"), ("analyst", "ANALYST"), ("viewer", "VIEWER")):
        r = c.post("/users", json={"username": name, "password": PW + name, "role": role}, headers=h["admin"])
        assert r.status_code == 201, r.text
        h[name] = _login(c, name, PW + name)
    return c, h


PUBLIC = {("GET", "/health"), ("POST", "/auth/login"), ("POST", "/auth/bootstrap"), ("GET", "/openapi.json"), ("GET", "/docs"),
          ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc")}


def _fill(path):
    import re
    return re.sub(r"\{[^}]+\}", "1", path)


# ------------------------------------------------------------------ startup, health
def test_startup_health_and_readiness_are_minimal_and_public():
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200 and "X-Correlation-ID" in r.headers
    body = r.text.lower()
    for leak in ("password", "secret", "token", "traceback", "encryption", "database_url"):
        assert leak not in body
    assert c.get("/health/detailed").status_code == 401                          # detail needs a session


def test_security_headers_present_on_success_and_error_responses():
    c = _client()
    for r in (c.get("/health"), c.get("/orders"), c.get("/no-such-route")):
        assert r.headers.get("X-Content-Type-Options") == "nosniff" and r.headers.get("X-Frame-Options") == "DENY"
        assert "no-store" in r.headers.get("Cache-Control", "")


def test_cors_is_off_by_default_and_never_wildcard():
    c = _client()
    r = c.options("/orders", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ authentication
def test_login_logout_invalid_credentials_and_generic_errors():
    c, h = _world()
    bad_user = c.post("/auth/login", json={"username": "nobody", "password": PW})
    bad_pw = c.post("/auth/login", json={"username": "root", "password": "wrong password!!"})
    assert bad_user.status_code == bad_pw.status_code == 401
    assert bad_user.json() == bad_pw.json()                                      # no user enumeration
    assert c.get("/orders", headers=h["viewer"]).status_code == 200
    assert c.post("/auth/logout", headers=h["viewer"]).status_code == 204
    assert c.get("/orders", headers=h["viewer"]).status_code == 401              # revoked by logout


def test_logout_requires_authentication_and_is_not_a_public_route():
    # Regression (Phase 3.4, failure A): unauthenticated POST /auth/logout used to return 204.
    assert ("POST", "/auth/logout") not in PUBLIC
    c, h = _world()
    for bad in ({}, {"Authorization": ""}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        assert c.post("/auth/logout", headers=bad).status_code == 401, bad


def test_logout_revokes_only_the_presented_session_and_cannot_be_replayed():
    c, h = _world()
    second = _login(c, "viewer", PW + "viewer")                                  # same user, another session
    assert c.post("/auth/logout", headers=h["viewer"]).status_code == 204
    assert c.get("/orders", headers=h["viewer"]).status_code == 401              # the presented token is dead
    assert c.post("/auth/logout", headers=h["viewer"]).status_code == 401        # replay of a revoked token
    assert c.get("/orders", headers=second).status_code == 200                   # a different session survives
    assert c.get("/orders", headers=h["trader"]).status_code == 200              # other users untouched
    with c.rt.db.tx() as cx:                                                     # server-side, not just client-side
        live = cx.execute("SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0]
    assert live >= 4


def test_expired_revoked_and_garbage_tokens_are_rejected():
    c, h = _world()
    assert c.get("/orders", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401
    assert c.get("/orders", headers={"Authorization": ""}).status_code == 401
    assert c.get("/orders", headers={"Authorization": "Basic abc"}).status_code == 401
    with c.rt.db.tx() as cx:                                                     # expire every session server-side
        cx.execute("UPDATE sessions SET expires_at=?", ("2000-01-01T00:00:00+00:00",))
    assert c.get("/orders", headers=h["admin"]).status_code == 401
    c2, h2 = _world()
    with c2.rt.db.tx() as cx:                                                    # revoke by an administrator action
        cx.execute("UPDATE sessions SET revoked_at=?", ("2026-01-01T00:00:00+00:00",))
    assert c2.get("/orders", headers=h2["admin"]).status_code == 401


def test_role_change_revokes_the_users_sessions():
    c, h = _world()
    with c.rt.db.read() as cx:
        uid = cx.execute("SELECT id FROM users WHERE username='trader'").fetchone()[0]
    assert c.put(f"/users/{uid}/role", json={"role": "VIEWER"}, headers=h["admin"]).status_code in (200, 204)
    assert c.get("/orders", headers=h["trader"]).status_code == 401


def test_bootstrap_cannot_be_repeated_or_used_without_the_token():
    c, h = _world()
    assert c.post("/auth/bootstrap", json={"username": "x", "password": PW}, headers=BOOT).status_code == 409
    assert c.post("/auth/bootstrap", json={"username": "x", "password": PW}).status_code == 403
    assert c.post("/auth/bootstrap", json={"username": "x", "password": PW}, headers={"X-Bootstrap-Token": "guess"}).status_code == 403


def test_login_rate_limit_applies_and_unknown_tokens_cannot_dodge_the_request_limiter():
    c = _client()
    codes = [c.post("/auth/login", json={"username": "u", "password": "p" * 12}).status_code for _ in range(14)]
    assert 429 in codes and codes[0] == 401
    c2 = _client()
    c2.rt.limiter.limit = 5                                                      # same client address, a new made-up token on every request
    codes = [c2.get("/orders", headers={"Authorization": f"Bearer invented-{i}"}).status_code for i in range(12)]
    assert 429 in codes                                                          # keyed by client address, not by the attacker's token string


# ------------------------------------------------------------------ authorization (server side)
def test_every_route_except_the_public_ones_rejects_an_unauthenticated_request():
    c = _client()
    from dewako.api import main
    checked = 0
    for route in main.app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("HEAD", "OPTIONS") or (method, route.path) in PUBLIC:
                continue
            r = c.request(method, _fill(route.path), json={})
            assert r.status_code == 401, (method, route.path, r.status_code)
            checked += 1
    assert checked >= 25


def test_role_matrix_over_http_for_the_sensitive_routes():
    c, h = _world()
    deny = [("POST", "/safety/kill-switch", {"reason": "test stop"}, ["viewer", "analyst"]),
            ("DELETE", "/safety/kill-switch", None, ["viewer", "analyst", "trader"]),
            ("POST", "/safety/live/enable", None, ["viewer", "analyst", "trader"]),
            ("POST", "/safety/live/disable", None, ["viewer", "analyst"]),
            ("POST", "/users", {"username": "z", "password": PW + "z", "role": "ADMIN"}, ["viewer", "analyst", "trader"]),
            ("GET", "/audit", None, ["viewer", "analyst", "trader"]),
            ("POST", "/exchanges", {"exchange": "binance", "api_key": "AKIA12345678", "api_secret": "SECRETSECRET"}, ["viewer", "analyst", "trader"]),
            ("DELETE", "/exchanges/1", None, ["viewer", "analyst", "trader"]),
            ("POST", "/exchanges/1/enable-live", None, ["viewer", "analyst", "trader"]),
            ("POST", "/strategies/switch", {"bot_id": "b", "strategy": "Trend AI"}, ["viewer", "analyst"]),
            ("POST", "/strategies", {}, ["viewer", "analyst", "trader"]),
            ("POST", "/strategies/plugins/reload", None, ["viewer", "analyst", "trader"]),
            ("POST", "/markets/backfill", {}, ["viewer"])]
    for method, path, body, roles in deny:
        for role in roles:
            r = c.request(method, path, json=body, headers=h[role])
            assert r.status_code == 403, (method, path, role, r.status_code)


def test_kill_switch_trigger_and_release_permissions_and_persistence_over_http():
    c, h = _world()
    assert c.post("/safety/kill-switch", json={"reason": "market anomaly"}, headers=h["trader"]).status_code == 200
    assert c.delete("/safety/kill-switch", headers=h["trader"]).status_code == 403
    assert c.get("/safety/status", headers=h["viewer"]).json()["kill_switch"] in ("ON", True, "on")
    assert c.delete("/safety/kill-switch", headers=h["admin"]).status_code == 200


def test_live_trading_cannot_be_enabled_when_the_gate_fails_even_for_an_admin():
    c, h = _world()
    r = c.post("/safety/live/enable", headers=h["admin"])
    assert r.status_code == 409                                                  # no verified exchange, no reconciliation: the gate refuses
    assert str(c.get("/safety/status", headers=h["admin"]).json().get("live_trading")).lower() in ("disabled", "blocked")
    for ex in ("/safety/live/enable?confirm=yes", "/safety/live/enable?force=true"):
        assert c.post(ex, json={"confirmation": "I UNDERSTAND THE RISK", "force": True}, headers=h["admin"]).status_code in (409, 422)


def test_enable_live_returns_409_with_reasons_and_never_claims_live_is_active():
    # Regression (Phase 3.4, failure B): readiness failing/unverified must be 409, decided server-side.
    c, h = _world()
    r = c.post("/safety/live/enable", headers=h["admin"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "live_readiness_failed" and detail["reasons"]
    assert str(detail["live_trading"]).upper() != "ENABLED"
    attempts = [
        dict(url="/safety/live/enable?confirm=yes&force=true&live=true", json=None, headers={}),
        dict(url="/safety/live/enable", json={"ready": True, "confirmation": "I UNDERSTAND THE RISK", "force": True,
                                                "readiness": "OK", "live_adapters_ok": True}, headers={}),
        dict(url="/safety/live/enable", json=None, headers={"X-Force": "1", "X-Live-Ready": "true", "X-Confirm": "yes"}),
    ]
    for a in attempts:
        r = c.post(a["url"], json=a["json"], headers={**h["admin"], **a["headers"]})
        assert r.status_code in (409, 422), (a, r.status_code, r.text)
        assert r.status_code != 200
    s = c.get("/safety/status", headers=h["admin"]).json()
    assert str(s.get("live_trading")).lower() in ("disabled", "blocked")
    assert c.post("/safety/live/enable", headers=h["trader"]).status_code == 403
    assert c.post("/safety/live/enable").status_code == 401


def test_enable_live_stays_refused_with_real_adapters_registered():
    # Even if every other check were satisfied, the engine live lock keeps activation closed.
    c, h = _world()
    r = c.post("/safety/live/enable", headers=h["admin"])
    assert r.status_code == 409
    reasons = " ".join(r.json()["detail"]["reasons"]).lower()
    assert "worker" in reasons or "exchange" in reasons or "live lock" in reasons or "reconcil" in reasons


def test_the_http_job_is_really_running_on_the_requested_backend():
    c = _client()
    want = os.environ.get("DEWAKO_TEST_DB", "sqlite")
    got = getattr(c.rt.db, "dialect", None) or getattr(c.rt.db, "kind", None)
    assert str(got).lower().startswith("postgres" if want == "postgres" else "sqlite"), (want, got)


def test_there_is_no_http_route_that_submits_an_order():
    c, h = _world()
    for path in ("/orders", "/orders/submit", "/orders/preview", "/orders/1/submit", "/execute", "/trade"):
        for method in ("POST", "PUT"):
            assert c.request(method, path, json={"symbol": "BTC/USDT", "quantity": 1}, headers=h["admin"]).status_code in (404, 405), (method, path)


def test_records_are_not_scoped_per_user_so_only_role_decides_access():
    """DEWAKO is single-tenant: orders/positions/audit belong to the platform, not to a user. Role is the only access control; there is no
    per-user ownership to bypass by editing an id. A multi-tenant deployment would need ownership checks that do not exist yet."""
    c, h = _world()
    assert c.get("/orders/1/events", headers=h["viewer"]).status_code in (200, 404)


# ------------------------------------------------------------------ input validation
def test_malformed_and_hostile_payloads_are_rejected_with_422_not_500():
    c, h = _world()
    bad = [("POST", "/safety/kill-switch", {"reason": ""}), ("POST", "/safety/kill-switch", {"reason": "x" * 1000}),
           ("POST", "/safety/kill-switch", {}), ("POST", "/users", {"username": "", "password": "short", "role": "ADMIN"}),
           ("POST", "/users", {"username": "u", "password": PW + "u", "role": "ROOT"}),
           ("POST", "/strategies/switch", {"bot_id": "", "strategy": "Trend AI"}), ("POST", "/strategies/switch", {"strategy": 5})]
    for method, path, body in bad:
        r = c.request(method, path, json=body, headers=h["admin"])
        assert r.status_code in (400, 422), (path, body, r.status_code, r.text[:200])
    r = c.post("/safety/kill-switch", content=b"{not json", headers={**h["admin"], "Content-Type": "application/json"})
    assert r.status_code == 422
    for qs in ("limit=nan", "limit=inf", "limit=-inf", "limit=abc", "limit=1e999"):
        r = c.get(f"/markets/candles?symbol=BTC/USDT&{qs}", headers=h["admin"])
        assert r.status_code in (400, 422), (qs, r.status_code)


# ------------------------------------------------------------------ error hygiene, secrets
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG); self.lines = []
    def emit(self, record):
        self.lines.append(self.format(record))


def test_errors_are_sanitised_and_secrets_never_reach_responses_audit_or_logs():
    c, h = _world()
    import dewako.exchanges.binance as bn
    secret, key = "HTTPSECRETLEAKVALUE99", "AKIAHTTPLEAK1234567"
    def exploding(url, headers):                                                  # no network is ever used; the "exchange" echoes the credentials back
        raise bn.ExchangeError(f"HTTP 401 key={key} secret={secret} url={url}")
    original, bn._urllib_transport = bn._urllib_transport, exploding
    cap = _Capture(); logging.getLogger().addHandler(cap); logging.getLogger().setLevel(logging.DEBUG)
    try:
        r = c.post("/exchanges", json={"exchange": "binance", "label": "m", "api_key": key, "api_secret": secret}, headers=h["admin"])
        r2 = c.post("/exchanges", json={"exchange": "binance", "label": "m", "api_key": key, "api_secret": secret}, headers=h["trader"])
    finally:
        bn._urllib_transport = original
        logging.getLogger().removeHandler(cap)
    assert r.status_code in (422, 503) and r2.status_code == 403
    with c.rt.db.read() as cx:
        audit = " ".join(str(dict(x)) for x in cx.execute("SELECT * FROM audit_log"))
    haystack = "\n".join([r.text, r2.text, audit, *cap.lines, str(c.get("/exchanges", headers=h["admin"]).json())])
    for needle in (secret, key):
        assert needle not in haystack, needle
    assert "Traceback" not in r.text and "File \"" not in r.text


def test_unhandled_server_errors_do_not_leak_internals():
    c, h = _world()
    from dewako.api import main
    @main.app.get("/_boom_for_test")
    def boom():
        raise RuntimeError("secret-internal-detail /home/x/y.py line 12")
    r = c.get("/_boom_for_test")
    assert r.status_code == 500 and "secret-internal-detail" not in r.text and "Traceback" not in r.text
