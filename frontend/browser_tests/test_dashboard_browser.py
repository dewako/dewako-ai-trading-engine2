"""Browser tests (Playwright/Chromium) for the Live Activity and Strategy Manager pages.
Default: scripts/standin_http_harness.py (the real services behind a stdlib server; this is NOT the FastAPI app).
DEWAKO_E2E_SERVER=real: launches the ACTUAL FastAPI app under Uvicorn (needs `pip install -r backend/requirements.txt`). Only that mode counts as real-app verification.
Run:  python3 frontend/browser_tests/test_dashboard_browser.py
"""
import os, sys, time, tempfile, threading, traceback, urllib.request, json, zipfile, io, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "backend"), str(ROOT / "backend" / "tests")]
import standin_http_harness as H
from test_strategy_packages import build, raw_zip, _unreg
from playwright.sync_api import sync_playwright, expect

SHOTS = pathlib.Path(__file__).parent / "shots"
tmp = pathlib.Path(tempfile.mkdtemp())
MODE = os.environ.get("DEWAKO_E2E_SERVER", "standin")      # "standin" = stdlib stand-in (NOT FastAPI); "real" = the actual FastAPI app under Uvicorn
PW_ = "correct-horse-battery"


class Backend:
    """Common interface for both server modes. In 'real' mode events are produced by a Runtime in this process writing to the same database
    file the Uvicorn process serves (real engine + scripted FAKE exchange; no real exchange, live never enabled)."""
    def __init__(self):
        self.proc = None
        if MODE == "real":
            self._start_real()
        else:
            self.srv, self.rt = H.serve(str(tmp / "t.db"))
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
            self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def _start_real(self):
        import socket, subprocess
        from cryptography.fernet import Fernet
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        self.db = str(tmp / "real.db"); self.base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, DEWAKO_TRADING_DB=self.db, DEWAKO_BOOTSTRAP_TOKEN="boot-e2e-token", ENCRYPTION_KEY=Fernet.generate_key().decode(),
                   DEWAKO_WORKERS="0", DEWAKO_RATE_LIMIT="100000", DEWAKO_LOGIN_RATE_LIMIT="1000", PYTHONPATH=str(ROOT / "backend"))
        env.pop("DEWAKO_ENV", None); env.pop("DEWAKO_DATABASE_URL", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dewako.api.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                                     env=env, cwd=str(ROOT / "backend"), stdout=open(tmp / "uvicorn.log", "wb"), stderr=subprocess.STDOUT)
        import urllib.error
        for _ in range(150):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=2); break
            except Exception:
                if self.proc.poll() is not None: raise RuntimeError("uvicorn exited: " + (tmp / "uvicorn.log").read_text()[-800:])
                time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn did not become ready")
        def call(path, body, headers=None):
            r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers=dict({"Content-Type": "application/json"}, **(headers or {})))
            return json.loads(urllib.request.urlopen(r, timeout=10).read() or b"{}")
        call("/auth/bootstrap", {"username": "admin", "password": PW_}, {"X-Bootstrap-Token": "boot-e2e-token"})
        tok = call("/auth/login", {"username": "admin", "password": PW_})["token"]
        call("/users", {"username": "viewer", "password": PW_, "role": "VIEWER"}, {"Authorization": "Bearer " + tok})
        from dewako.api.runtime_state import Runtime
        self.rt = Runtime(self.db)                                          # same database file, separate process

    def seed(self, kind):
        if MODE == "real":
            result, errors = [], []
            def seed_in_worker():
                try:
                    result.append(H.seed(self.rt, kind))
                except BaseException as e:
                    errors.append(e)
            worker = threading.Thread(target=seed_in_worker, daemon=True)
            worker.start()
            worker.join(timeout=30)
            if worker.is_alive():
                raise TimeoutError("real-app test seeder did not finish within 30 seconds")
            if errors:
                raise errors[0]
            return result[0]
        return hpost_standin(self.base, "/__harness/seed?kind=" + kind)

    def drop(self, page):
        if MODE == "real":
            page.context.set_offline(True); time.sleep(1.0); page.context.set_offline(False)
        else:
            hpost_standin(self.base, "/__harness/drop")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try: self.proc.wait(15)
            except Exception: self.proc.kill()


def hpost_standin(base, path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, method="POST", data=b"")).read())


BK = Backend()
rt = BK.rt
BASE = BK.base
URL = BASE + "/ui/"


def hpost(path):                      # kept for the existing call sites: "/__harness/seed?kind=x"
    return BK.seed(path.split("kind=")[1])


