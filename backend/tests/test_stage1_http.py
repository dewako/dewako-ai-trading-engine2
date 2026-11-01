"""Stage 1 HTTP tests against the real FastAPI app (routing, auth, lifespan, Application, workers, database).

EVIDENCE STATUS: these tests were WRITTEN in an environment where FastAPI/httpx could not be installed, so they have NOT been executed by the
author. They skip locally without FastAPI and FAIL (by design) when DEWAKO_REQUIRE_HTTP_TESTS=1, which the CI jobs set. Until a run with
FastAPI is recorded they count as "not run" in the Stage 1 evidence matrix.

Market data is injected by this test file only, through the Python-level `set_application_factory` seam; no route or setting can feed prices.
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

import scenarios as S  # noqa: E402

PW = "correct horse battery"
BOOT = {"X-Bootstrap-Token": "boot-secret-123"}
DATA = S.steady_uptrend()
NOW = DATA[-1].t + S.STEP
LAST = DATA[-1].c
_ENV = {"DEWAKO_MAX_SPREAD_PCT": "0.5", "DEWAKO_MAX_PRICE_DEVIATION_PCT": "1.0", "DEWAKO_MAX_QUOTE_AGE_SECONDS": "120",
        "DEWAKO_BOOTSTRAP_TOKEN": "boot-secret-123"}


def _need():
    if TestClient is None:
        if os.environ.get("DEWAKO_REQUIRE_HTTP_TESTS") == "1":
            raise AssertionError("DEWAKO_REQUIRE_HTTP_TESTS=1 but FastAPI/httpx are not installed: HTTP tests did not run")
        raise unittest.SkipTest("fastapi is not installed")


def _target():
    base = os.environ.get("DEWAKO_TEST_PG_URL") if os.environ.get("DEWAKO_TEST_DB") == "postgres" else None
    if base:
        import uuid
        return f"{base}?schema=h_{uuid.uuid4().hex[:14]}"
    return os.path.join(tempfile.mkdtemp(), "api.db")


class _Env:
    def __enter__(self):
        from cryptography.fernet import Fernet
        self.saved = {k: os.environ.get(k) for k in list(_ENV) + ["ENCRYPTION_KEY"]}
        os.environ.update(_ENV)
        os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _factory(target, with_provider=True, paper=True):
    from dewako.runtime.application import Application, Config
    from dewako.core.types import MarketState
    box = {"now": NOW}

    async def prov(symbol, timeframe):
        return MarketState(symbol=symbol, as_of=box["now"], candles=DATA)

    def make(existing):
        cfg = Config(db_target=target, workers_enabled=True, paper_enabled=paper, market_data="none", lease_ttl=60,
                     bot_interval=3600, exit_interval=3600, price_interval=3600, risk_interval=3600, market_interval=3600,
                     recon_interval=3600, recover_interval=3600, health_interval=3600, audit_verify_interval=3600)
        a = Application(cfg, rt=existing, state_provider=prov if with_provider else None, clock=lambda: box["now"])
        a._clock_box = box
        return a
    return make


def _client(target, **kw):
    _need()
    from dewako.api import main
    from dewako.api.runtime_state import Runtime, set_application_factory, set_runtime
    rt = Runtime(target)
    set_runtime(rt)
    set_application_factory(_factory(target, **kw))
    return TestClient(main.app, raise_server_exceptions=False)


def _login(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _users(c):
    assert c.post("/auth/bootstrap", json={"username": "root", "password": PW}, headers=BOOT).status_code == 201
    h = {"admin": _login(c, "root", PW)}
    for name, role in (("trader", "TRADER"), ("viewer", "VIEWER")):
        r = c.post("/users", json={"username": name, "password": PW + name, "role": role}, headers=h["admin"])
        assert r.status_code == 201, r.text
        h[name] = _login(c, name, PW + name)
    return h


def _tick(app, name):
    w = next(x for x in app.supervisor.workers if x.name == name)
    return asyncio.run(app.supervisor.run_once(w))


BOT = {"bot_id": "p1", "strategy": "Trend AI", "version": "2.2", "symbol": "BTC/USDT", "timeframe": "15m", "exchange": "paper"}


class HttpLifecycle(unittest.TestCase):
    def test_create_bot_over_http_then_see_an_order_and_a_protected_position(self):
        _need()
        with _Env():
            target = _target()
            with _client(target) as c:
                h = _users(c)
                app = c.app.state.application
                self.assertEqual(c.get("/health").json()["state"], "READY")
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
                self.assertEqual(c.post("/bots", json=BOT, headers=h["trader"]).status_code, 403)      # not an admin action
                self.assertEqual(c.post("/bots", json=BOT, headers=h["viewer"]).status_code, 403)
                self.assertEqual(c.post("/bots", json={**BOT, "symbol": "XAU/USD"}, headers=h["admin"]).status_code, 409)
                self.assertEqual(c.post("/bots", json={**BOT, "environment": "LIVE"}, headers=h["admin"]).status_code, 409)
                self.assertEqual(c.post("/bots", json=BOT, headers=h["admin"]).status_code, 201)
                self.assertEqual(c.post("/bots/p1/enable", headers=h["admin"]).status_code, 200)
                self.assertEqual([b["bot_id"] for b in c.get("/bots", headers=h["viewer"]).json()], ["p1"])
                _tick(app, "exit_monitor"); _tick(app, "risk_equity"); _tick(app, "bot_runner")
                orders = c.get("/orders", headers=h["viewer"]).json()
                self.assertEqual([o["side"] for o in orders], ["BUY"])
                pos = c.get("/positions", headers=h["viewer"]).json()
                self.assertEqual(len(pos), 1)
                self.assertEqual(pos[0]["status"], "OPEN")
                self.assertIsNotNone(pos[0]["stop_loss"])
                self.assertLess(pos[0]["stop_loss"], pos[0]["average_price"])

    def test_position_survives_an_application_restart_and_is_closed_by_its_stop(self):
        _need()
        with _Env():
            target = _target()
            with _client(target) as c:
                h = _users(c)
                app = c.app.state.application
                app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=app.clock())
                c.post("/bots", json=BOT, headers=h["admin"]); c.post("/bots/p1/enable", headers=h["admin"])
                _tick(app, "exit_monitor"); _tick(app, "risk_equity"); _tick(app, "bot_runner")
                before = c.get("/positions", headers=h["admin"]).json()[0]
            # the context manager exit ran the lifespan shutdown: the application "died". Start a new one on the same database.
            with _client(target) as c2:
                h2 = {"admin": _login(c2, "root", PW)}
                app2 = c2.app.state.application
                after = c2.get("/positions", headers=h2["admin"]).json()[0]
                self.assertEqual((after["stop_loss"], after["take_profit"], after["quantity"], after["status"]),
                                 (before["stop_loss"], before["take_profit"], before["quantity"], "OPEN"))
                app2.adapters["paper"].set_price("BTC/USDT", before["stop_loss"] * 0.95, as_of=app2.clock())
                _tick(app2, "exit_monitor")
                self.assertEqual(c2.get("/positions", headers=h2["admin"]).json()[0]["status"], "CLOSED")
                self.assertEqual(sorted(o["side"] for o in c2.get("/orders", headers=h2["admin"]).json()), ["BUY", "SELL"])


class HttpReadinessAndRisk(unittest.TestCase):
    def test_nothing_configured_is_not_ready_over_http(self):
        _need()
        with _Env():
            with _client(_target(), with_provider=False, paper=False) as c:
                r = c.get("/health").json()
                self.assertEqual((r["status"], r["state"]), ("not_ready", "NOT_CONFIGURED"))

    def test_risk_limits_are_admin_changeable_validated_audited_and_report_unenforced_controls(self):
        _need()
        with _Env():
            with _client(_target()) as c:
                h = _users(c)
                g = c.get("/risk/limits", headers=h["viewer"]).json()
                self.assertEqual(g["source"], "DEFAULT")
                self.assertEqual(g["controls"]["high_impact_news_blackout"]["status"], "NOT_ENFORCED")
                self.assertEqual(c.put("/risk/limits", json={"limits": {"max_drawdown_pct": 4}}, headers=h["trader"]).status_code, 403)
                self.assertEqual(c.put("/risk/limits", json={"limits": {"max_position_pct": 900}}, headers=h["admin"]).status_code, 422)
                self.assertEqual(c.put("/risk/limits", json={"limits": {"require_stop_loss": False}}, headers=h["admin"]).status_code, 422)
                ok = c.put("/risk/limits", json={"limits": {"max_drawdown_pct": 4}, "reason": "tighten"}, headers=h["admin"])
                self.assertEqual(ok.status_code, 200, ok.text)
                g2 = c.get("/risk/limits", headers=h["viewer"]).json()
                self.assertEqual((g2["source"], g2["version"], g2["limits"]["max_drawdown_pct"]), ("STORED", 1, 4.0))

    def test_concurrent_style_second_bootstrap_is_refused_and_bootstrap_is_rate_limited(self):
        _need()
        with _Env():
            with _client(_target()) as c:
                self.assertEqual(c.post("/auth/bootstrap", json={"username": "root", "password": PW}, headers=BOOT).status_code, 201)
                self.assertEqual(c.post("/auth/bootstrap", json={"username": "r2", "password": PW}, headers=BOOT).status_code, 409)
                codes = [c.post("/auth/bootstrap", json={"username": f"x{i}", "password": PW}, headers={"X-Bootstrap-Token": "wrong"}).status_code
                         for i in range(30)]
                self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
