# Phase 4.5 Report — integration, verification and readiness audit

Historical implementation report. Its initial verification blockers were re-tested in `PHASE4_5_INDEPENDENT_AUDIT.md`; use that audit for the current results.

**Live trading: LOCKED. Phase 3: INCOMPLETE. No exchange is verified. Phase 4.5 is NOT complete by its own definition of done:** the real FastAPI app has not run (the package index is unreachable from the build sandbox), so real HTTP and real-app dashboard verification are **NOT VERIFIED / BLOCKED**.

Baseline: `PHASE4_5_BASELINE.md` (input ZIP SHA-256 `4b0f32fa…0bfc`; not a Git repo, so no commit id). Earlier reports and audits are unmodified.

## 1. Implemented
- **Shared dashboard policy** `api/http_policy.py`: `UI_CSP`, `UI_ASSETS`, `UI_HEADERS`; `/ui` redirects to `/ui/`; allowlist now `activity.js, packages.js, app.js, app.css`; headers also on 404/redirect.
- **CSP without `unsafe-inline` for scripts**: inline `<script>`/`<style>` moved to `app.js`/`app.css`; Google Fonts link removed; `<meta charset>` added. Exception kept and documented: `style-src-attr 'unsafe-inline'`.
- **Real bug found:** the page referenced `app.js`/`activity.js` relatively, so served at `/ui` they would resolve to `/activity.js` → 404 on the real app. The stand-in had been masking it by serving root paths. Fixed by the redirect to `/ui/` and by removing the stand-in's root-path serving.
- **DEMO / SIMULATED banner** on every page except Live Activity and Strategy Manager; sidebar "Online" replaced with "Demo UI (most pages simulated)".
- **New HTTP tests (real app)** `backend/tests/test_api_phase44.py`, 8 tests: `/auth/me`, `/ui` + assets + traversal, `/activity` schema/pagination/filters/bad input, `/activity/status`, UNKNOWN order stays unknown across reads, package upload/authorization/duplicate/malicious/oversize/no-activation, and (real Uvicorn + httpx) SSE hello/events/heartbeat/resume without duplicates, revoked-session close, stream cap (429), no exception text in frames.
- `stream.event_stream` bounds are resolved per call (so the HTTP tests can shorten them); `/ui` added to the public-route allowlist of the existing "every route needs auth" test (static allowlisted files only).
- **Invariant tests** `test_phase45_invariants.py` (7): activity package is read-only and cannot import anything that acts; no activity/package/AI route can submit/cancel/retry orders or enable live; only the exchange-verification path writes `verified_at`, production code imports no test fixtures; no "live" environment variable; tooling uses only fake exchanges and CI carries no exchange credentials; UI real pages call no order/live routes; UI package labels equal backend states. (`LIVE_ENABLED` exists only as a pre-existing *audit action name*; an existing test asserts it is not a readiness state.)
- **Bounded tests:** the standalone runner enforces a per-test timeout (`DEWAKO_TEST_TIMEOUT`, default 180 s; reports test name; uses a BaseException so library code cannot swallow it). Pytest jobs in CI use `--timeout=300`.
- **Reproducible deps:** `backend/requirements-test.txt`, `backend/constraints-test.txt` (**pins unresolved**, written from memory; replace with `pip freeze` after the first real install).
- **CI:** new `e2e-real-app` job (Uvicorn + Playwright, `DEWAKO_E2E_SERVER=real`), timeouts, and the new tests added to the `--require` lists. **Never executed.**
- **Browser test suite** now has two server modes (`standin` default, `real`) and added tests: CSP enforcement and headers, hostile filename inertness, demo-page labels. `DEWAKO_LOGIN_RATE_LIMIT` knob (default unchanged = 10/min) lets the e2e run log in repeatedly.

## 2. Verified by automated tests (observed)
| Check | Result | Log |
|---|---|---|
| SQLite full suite (custom runner, Python 3.13.16) | 351 passed, 0 failed, 33 skipped, 59 s, rc 0 | `docs/evidence/phase4_5_sqlite.log` |
| PostgreSQL 16.15 full suite | 354 passed, 0 failed, 30 skipped, 102 s, rc 0 | `phase4_5_pg16.log` |
| Frontend Node tests | 9 passed | `phase4_5_node.log` |
| Browser tests vs **stand-in** (not FastAPI) | 13 passed, 0 failed | `phase4_5_browser_standin.log` |
| Secret scan, compileall | 0 findings / OK | — |
Skips: SQLite 33 = 30 HTTP tests (22 existing + 8 new; FastAPI/httpx/uvicorn absent) + 3 PostgreSQL-only crash cases. PG 30 = the 30 HTTP tests. Skipped is not passed.