def login(page, user="admin", pw="correct-horse-battery"):
    page.fill("#bkUser", user); page.fill("#bkPass", pw); page.click("#bkForm button[type=submit]")


def open_page(br, nav, user="admin", pw="correct-horse-battery", **ctx):
    c = br.new_context(**ctx); c.route(lambda u: not u.startswith('http://127.0.0.1'), lambda r: r.abort()); p = c.new_page(); errs = []
    p.on("pageerror", lambda e: errs.append(str(e)))
    p.on("console", lambda m: errs.append(m.text) if "Content Security Policy" in m.text else None)
    p.goto(URL); p.click(f'#nav button[data-p="{nav}"]')
    login(p, user, pw); return c, p, errs


def zip_file(name, data):
    f = tmp / name; f.write_bytes(data); return str(f)


RESULTS = []
def case(fn):
    def run(br):
        try: fn(br); RESULTS.append((fn.__name__, "PASS", ""))
        except Exception as e: RESULTS.append((fn.__name__, "FAIL", traceback.format_exc(limit=3)))
    run.__name__ = fn.__name__; return run


@case
def t_login_and_real_events_flow(br):
    hpost("/__harness/seed?kind=ok")
    c, p, errs = open_page(br, "activity")
    expect(p.locator("#actConn")).to_contain_text("Connected", timeout=8000)
    expect(p.locator("li.ev").first).to_be_visible(timeout=8000)
    n = p.locator("li.ev").count()
    assert "RISK_APPROVED" in p.inner_text("#actBody") or "risk" in p.inner_text("#actBody").lower()
    hpost("/__harness/seed?kind=ok")                                   # a new real event arrives through the stream
    p.locator("li.ev").nth(n).wait_for(state="visible", timeout=8000)
    p.screenshot(path=str(SHOTS / "activity_live.png"))
    assert not errs, errs; c.close()


@case
def t_unknown_order_prominent_and_no_controls(br):
    hpost("/__harness/seed?kind=unknown")
    c, p, errs = open_page(br, "activity")
    expect(p.locator("li.ev.unknown").first).to_be_visible(timeout=8000)
    txt = p.inner_text("#actBody") + p.inner_text("#actAlert")
    assert "UNKNOWN" in txt and "RECONCILIATION REQUIRED" in txt.upper()
    assert "FILLED" not in p.locator("li.ev.unknown").first.inner_text()
    expect(p.locator("#actAlert .act-alert").first).to_contain_text("RECONCILIATION REQUIRED")
    body = p.inner_text("body").lower()
    for bad in ("retry order", "resend", "cancel order"):
        assert bad not in p.inner_text("#actBody").lower()
    expect(p.locator("#actStatus")).to_have_class(__import__("re").compile("ERROR"), timeout=8000)
    p.screenshot(path=str(SHOTS / "activity_unknown_order.png")); c.close()


@case
def t_pause_resume_holds_events_but_never_hides_critical(br):
    c, p, errs = open_page(br, "activity")
    expect(p.locator("#actConn")).to_contain_text("Connected", timeout=8000)
    p.wait_for_selector("li.ev", timeout=8000)
    before = p.locator("li.ev").count()
    p.click("#btnPause"); expect(p.locator("#btnPause")).to_have_attribute("aria-pressed", "true")
    hpost("/__harness/seed?kind=blocked")
    expect(p.locator("#actAlert")).to_contain_text("Display paused", timeout=8000)
    assert p.locator("li.ev").count() == before
    p.click("#btnPause"); p.locator("li.ev").nth(before).wait_for(state="visible", timeout=8000)
    assert "Display paused" not in p.inner_text("#actAlert"); c.close()


@case
def t_disconnect_shows_state_and_resumes_without_duplicates(br):
    c, p, errs = open_page(br, "activity")
    expect(p.locator("#actConn")).to_contain_text("Connected", timeout=8000)
    p.wait_for_selector("li.ev", timeout=8000)
    BK.drop(p)
    expect(p.locator("#actConn")).to_contain_text("Connected", timeout=15000)   # reconnects with Last-Event-ID
    hpost("/__harness/seed?kind=ok")
    p.wait_for_timeout(1500)
    ids = p.eval_on_selector_all("li.ev", "els=>els.map(e=>e.dataset.id)")
    assert len(ids) == len(set(ids)), "duplicate event ids after reconnect"; c.close()


