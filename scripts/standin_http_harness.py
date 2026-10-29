"""STAND-IN HTTP HARNESS -- NOT THE FASTAPI APP.

FastAPI/httpx cannot be installed in the sandbox this was built in, so the real routes in backend/dewako/api/main.py have never run over HTTP.
To let a real browser exercise the dashboard anyway, this serves the SAME dashboard files and the SAME service objects
(AuthService, ActivityService, event_stream, PackageManager, the execution engine) behind a minimal stdlib HTTP server that mimics the route shapes.
It proves the browser <-> service wiring. It does NOT prove the FastAPI routes, their dependencies, CORS, middleware or CSP headers.
Seeding endpoints (/__harness/*) use test doubles (a scripted fake exchange); no real exchange is ever contacted and live trading is not enabled by it.
"""
import asyncio, base64, binascii, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "tests")]
UI = ROOT / "frontend" / "prototype"

from dewako.api.http_policy import UI_ASSETS, UI_HEADERS   # the SAME policy constants the real app uses
from dewako.api.runtime_state import Runtime
from dewako.auth.service import PERMISSIONS, AuthError, Forbidden
from dewako.activity.service import ActivityService, BadRequest, Filters, decode_cursor
from dewako.activity.stream import event_stream
from dewako.strategy_packages.archive import PackageRejected
from dewako.strategy_packages.manager import TransitionRefused

STATE = {"drop": 0, "stream_open": 0, "seq": 0}


def make(db_path):
    rt = Runtime(db_path)
    try:
        uid = rt.auth.create_user(None, "admin", "correct-horse-battery", "ADMIN", "harness")
        rt.auth.create_user({"user_id": uid, "username": "admin", "role": "ADMIN"}, "viewer", "correct-horse-battery", "VIEWER", "harness")
    except Exception as e:
        print("harness user setup:", e, file=sys.stderr)
    return rt


def seed_placeholder():
    pass


_ENG = {}


def seed(rt, kind):
    from hardening_helpers import FakeLive, decision, engine_for, make_live_ready, run
    if not _ENG:
        ad = FakeLive("ok")
        eng, safety = engine_for(rt.db, ad)
        eng.exchange_ids["fake"] = make_live_ready(rt.db, safety)
        _ENG.update(ad=ad, eng=eng, safety=safety)
    ad, eng, safety = _ENG["ad"], _ENG["eng"], _ENG["safety"]
    ad.mode = "timeout_absent" if kind == "unknown" else "ok"
    ad.get_fails = kind == "unknown"          # the exchange cannot confirm the order, so the outcome stays UNKNOWN
    STATE["seq"] += 1
    if kind == "blocked":
        safety.activate_kill_switch({"user_id": 1, "username": "admin", "role": "ADMIN"}, "harness drill")
    return run(eng.execute(decision(f"h{STATE['seq']}-{int(time.time()*1000)}"), bot_id="b", environment="LIVE", exchange="fake")).outcome


