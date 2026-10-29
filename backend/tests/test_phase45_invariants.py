"""Phase 4.5 integration safety invariants, as executable checks over the code and the runtime (no network, no exchange)."""
import ast
import pathlib
import re
import unittest

from hardening_helpers import *

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOT = BACKEND_ROOT.parent
PKG = BACKEND_ROOT / "dewako"


def _need_repo():
    """The checks that read frontend/, scripts/ or .github/ need the whole repository. The backend-only Docker image does not have them:
    that is reported as a SKIP (never a pass), and CI's repository jobs require these tests to have run."""
    if not (ROOT / "frontend").is_dir() or not (ROOT / ".github").is_dir():
        raise unittest.SkipTest("full repository layout not present (backend-only image)")


def _py(root):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(path):
    t = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_activity_package_is_read_only_and_cannot_reach_anything_that_acts():
    forbidden = ("dewako.execution", "dewako.exchanges", "dewako.safety", "dewako.risk", "dewako.strategy_packages", "dewako.ai", "dewako.runtime")
    for p in _py(PKG / "activity"):
        bad = [m for m in _imports(p) if m.startswith(forbidden)]
        assert not bad, (p.name, bad)
        src = p.read_text()
        assert not re.search(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", src, re.I), f"{p.name} contains a write statement"
        assert not re.search(r"\.(create_order|cancel_order|submit)\(", src), p.name


def test_no_activity_or_package_route_can_submit_cancel_or_retry_an_order():
    src = (PKG / "api" / "main.py").read_text()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routes = [d for d in n.decorator_list if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post", "put", "delete")]
            path = ast.unparse(routes[0].args[0]) if routes and routes[0].args else ""
            if path.strip("'\"").startswith(("/activity", "/ui", "/strategy-packages", "/ai/", "/auth/me")):
                body = ast.unparse(n)
                assert not re.search(r"engine\.|\.execute\(.*decision|create_order|cancel_order|\.submit\(|enable_live|activate_live|release_kill", body), (path, n.name)


def test_only_the_exchange_verification_path_writes_verified_at_and_no_production_code_imports_test_fixtures():
    allowed = {"persistent_manager.py"}
    for p in _py(PKG):
        src = p.read_text()
        if re.search(r"verified_at\s*=", src) or re.search(r"SET[^\"']*verified_at", src):
            assert p.name in allowed, f"{p.name} writes exchanges.verified_at"
        assert "hardening_helpers" not in src and "from tests" not in src and "import tests" not in src, p.name
        for forbidden in ("mark_exchange_verified", "force_verified", "skip_verification", "DEWAKO_ALLOW_LIVE", "LIVE_OVERRIDE"):
            assert forbidden not in src, (p.name, forbidden)


def test_there_is_no_live_enabled_state_and_no_environment_variable_that_turns_live_on():
    from dewako.safety.controller import SafetyController
    db, _ = tmp_db()
    states = SafetyController(db).readiness_state()["state"]
    assert states != "LIVE_ENABLED"
    for p in _py(PKG):
        src = p.read_text()
        for m in re.finditer(r"os\.environ(?:\.get)?\(\s*[\"'](DEWAKO_[A-Z_]*LIVE[A-Z_]*)[\"']", src):
            raise AssertionError(f"{p.name} reads an environment variable that mentions live: {m.group(1)}")
        for m in re.finditer(r"environ\.get\(\s*[\"'](DEWAKO_[A-Z_]*LIVE[A-Z_]*)[\"']", src):
            raise AssertionError(f"{p.name}: {m.group(1)}")


def test_test_tooling_in_this_repo_uses_only_scripted_fake_exchanges():
    _need_repo()
    """The stand-in server, the browser tests and the HTTP tests seed events through FakeLive. None imports a real adapter or an HTTP exchange client."""
    real = ("ccxt", "binance", "bybit", "okx", "kraken", "coinbase")
    for rel in ("scripts/standin_http_harness.py", "frontend/browser_tests/test_dashboard_browser.py", "backend/tests/test_api_phase44.py"):
        p = ROOT / rel
        mods = {m.lower() for m in _imports(p)}
        assert not [m for m in mods if any(r in m for r in real)], (rel, mods)
        assert "FakeLive" in p.read_text() or rel.endswith("test_dashboard_browser.py")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert not re.search(r"(API_KEY|API_SECRET|BINANCE|BYBIT|OKX)", ci), "CI must not carry exchange credentials"


def test_ui_page_never_offers_an_order_action_on_the_live_pages():
    _need_repo()
    js = (ROOT / "frontend" / "prototype" / "app.js").read_text()
    # everything between the Live Activity and Strategy Manager sections uses the real backend; it must not call an order-changing route
    start = js.index("/* ---------- real backend: Live Activity")
    end = js.index("/* ---------- live simulation ---------- */")
    real_ui = js[start:end]
    for route in re.findall(r"bk\(\s*['\"`]([^'\"`?]+)", real_ui):
        assert not re.search(r"order|execute|trade|kill|live|exchange", route, re.I), route
    assert not re.search(r"(retry|resend|cancel)\s*(order|trade)", real_ui, re.I)


def test_ui_package_labels_match_backend_states():
    _need_repo()
    from dewako.strategy_packages.manager import STATES
    js = (ROOT / "frontend" / "prototype" / "packages.js").read_text()
    m = re.search(r"LABELS\s*=\s*\{(.*?)\};", js, re.S)
    keys = set(re.findall(r"([A-Z_]+)\s*:", m.group(1)))
    assert keys == set(STATES), (keys ^ set(STATES))