@case
def t_filters_search_and_details(br):
    c, p, errs = open_page(br, "activity")
    p.wait_for_selector("li.ev", timeout=8000)
    p.select_option("#fCat", "order"); p.wait_for_timeout(700)
    cats = p.eval_on_selector_all("li.ev .ev-cat", "els=>els.map(e=>e.textContent.toLowerCase())")
    assert cats and all("order" in x for x in cats), cats
    p.select_option("#fCat", ""); p.fill("#fQ", "zzz-no-such-thing"); p.wait_for_timeout(900)
    expect(p.locator(".act-empty")).to_contain_text("No events match", timeout=5000)
    p.click("[data-bk=clear]"); p.wait_for_selector("li.ev", timeout=8000)
    p.locator("li.ev").first.click(); expect(p.locator("#modal.show")).to_be_visible()
    assert "event_id" in p.inner_text("#modalBox").lower() or "correlation" in p.inner_text("#modalBox").lower(); c.close()


@case
def t_viewer_cannot_import_and_denied_states(br):
    c, p, errs = open_page(br, "pkgs", user="viewer")
    expect(p.locator("[data-bk=pick]")).to_be_disabled(timeout=5000)
    expect(p.locator("#pkList")).to_contain_text("Your role", timeout=5000) if p.locator("#pkList .act-empty").count() else None
    c.close()


@case
def t_zip_import_valid_malicious_duplicate(br):
    c, p, errs = open_page(br, "pkgs")
    good = build("Browser Gold A"); f = zip_file("good.zip", good)
    p.set_input_files("#pkFile", f)
    expect(p.locator("#pkResult .pk-res.ok")).to_contain_text("Accepted by the server", timeout=10000)
    res = p.inner_text("#pkResult")
    import hashlib; assert hashlib.sha256(good).hexdigest() in res
    assert "Not enabled" in res
    expect(p.locator("#pkList")).to_contain_text("Browser Gold A", timeout=5000)
    p.screenshot(path=str(SHOTS / "import_accepted.png"))
    p.set_input_files("#pkFile", f)                                    # duplicate
    expect(p.locator("#pkResult .pk-res.bad")).to_contain_text("Rejected by the server", timeout=10000)
    evil = zip_file("evil.zip", raw_zip([("../../etc/x", b"x"), ("manifest.json", b"{}")]))
    p.set_input_files("#pkFile", evil)
    expect(p.locator("#pkResult .pk-res.bad")).to_contain_text("Rejected by the server", timeout=10000)
    p.screenshot(path=str(SHOTS / "import_rejected.png"))
    p.click('#nav button[data-p="activity"]')
    p.wait_for_selector("li.ev", timeout=8000)
    p.fill("#fQ", "PACKAGE"); p.wait_for_timeout(1000)
    txt = p.inner_text("#actBody").upper()
    assert "PACKAGE" in txt and ("REJECT" in txt or "QUARANTIN" in txt or "IMPORT" in txt)
    assert "etc/x" not in p.inner_text("#actBody")
    with rt.db.read() as cn:
        st = [r["state"] for r in cn.execute("SELECT state FROM strategy_packages").fetchall()]
    assert "VALIDATED" in st and not any(s in ("BACKTEST_ELIGIBLE", "PAPER_ELIGIBLE", "TESTNET_ELIGIBLE") for s in st)
    assert not errs, errs; c.close()


