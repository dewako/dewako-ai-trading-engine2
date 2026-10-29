# Phase 4.4 Report — Live Activity pipeline + Manual Algo ZIP import

**Live trading: LOCKED. Phase 3: INCOMPLETE. No exchange is live-verified.** Nothing here can place, retry or cancel an order.

## Implemented
- `backend/dewako/activity/` (events, service, stream): read-only unified view over `audit_log`, `order_events`, `reconciliation_events`; keyset pagination; SSE with per-source watermarks, resume via `Last-Event-ID`/`?cursor=`, heartbeat, auth recheck, bounded streams.
- Real instrumentation: `RISK_APPROVED` (execution engine, inside the transaction), `ENGINE_STARTUP`/`ENGINE_SHUTDOWN`.
- Routes: `GET /activity`, `/activity/status`, `/activity/stream`, `/auth/me`, `/ui`, `/ui/{activity.js|packages.js}` (allowlisted).
- Dashboard (`frontend/prototype/index.html`, `activity.js`, `packages.js`): sign-in panel (token in memory), **Live Activity** page (animated timeline, status, connection state, filters, search, load-older, pause/resume of display only, UTC/local, details, empty/loading/stale/denied/error states, reduced motion), **Strategy Manager** page with an **Import Algo ZIP** button/drop zone (XHR progress, cancel, server-confirmed result, package list/details, promote/disable showing server refusals, "view activity"), and an "Import Algo ZIP" button on the demo Strategies page.
- Package import is atomic (row + audit + state in one transaction).
- Docs: `docs/LIVE_ACTIVITY.md`, `docs/STRATEGY_PACKAGES.md` (dashboard workflow). Stand-in server: `scripts/standin_http_harness.py`.

## Tested (observed, this sandbox)
| Suite | Result | Evidence |
|---|---|---|
| SQLite full (custom runner) | **344 passed, 0 failed, 25 skipped** (22 HTTP tests need FastAPI/httpx; 3 crash-matrix cases are PostgreSQL-only), 57 s | `docs/evidence/phase4_4_sqlite.log` |
| PostgreSQL 16 full | **347 passed, 0 failed, 22 skipped** (the HTTP tests), 95 s | `docs/evidence/phase4_4_pg16.log` |
| `test_activity` | 24 tests (incl. atomic rollback on audit failure, persistence after restart, import does not activate/order, rejection event without file contents, viewer refused) | in both logs |
| Node frontend model tests | 9 passed | `phase4_4_node.log` |
| Browser (Chromium/Playwright) vs **stand-in server, NOT FastAPI** | 10 passed: login/bad login/logout, real events flow, UNKNOWN order shown prominently with no order controls, pause/resume with held-event banner, forced disconnect → reconnect with no duplicate ids, filters/search/details, viewer cannot import, valid/duplicate/malicious ZIP via file chooser with package events visible in the feed and DB state VALIDATED only, upload cancel, reduced motion | `phase4_4_browser_standin.log`, `frontend/browser_tests/shots/*.png` |

The browser tests caught one real UI bug (login read the form after re-rendering it, sending empty credentials) and an empty-state wording bug; both fixed.

## The "full-suite / crash-matrix timeout"
Not reproduced. The complete SQLite suite ran in 57 s and PG16 in 95 s with no hang. The only hang I observed was my own new stream tests (frozen fake clock), fixed. The custom runner still has no per-test timeout, so a future hang would again block silently; I did not add one or skip anything. Treat the original report as unexplained rather than resolved.

## Not verified / blocked
- **FastAPI routes never ran over real HTTP** (pip is blocked here). SSE, `/ui`, `/auth/me`, CSP/headers, CORS and middleware are verified at source level only. The 22 `test_api_testclient` tests are skipped.
- Real pytest not used (custom runner + shim). No Docker daemon; CI, Docker and Windows NOT RUN.
- No real exchange or AI server contacted. The browser tests use test doubles.
- Safari/Firefox not tested; screen-reader behavior not tested (ARIA roles present only).
- `/ui` CSP needs `'unsafe-inline'`; Google Fonts are blocked by it. Sidebar "DEMO MODE"/"Online" and other pages remain simulated and are not backend data.
- A cancelled upload cannot prove the server did not import; the UI says so.

## Security notes
Read-only activity API behind `read`; import behind `strategies`; server computes hashes/fingerprints; nothing in a package executes; import never enables or selects; sessions in memory only; error frames and UI errors contain no stack traces; unknown orders are never shown as filled/failed.

## Rollback
Remove `backend/dewako/activity/`, the new routes in `api/main.py`, the `RISK_APPROVED`/`ENGINE_*` audit calls, and the two new pages/script tags in `index.html`. No migration was added in 4.4 (migrations 6 and 7 are from 4.2/4.3).

## Manual HTTP check (you can run this on your machine)
`pip install fastapi httpx pytest uvicorn`, then `cd backend/tests && python -m pytest test_api_testclient.py` and the other suites. To use the dashboard: set `DEWAKO_BOOTSTRAP_TOKEN`, `ENCRYPTION_KEYS`, `DEWAKO_TRADING_DB`, run `uvicorn dewako.api.main:app`, `POST /auth/bootstrap`, then open `http://127.0.0.1:8000/ui`.

## Changed / new files
backend: `activity/{__init__,events,service,stream}.py`, `api/main.py`, `execution/engine.py`, `runtime/application.py`, `strategy_packages/manager.py`; tests: `test_activity.py`; frontend: `prototype/{index.html,activity.js,packages.js}`, `tests/activity.test.js`, `browser_tests/test_dashboard_browser.py` + `shots/`; `scripts/standin_http_harness.py`; docs: `LIVE_ACTIVITY.md`, `STRATEGY_PACKAGES.md`, `evidence/phase4_4_*`; this report.