The first PG run in this phase failed wholesale because the local PostgreSQL server was not running (connection refused, infrastructure); after restarting it the suite was rerun in full. One SQLite failure (`test_activity…csp_is_scoped`) was a 4.4 test pinned to the old CSP; it was updated to a **stricter** assertion (script-src exactly `'self'`, style-src-attr the only exception).

## 3. Verified manually
Screenshots from Phase 4.4 only (`frontend/browser_tests/shots/`); no new manual verification.

## 4. Partially verified
- CSP/header behavior: enforced in Chromium against the stand-in using the *same constants*; the actual FastAPI response headers are NOT VERIFIED.
- Unknown-order handling: verified at service level on SQLite and PG16; the HTTP-level test exists but has not run. The product has no HTTP route that could resolve it; reconciliation is the only resolver.
- Live Activity / Strategy Manager workflows: verified end to end in a real browser against the stand-in only.

## 5. Not verified / 6. Blocked
- **BLOCKED:** installing FastAPI/httpx/uvicorn/pytest/playwright-pins (`pip` → DNS failure direct; `403 Forbidden` via the sandbox proxy). Not bypassed. Evidence: `phase4_5_browser_real_BLOCKED.log` (real mode stops at `No module named 'fastapi'`).
- NOT RUN: real-app HTTP tests (30), real-app dashboard e2e, GitHub Actions, Docker build/compose, Windows run, real pytest (custom runner + shim used), other browsers, screen readers.
- NOT VERIFIED: upload rate limits over HTTP, session expiry in the browser, secrets-in-browser-storage check on the real app (the UI stores nothing in localStorage/sessionStorage by design; not asserted by a test).

## 7. Known limitations and residual risks
`style-src-attr 'unsafe-inline'` exception (scope above); third-party fonts removed; the 8 new HTTP tests and the e2e real mode were written without ever being executed and will probably need debugging on first run; unresolved dependency pins; adapters remain unverified on real exchanges; all earlier Phase 3.4 blockers stand (`docs/LIVE_TRADING_READINESS.md`).

## Remaining blockers before any testnet or live consideration
1. Run CI for real (HTTP tests, e2e-real-app, PG16 under pytest, Docker, Windows) and fix what it finds; freeze dependencies.
2. Real exchange **testnet** lifecycle verification (submit, lookup by client id, fills, cancel, reconciliation) per adapter.
3. Protective-order handling, account identity, balance/position reconciliation against a real exchange, candle-level market-data freshness.
4. Operator authorization drill, MFA, off-host audit anchor, alerting, backup/restore drill.
5. Replace simulated dashboard pages with backend data or keep them labelled.

## Rollback
Restore `frontend/prototype/index.html` from the Phase 4.4 ZIP and delete `app.js`/`app.css`; revert `api/http_policy.py` additions and the `/ui` routes in `api/main.py`; delete `test_api_phase44.py`, `test_phase45_invariants.py`, `requirements-test.txt`, `constraints-test.txt`; revert `ci.yml`, `run_tests_standalone.py`, `stream.py` default handling, the `DEWAKO_LOGIN_RATE_LIMIT` line in `runtime_state.py`. No migrations were added.

## Changed files
backend: `dewako/api/{http_policy,main,runtime_state}.py`, `dewako/activity/stream.py`, `tests/{test_api_phase44,test_phase45_invariants,test_activity,test_api_testclient}.py`, `requirements-test.txt`, `constraints-test.txt`; frontend: `prototype/{index.html,app.js,app.css}`, `browser_tests/test_dashboard_browser.py`; `scripts/{standin_http_harness,run_tests_standalone}.py`; `.github/workflows/ci.yml`; docs: `LIVE_ACTIVITY.md`, `TESTING.md`, `LIVE_TRADING_READINESS.md` (addendum), `evidence/phase4_5_*`; `PHASE4_5_BASELINE.md`, `PHASE4_5_REPORT.md`, `PHASE4_COMPLETION.md`.