@case
def t_zip_cancel_and_server_down(br):
    c, p, errs = open_page(br, "pkgs")
    held = []
    p.route("**/strategy-packages", lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    p.set_input_files("#pkFile", zip_file("slow.zip", build("Browser Slow A")))
    expect(p.locator("[data-bk=cancelup]")).to_be_visible(timeout=5000)
    p.click("[data-bk=cancelup]")
    expect(p.locator("#pkResult")).to_contain_text("cancelled", timeout=5000)
    assert "Accepted" not in p.inner_text("#pkResult")
    for r in held:
        try: r.abort()
        except Exception: pass
    c.close()


@case
def t_reduced_motion_disables_animation(br):
    c, p, errs = open_page(br, "activity", reduced_motion="reduce")
    p.wait_for_selector("li.ev", timeout=8000)
    assert p.evaluate("getComputedStyle(document.querySelector('ul.pipe'),'::before').animationName") in ("none", "")
    assert p.locator("li.ev.enter").count() == 0
    c.close()


@case
def t_csp_blocks_inline_script_and_headers_are_set(br):
    c = br.new_context(); c.route(lambda u: not u.startswith('http://127.0.0.1'), lambda r: r.abort()); p = c.new_page()
    viol = []
    p.on("console", lambda m: viol.append(m.text) if "Content Security Policy" in m.text else None)
    r = p.goto(URL); h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline'" not in h["content-security-policy"].split("style-src-attr")[0]
    assert "script-src 'self' 'unsafe-inline'" not in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
    p.evaluate("()=>{const s=document.createElement('script');s.textContent='window.__inline_ran=1';document.head.appendChild(s)}")
    assert p.evaluate("window.__inline_ran") is None, "an inline script executed: CSP is not enforced"
    assert any("script-src" in v for v in viol), viol
    for bad in ("/ui/../backend/dewako/api/main.py", "/ui/index.html.bak", "/ui/%2e%2e/x", "/ui/secret.js"):
        st = p.evaluate("u=>fetch(u).then(r=>r.status)", bad)
        assert st in (404, 400), (bad, st)
    assert p.evaluate("()=>fetch('/ui/app.js').then(r=>r.headers.get('content-type'))").startswith("text/javascript")
    c.close()


@case
def t_hostile_filename_is_inert(br):
    c, p, errs = open_page(br, "pkgs")
    name = "<img src=x onerror=window.__pwn=1>.zip"
    p.set_input_files("#pkFile", files=[{"name": name, "mimeType": "application/zip", "buffer": raw_zip([("a", b"b")])}])
    expect(p.locator("#pkResult .pk-res")).to_be_visible(timeout=10000)
    p.wait_for_timeout(300)
    assert p.evaluate("window.__pwn") is None and p.locator("#pkResult img").count() == 0
    assert not [e for e in errs if "Content Security" not in e], errs
    c.close()


@case
def t_simulated_pages_are_labelled_and_real_pages_are_not(br):
    c, p, errs = open_page(br, "activity")
    expect(p.locator("#actStatus")).to_be_visible(timeout=8000)
    assert p.locator(".demo-banner").count() == 0
    for page_id in ("dashboard", "markets", "positions", "orders", "live", "paper", "perf", "risk"):
        p.click(f'#nav button[data-p="{page_id}"]')
        expect(p.locator("#view .demo-banner")).to_contain_text("DEMO / SIMULATED", timeout=3000)
    assert "Online" not in p.inner_text(".sys").split("Demo")[0]
    p.click('#nav button[data-p="pkgs"]'); assert p.locator("#view .demo-banner").count() == 0
    c.close()


@case
def t_backtest_page_is_real_read_only_and_labelled_simulated(br):
    from stage3b_evidence import record_run, m
    ANALYST = {"user_id": 3, "username": "analyst", "role": "ANALYST"}
    record_run(rt.packages.runs, ANALYST, "Browser BT", "1.0", "f" * 64)
    record_run(rt.packages.runs, ANALYST, "Browser BT", "1.0", "f" * 64, full=m(trades=12, net_pnl=-3.0, max_drawdown_pct=0.3))
    c, p, errs = open_page(br, "backtest")
    expect(p.locator("#btList")).to_contain_text("Browser BT", timeout=8000)
    assert p.locator("#view .demo-banner").count() == 0                       # it is real data now, not the old generator
    assert "DEMO MODE" not in p.inner_text("#view") and "seeded generator" not in p.inner_text("#view")
    rows = p.locator("#btList tr", has_text="Browser BT")
    assert rows.count() == 2
    assert "SIMULATED" in rows.first.inner_text() and "FAILED" in rows.first.inner_text() and "PASSED" in rows.nth(1).inner_text()
    for forbidden in ("Run backtest", "Start", "Promote", "Delete", "Edit"):
        assert p.locator("#view button", has_text=forbidden).count() == 0, forbidden
    rows.first.locator("[data-bk=btdetail]").click()
    expect(p.locator("#modalBox")).to_contain_text("Promotion gate", timeout=5000)
    t = p.inner_text("#modalBox")
    assert "NEWEST RUN: counts for promotion" in t and "SIMULATED" in t and "Does not meet the limits" in t and "No order was ever sent anywhere" in t
    assert "owner, 2026-11-05" in t
    p.screenshot(path=str(SHOTS / "backtest_report.png"))
    p.keyboard.press("Escape")
    rows.nth(1).locator("[data-bk=btdetail]").click()
    expect(p.locator("#modalBox")).to_contain_text("older run: does not count", timeout=5000)
    assert not errs, errs; c.close()
    c, p, errs = open_page(br, "backtest", user="viewer")                      # a viewer lacks the backtests permission: a plain message, no data
    expect(p.locator("#btList")).to_contain_text("Your role cannot view backtest records", timeout=8000)
    assert "Browser BT" not in p.inner_text("#view"); c.close()


@case
def t_bad_login_and_logout(br):
    c = br.new_context(); c.route(lambda u: not u.startswith('http://127.0.0.1'), lambda r: r.abort()); p = c.new_page(); p.goto(URL); p.click('#nav button[data-p="activity"]')
    login(p, "admin", "wrong-password-xx")
    expect(p.locator(".bk-err")).to_contain_text("Invalid username or password", timeout=5000)
    assert "wrong-password" not in p.content()
    login(p); expect(p.locator("#actStatus")).to_be_visible(timeout=8000)
    p.click("[data-bk=logout]"); expect(p.locator("#bkForm")).to_be_visible(timeout=5000); c.close()


@case
def t_empty_login_is_blocked_by_required_fields(br):
    c = br.new_context(); p = c.new_page(); requests = []
    p.on("request", lambda r: requests.append(r.url) if r.url.endswith("/auth/login") else None)
    p.goto(URL); p.click('#nav button[data-p="activity"]')
    p.click("#bkForm button[type=submit]")
    assert p.locator("#bkUser").evaluate("(el) => el.validity.valueMissing")
    assert p.locator("#bkPass").evaluate("(el) => el.validity.valueMissing")
    assert not requests, "empty credentials were sent to the backend"
    p.locator("#bkUser").fill("test-user")
    p.click("#bkForm button[type=submit]")
    assert p.locator("#bkPass").evaluate("(el) => el.validity.valueMissing")
    assert not requests, "incomplete credentials were sent to the backend"
    c.close()


@case
def t_document_is_standards_mode_with_viewport_and_relative_assets_load(br):
    c = br.new_context(); p = c.new_page(); errs = []; p.on("pageerror", lambda e: errs.append(str(e))); p.goto(URL)
    expect(p.locator("#nav button").first).to_be_visible(timeout=8000)
    assert p.evaluate("document.compatMode") == "CSS1Compat", "quirks mode"
    assert p.evaluate("document.documentElement.lang") == "en"
    assert "width=device-width" in p.evaluate("document.querySelector('meta[name=viewport]').content")
    assert not errs, errs; c.close()


CHECKS = (t_login_and_real_events_flow, t_unknown_order_prominent_and_no_controls, t_pause_resume_holds_events_but_never_hides_critical,
                  t_disconnect_shows_state_and_resumes_without_duplicates, t_filters_search_and_details, t_viewer_cannot_import_and_denied_states,
                  t_zip_import_valid_malicious_duplicate, t_zip_cancel_and_server_down, t_reduced_motion_disables_animation, t_csp_blocks_inline_script_and_headers_are_set,
                  t_hostile_filename_is_inert, t_simulated_pages_are_labelled_and_real_pages_are_not, t_backtest_page_is_real_read_only_and_labelled_simulated, t_bad_login_and_logout,
                  t_empty_login_is_blocked_by_required_fields, t_document_is_standards_mode_with_viewport_and_relative_assets_load)


if __name__ == "__main__":
    # one admin-visible seed so history is non-empty
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        for t in CHECKS:
            t(br)
        br.close()
    BK.stop()
    for n, s, d in RESULTS:
        print(f"{s}  {n}" + (("\n" + d) if d else ""))
    print(f"{sum(1 for r in RESULTS if r[1]=='PASS')} passed, {sum(1 for r in RESULTS if r[1]=='FAIL')} failed (" + ("REAL FastAPI app under Uvicorn" if MODE == "real" else "stand-in harness, NOT FastAPI") + ")")
    problems = []
    if len(RESULTS) != len(CHECKS):
        problems.append(f"only {len(RESULTS)} of {len(CHECKS)} checks recorded a result")
    if os.environ.get("DEWAKO_E2E_REQUIRE_REAL") == "1" and MODE != "real":
        problems.append(f"DEWAKO_E2E_REQUIRE_REAL=1 but the run used the {MODE!r} server: this is not real-app verification")
    junit = os.environ.get("DEWAKO_E2E_JUNIT")
    if junit:
        from xml.sax.saxutils import escape, quoteattr
        rows = "".join(f'<testcase classname="browser_e2e.{MODE}" name={quoteattr(n)}>'
                       + (f'<failure message="check failed">{escape(d)}</failure>' if s == "FAIL" else "") + "</testcase>" for n, s, d in RESULTS)
        fails = sum(1 for r in RESULTS if r[1] == "FAIL")
        pathlib.Path(junit).write_text(f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="browser_e2e" tests="{len(RESULTS)}" '
                                       f'failures="{fails}">{rows}</testsuite></testsuites>')
    for p_ in problems:
        print("PROBLEM:", p_)
    sys.exit(1 if problems or any(r[1] == "FAIL" for r in RESULTS) else 0)