def handler_for(rt):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass

        def _send(self, code, body=None, ctype="application/json", extra=None):
            raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(raw)))
            if "Cache-Control" not in (extra or {}): self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(raw)

        def _actor(self, perm=None):
            tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            try:
                a = rt.auth.authenticate(tok)
            except AuthError:
                self._send(401, {"detail": "authentication required"}); return None
            if perm:
                try: rt.auth.require(a, perm, "h")
                except Forbidden as e:
                    self._send(403, {"detail": str(e)}); return None
            return a

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000: return None
            try: return json.loads(self.rfile.read(n) or b"{}")
            except Exception: return None

        def do_GET(self):
            u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}; p = u.path
            if p == "/ui": return self._send(307, b"", "text/plain", dict(UI_HEADERS, Location="/ui/"))
            if p == "/ui/": return self._send(200, (UI / "index.html").read_bytes(), "text/html; charset=utf-8", UI_HEADERS)
            if p.startswith("/ui/") and p[4:] in UI_ASSETS: return self._send(200, (UI / p[4:]).read_bytes(), UI_ASSETS[p[4:]], UI_HEADERS)
            if p.startswith("/ui/"): return self._send(404, b"not found", "text/plain", UI_HEADERS)
            if p == "/auth/me":
                a = self._actor();
                if a: self._send(200, {"username": a["username"], "role": a["role"], "permissions": sorted(PERMISSIONS.get(a["role"], set()))})
                return
            if p == "/activity":
                if not self._actor("read"): return
                try:
                    flt = Filters(**{k: v for k, v in q.items() if k in ("category","severity","status","strategy","q","since","until","type","source","environment","instrument","correlation_id") and v})
                    return self._send(200, ActivityService(rt.db).query(flt, limit=int(q.get("limit", 50)), cursor=q.get("cursor")))
                except (BadRequest, ValueError) as e: return self._send(422, {"detail": str(e)})
            if p == "/activity/status":
                if self._actor("read"): self._send(200, ActivityService(rt.db).status(None))
                return
            if p == "/activity/stream":
                a = self._actor("read")
                if not a: return
                tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                try: marks = decode_cursor(self.headers.get("Last-Event-ID") or q.get("cursor")) or None
                except BadRequest as e: return self._send(422, {"detail": str(e)})
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close"); self.end_headers()
                mydrop = STATE["drop"]
                def ok():
                    if STATE["drop"] != mydrop: return False
                    try: rt.auth.authenticate(tok); return True
                    except Exception: return False
                async def go():
                    async for chunk in event_stream(ActivityService(rt.db), marks=marks, is_authorized=ok, poll_interval=0.2, heartbeat=3):
                        self.wfile.write(chunk.encode()); self.wfile.flush()
                STATE["stream_open"] += 1
                try: asyncio.run(go())
                except Exception: pass
                finally: STATE["stream_open"] -= 1
                self.close_connection = True; return
            if p == "/strategy-packages":
                if self._actor("strategies"): self._send(200, rt.packages.list())
                return
            if p.startswith("/strategy-packages/"):
                if not self._actor("strategies"): return
                try: return self._send(200, rt.packages.get(int(p.rsplit("/", 1)[1])))
                except (KeyError, ValueError): return self._send(404, {"detail": "no such package"})
            if p == "/__harness/state": return self._send(200, dict(STATE))
            self._send(404, {"detail": "not found"})

        def do_POST(self):
            u = urlparse(self.path); p = u.path; q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if p.startswith("/__harness/"):
                if p == "/__harness/seed": return self._send(200, {"outcome": seed(rt, q.get("kind", "ok"))})
                if p == "/__harness/drop": STATE["drop"] += 1; return self._send(200, {"dropped": True})
                return self._send(404, {"detail": "unknown"})
            if p == "/auth/login":
                b = self._body() or {}
                try: return self._send(200, {"token": rt.auth.login(b.get("username", ""), b.get("password", ""), "h"), "token_type": "Bearer"})
                except AuthError as e: return self._send(401, {"detail": str(e)})
            if p == "/auth/logout":
                if self._actor(): rt.auth.logout(self.headers.get("Authorization", "").removeprefix("Bearer ").strip(), "h"); self._send(204)
                return
            if p == "/strategy-packages":
                a = self._actor("strategies")
                if not a: return
                b = self._body()
                try: data = base64.b64decode((b or {}).get("archive_base64", ""), validate=True)
                except (binascii.Error, ValueError): return self._send(422, {"detail": "archive_base64 is not valid base64"})
                if os.environ.get("HARNESS_SLOW"): time.sleep(float(os.environ["HARNESS_SLOW"]))
                try: return self._send(201, rt.packages.import_package(data, a, "h"))
                except PackageRejected as e: return self._send(409 if e.code in ("VERSION_CONFLICT", "DUPLICATE_PACKAGE") else 422, {"detail": {"code": e.code, "detail": e.detail[:300]}})
                except TransitionRefused as e: return self._send(403, {"detail": str(e)})
            if p.startswith("/strategy-packages/") and p.endswith("/transition"):
                a = self._actor("strategies")
                if not a: return
                b = self._body() or {}
                try: return self._send(200, rt.packages.transition(int(p.split("/")[2]), b.get("to", ""), a, b.get("reason", ""), "h"))
                except KeyError: return self._send(404, {"detail": "no such package"})
                except TransitionRefused as e: return self._send(403 if "permission" in str(e) else 409, {"detail": str(e)})
            self._send(404, {"detail": "not found"})
    return H


def serve(db_path, port=0):
    rt = make(db_path)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler_for(rt)); srv.daemon_threads = True
    return srv, rt


if __name__ == "__main__":
    srv, _ = serve(sys.argv[1] if len(sys.argv) > 1 else ":memory:", int(sys.argv[2]) if len(sys.argv) > 2 else 8765)
    print("STAND-IN harness (not FastAPI) on http://127.0.0.1:%d/ui  users: admin / viewer, password correct-horse-battery" % srv.server_address[1]); srv.serve_forever()
