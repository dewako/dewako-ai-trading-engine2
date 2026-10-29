# Phase 5.2 Windows Acceptance Test Report

Date: 2026-10-29  
Project: `C:\AI_Trading\dewako_phase5_2_reviewed`

## Baseline before source changes

The supplied folder is an extracted source package, not a Git checkout. There is no `.git` directory, no active virtual environment, and no existing Dewako process or listener on port 8000 at initial inspection. The source and prior reports were read before modification; no application code had been changed when this baseline was recorded.

The first local verification found one reproducible UI defect in the served dashboard: submitting the empty sign-in form on Live Activity sent `POST /auth/login`, returned HTTP 422, and displayed the generic message `Sign-in failed (422)`. The Username and Password inputs had no required-field validation. The empty submission used no credentials and created no session. This defect is recorded for a minimal fix and browser regression check below.

### Baseline environment and services

- Windows 11 Pro, version 10.0.26200.
- Python 3.13.15 and pip 26.2.1 were active. The repository recommends Python 3.12 in the Windows instructions and declares Python `>=3.11`; Python 3.12 is not installed here.
- Installed dependencies included FastAPI 0.162.0, Uvicorn 0.54.0, Pydantic 2.13.5, cryptography 50.0.2, httpx 0.28.1, and pytest 9.1.1. Pydantic is below the declared `>=2.14.1` minimum. No dependencies were installed or changed.
- Node.js 24.19.0 and npm 11.17.0; frontend tests use Node's built-in test runner and no `package.json` was found.
- Docker CLI 29.9.2 and Compose 5.6.1 are installed, but the Docker Desktop engine was unreachable.
- Google Chrome 156.0.8078.48 is installed. No project virtual environment was present.
- No application secrets or required bootstrap/database variables were configured in the launching shell. Secret values were not printed.
- No PostgreSQL listener was found at the initial port check. A later check observed `postgres.exe` listening on `0.0.0.0:5432` and `[::]:5432`; it was not started or contacted by these tests, and its owning service/database could not be verified. No PostgreSQL test was run against it.

## Baseline checks completed before the fix

- Full SQLite/backend suite with HTTP tests required: **426 passed, 0 failed, 3 skipped**, exit code 0, 131.81 seconds. The skips are PostgreSQL-only. One Starlette/httpx deprecation warning was emitted.
- Focused executable-price, market-execution, reservation, live-activation, and strategy-package suite: **89 passed, 0 failed, 0 skipped**, exit code 0, 15.49 seconds. The SELL send-time minimum-notional/lot-size regression is included.
- Frontend Node suite: **9 passed, 0 failed, 0 skipped**, exit code 0.
- Python compilation: exit code 0.
- Secret scan: exit code 0, 0 findings.
- Docker Compose test configuration parsed successfully, but PostgreSQL services and tests were not run because Docker Desktop was unavailable.
- The app was launched on `127.0.0.1:8000`; `/health` returned HTTP 200 with `READY`. `/ui/` and required CSS/JavaScript assets returned HTTP 200; `/ui` redirected to `/ui/`; unauthenticated `/auth/me` returned 401; an invalid API route returned 404.
- Google Chrome navigation, page-level behavior, responsive layout, mode-lock display, and authentication failure results are detailed in the final report after the regression retest.

The previous Phase 5 reports and evidence files describe earlier environments/runs. Their results are retained as historical evidence and are not substituted for the checks above.

## A. Environment

| Item | Observed |
|---|---|
| Windows | Windows 11 Pro, 10.0.26200 |
| Python / pip | Python 3.13.15; pip 26.2.1. Python 3.12 is not installed. The repository declares Python `>=3.11`, while its Windows README example recommends 3.12. |
| Node / npm | Node v24.19.0; npm 11.17.0 |
| Docker / Compose | Docker CLI 29.9.2; Compose v5.6.1. The Docker Desktop Linux engine pipe was unavailable, so no containers ran. |
| Chrome | Google Chrome 156.0.8078.48; launched directly with an isolated temporary profile and controlled over its local DevTools protocol. |
| Virtual environment | No project `.venv` or `venv` found. |
| Git | No `.git` directory; branch and working-tree status are unavailable. |
| Frontend package manager | No frontend `package.json`; the documented frontend suite uses Node's built-in test runner. |
| PostgreSQL | A later inspection found `postgres.exe` PID 13556 listening on `0.0.0.0:5432` and `[::]:5432`. Its service, version, database, and test credentials could not be verified. It was not contacted or changed. Docker was unavailable. |

Installed Python packages were FastAPI 0.162.0, Uvicorn 0.54.0, Pydantic 2.13.5, cryptography 50.0.2, httpx 0.28.1, and pytest 9.1.1. FastAPI, Uvicorn, cryptography, httpx, and pytest meet the declared minimums. **Pydantic 2.13.5 is below the project's declared `>=2.14.1` minimum.** `pytest-timeout`, Playwright, Ruff, and pip-audit were not installed. No dependency was installed, upgraded, or downgraded.

No API credentials or bootstrap/database secrets were printed. The project launcher generated a reusable per-user development encryption key outside the source tree because none was configured; the key contents were not displayed. The app ran in its launcher-selected development configuration, bound only to loopback, and remained locked from live execution.

## B. Application startup

### Commands and observations

- The cold-start invocation was `python scripts\run_prototype.py --help` from the project root. The script does not implement a help option, so it proceeded through its normal startup path. Uvicorn started on `http://127.0.0.1:8000` and logged successful application startup without an unexpected traceback.
- The documented Windows launcher was then run as `cmd.exe /c run_prototype.bat`. It exited 0, detected the healthy server already running on port 8000, and reused it rather than starting a duplicate.
- Occupied-port behavior was tested by binding a temporary loopback listener on port 8001 and running `python scripts\run_prototype.py` with `DEWAKO_PROTOTYPE_PORT=8001`. The expected bind failure was reported as Windows error 10048, the attempted Uvicorn instance logged `Application shutdown complete`, and no listener remained on 8001. The launcher process exited 3. The existing app on 8000 stayed healthy.
- The existing app returned `/health` HTTP 200 with `{"status":"ok","state":"READY"}`.
- `/ui/`, `/ui/app.css`, `/ui/app.js`, `/ui/activity.js`, and `/ui/packages.js` each returned HTTP 200. `/ui` redirected to `/ui/` with HTTP 307.
- Unauthenticated `/auth/me`, `/safety/status`, `/orders`, and `POST /safety/live/enable` returned HTTP 401. An empty login request returned HTTP 422 before the UI fix; an invalid synthetic login returned HTTP 401 with a generic message. `/api/does-not-exist` returned HTTP 404.
- The startup log contained optional `GET /favicon.ico` HTTP 404 responses. This is not a required dashboard asset; all required page assets returned 200.
- After browser validation, a Ctrl-Break was sent to the test launcher console. Uvicorn logged `Shutting down`, `Application shutdown complete`, and `Finished server process [21800]`; the launcher session exited 0. Follow-up inspection found no listener on ports 8000, 8001, or 9222. The isolated Chrome instance was closed and its temporary profile removed.

No production endpoint or exchange URL was contacted. No API credentials, testnet credentials, or orders were used.

## C. Browser results

The actual installed Google Chrome binary was used, not `file://` and not the VS Code embedded Electron browser. The browser was opened at `http://127.0.0.1:8000/ui/` with a dedicated temporary profile and interacted with through Chrome DevTools Protocol automation; this was not a human mouse/keyboard session. The single-page navigation changed content while retaining the `/ui/` URL.

### Navigation and page type

- All 17 visible navigation items opened and rendered content.
- **Backend-connected:** Live Activity and Strategy Manager. Both displayed a sign-in form and clearly stated that their content comes from the backend.
- **Simulated/demo:** Dashboard, Markets, Strategies, Backtesting, Paper Trading, Live Trading, Positions, Orders, Performance, AI Lab, News & Events, Exchange Manager, Risk Manager, Logs, and Settings. Their displayed demo/simulated labels were present.
- Live Activity and Strategy Manager could not be authenticated because no test account was configured. Empty and invalid synthetic login attempts were rejected; no user credentials were used. Therefore backend activity records, live stream empty-state behavior, package listing/details, and authenticated import workflow were **not verified in the browser**. The API and service-level tests ran separately.

### Controls exercised

- Dashboard date range and market selection; market search navigated to Markets with the selected symbol.
- Notifications opened and closed. The emergency-stop confirmation opened and was cancelled without activating it.
- Market indicator control toggled.
- Strategy builder opened; an empty strategy was rejected with a specific name-validation message. Code-plugin UI explicitly reported that code plugins are disabled in the demo. Strategy details expanded. “Import Algo ZIP” navigated to the authenticated Strategy Manager login page; no file was selected or uploaded.
- A default backtest completed using generated demo data only.
- Paper Trading pause/resume and read-only Configure dialog behaved as demo controls. No exchange order was submitted.
- Live Trading's only control was disabled. Selecting LIVE in the top-level demo mode selector displayed `LIVE TRADING · EXECUTION DISABLED IN THIS DEMO`; the selector was returned to PAPER.
- Orders filter, Exchange Manager add-form empty validation (no key/secret entered), Risk Manager stop confirmation/cancel, Logs search, and Settings checkbox were exercised.
- No authenticated refresh/stream control could be exercised without a test account. All tested browser network requests were local to 127.0.0.1; no order endpoint or external exchange endpoint was called.

### Layout, console, and network

| Viewport | Document/client width | Result |
|---|---:|---|
| 1920×1080 | 1920 / 1920 | No horizontal overflow |
| 1366×768 | 1366 / 1366 | No horizontal overflow |
| 390×844 | 390 / 390 | No horizontal overflow |

The page title, navigation, layout, styling, and JavaScript rendered. No Chrome console errors, uncaught exceptions, failed requests, or failed required-asset responses were observed. Required CSS and JavaScript assets all returned 200. The optional favicon 404 is noted above.

The post-fix empty-login check confirmed both inputs are required. Submitting with both fields empty, or with only a username, produced no `/auth/login` request. Supplying synthetic invalid credentials still produced the expected generic “Invalid username or password” message and HTTP 401.

Saved evidence:

- [Chrome dashboard screenshot](./evidence/phase5_2_windows_chrome_dashboard.png)
- [Chrome navigation, viewport, lock, login, and network results](./evidence/phase5_2_windows_chrome_results.json)
- [Chrome simulated-control results](./evidence/phase5_2_windows_chrome_controls.json)
- [Chrome strategy-control results](./evidence/phase5_2_windows_chrome_strategy_controls.json)

## D. Automated tests

Durations below are from this session. The full backend run occurred before the frontend-only login validation fix; the relevant frontend tests and real-Chrome regression check were rerun after that fix.

| Command | Exit code | Passed | Failed | Skipped | Duration / notes |
|---|---:|---:|---:|---:|---|
| `Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed\backend'; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; python -m pytest -q -p no:cacheprovider tests` | 0 | 426 | 0 | 3 | 131.81 s. PostgreSQL-only skips; one Starlette/httpx deprecation warning. HTTP tests were required and ran. |
| `Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed\backend'; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; python -m pytest -q -p no:cacheprovider tests\test_executable_price_accounting.py tests\test_market_execution_guards.py tests\test_risk_reservations.py tests\test_live_activation.py tests\test_strategy_packages.py` | 0 | 89 | 0 | 0 | Baseline: 15.49 s; post-audit-assertion retest: 26.25 s. Includes the SELL send-time bid/minimum-notional/lot-size regression and strategy-package validation. |
| `Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed\backend'; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; python -m pytest -q -p no:cacheprovider tests\test_executable_price_accounting.py -k 'sell_that_passes_initial_validation or blocked_sell_creates_no_retry or sell_whose_notional_still_meets_the_minimum_at_the_send_time_bid'` | 0 | 3 | 0 | 0 | 2.67 s; 19 deselected. SELL send-time notional, persisted audit outcome, successful threshold control case, and duplicate/no-retry checks. |
| `node --test frontend/tests/*.test.js` (after fix) | 0 | 9 | 0 | 0 | 0.122 s |
| `node --check frontend\prototype\app.js`, `node --check frontend\prototype\activity.js`, `node --check frontend\prototype\packages.js` | 0 | 3 files checked | 0 | 0 | 0.144 s total |
| `python -m compileall -q frontend\browser_tests\test_dashboard_browser.py` | 0 | N/A | N/A | N/A | Passed; syntax check only; 0.094 s. |
| `python scripts\build_standalone_dashboard.py` | 0 | N/A | N/A | N/A | Generated the standalone dashboard from the changed source; 0.064 s. |
| `python scripts\scan_secrets.py` | 0 | N/A | N/A | N/A | 0 findings; final report-inclusive scan 0.277 s. |
| `docker compose -f docker-compose.test.yml config --quiet` | 0 | N/A | N/A | N/A | Configuration parse only; 0.319 s. No container started. |
| Real Chrome smoke and UI controls via local Chrome DevTools Protocol | 0 | 17 navigation items; all recorded checks passed | 0 observed browser exceptions | N/A | 2.533 s for the main navigation/viewport/login run; subsequent control checks were not separately timed and are saved separately. |
| `python frontend\browser_tests\test_dashboard_browser.py` | N/A — NOT RUN | N/A | N/A | N/A | Python Playwright is not installed. |
| `docker compose -f docker-compose.test.yml run --build --rm tests` | N/A — NOT RUN | N/A | N/A | N/A | Docker Desktop engine was unavailable; Compose configuration parsing alone passed. |
| Ruff / pip-audit checks | N/A — NOT RUN | N/A | N/A | N/A | Neither executable is installed in the active environment. |

The SELL send-time regression was rerun after adding an explicit persisted-audit assertion: all 3 targeted tests passed (19 deselected), and the surrounding execution-safety/package suite passed 89/89. The Playwright browser suite `frontend\browser_tests\test_dashboard_browser.py` was **NOT RUN** because Python Playwright and its browser test dependencies are not installed. The real installed Chrome was nevertheless exercised directly and its results are saved above. The full PostgreSQL Docker command was **NOT RUN** because the Docker Desktop engine was unavailable. Ruff and pip-audit were **NOT RUN** because those tools were absent. These are not counted as passes.

## E. Database

- **SQLite: PASS.** The full backend suite completed with 426 passed, 0 failed, and 3 PostgreSQL-only skips. It includes SQLite migrations/configuration, transaction and constraint checks, crash/recovery fakes, risk reservations, order lifecycle, and audit-integrity tests. The focused run also passed 89/89 execution-safety and strategy-package tests.
- **PostgreSQL: BLOCKED.** The Compose YAML parsed, but Docker could not connect to `dockerDesktopLinuxEngine`. The observed host listener on port 5432 had no verified test database/credentials and was not contacted. No PostgreSQL migration, health, restart, or test run is claimed for this session. Earlier PostgreSQL results in the readiness matrix are historical, not reproduced here.
- **Interruption/recovery:** bounded crash/reconciliation tests passed against synthetic/fake adapters as part of the backend suite. No interruption test was run against a real exchange or the observed PostgreSQL process. Unknown order outcomes were never sent to an exchange or blindly retried in this assignment.
- No database was reset, deleted, or intentionally overwritten.

## F. Security and quality

- Secret scanner: **PASS**, 0 findings. Secret values were not printed.
- Python compilation and dashboard JavaScript syntax checks: **PASS**.
- Authentication/authorization and live-gate tests: **PASS** in the full backend suite. Direct unauthenticated access to `/auth/me`, `/safety/status`, `/orders`, and `/safety/live/enable` returned 401.
- Live activation in the UI remained visibly disabled; no authenticated activation attempt, real order, testnet order, or production request was made.
- Docker Compose configuration validation: **PASS (configuration only)**; Docker image/runtime checks were blocked.
- Ruff and dependency audit: **NOT RUN**; Ruff and pip-audit were not installed.
- Dependency conformance limitation: installed Pydantic is below the declared minimum, despite the passing tests. No dependency change was made.
- The local PostgreSQL listener on all interfaces (port 5432) was left untouched because its ownership and purpose were not established.

## G. Defects and changes

### LOW — Empty sign-in submission bypassed browser validation

- **Root cause:** Username and Password inputs in `loginPanel()` lacked `required` attributes. An empty form was sent to the API, which correctly returned 422; the UI then displayed the generic `Sign-in failed (422)` message.
- **Fix:** Added native required-field validation to both inputs. Regenerated the single-file standalone dashboard and added `t_empty_login_is_blocked_by_required_fields` to the browser suite.
- **Files changed:** `frontend/prototype/app.js`, `frontend/prototype/index.standalone.html`, and `frontend/browser_tests/test_dashboard_browser.py`.
- **Regression result:** Real Chrome verified empty and partially complete submissions trigger browser validation and send no login request. A subsequent synthetic invalid login still returned 401 and displayed the generic credential error. Node tests passed 9/9; all three dashboard JavaScript files passed `node --check`.
- **Retest limitation:** The Playwright suite containing the new automated browser test was not run because Playwright is unavailable. Equivalent validation behavior was reproduced in the installed Google Chrome using local browser automation.

No backend/execution defect was confirmed in the current run. The open quote-based sizing limitation remains: a cap breach after executable-price movement is safely blocked, but automatic quantity reduction is not implemented. The optional favicon 404 is non-blocking and does not affect a required asset.

## H. Readiness decision

| Area | Classification | Evidence and limits |
|---|---|---|
| Backend | **PASS WITH DOCUMENTED LIMITATIONS** | Started and returned READY; all SQLite/backend tests passed. Pydantic version is below the declared minimum. |
| Frontend | **PASS WITH DOCUMENTED LIMITATIONS** | Node 9/9, syntax checks, actual Chrome navigation and simulated controls passed. Authenticated backend pages were not fully exercised. |
| Real Chrome behavior | **PASS WITH DOCUMENTED LIMITATIONS** | Google Chrome 156 tested at all requested sizes with no browser exceptions or required asset failures. No authenticated account was available. |
| SQLite | **PASS** | Full suite passed; 3 PostgreSQL-only tests skipped. |
| PostgreSQL | **BLOCKED** | Docker engine unavailable; the unidentified host listener was not used. |
| Execution safety | **PASS WITH DOCUMENTED LIMITATIONS** | Paper/simulation-only tests passed; the UI stayed locked and no exchange was contacted. Venue behavior and quote-based sizing remain unverified/not implemented. |
| Security checks | **PASS WITH DOCUMENTED LIMITATIONS** | Secret scan, compilation, authorization tests, and live-lock tests passed. Ruff, dependency audit, and Docker runtime checks were unavailable. |
| Overall Phase 5.2 paper/simulation readiness | **PASS WITH DOCUMENTED LIMITATIONS** | Ready to continue paper/simulation development only. This is not testnet or live readiness. |

**Testnet: NOT READY. Live trading: NOT READY — KEEP LOCKED.** No production exchange endpoint, real credential, testnet, or order was used.

### Required follow-up

1. To reproduce the PostgreSQL suite, start Docker Desktop and run `docker compose -f docker-compose.test.yml run --build --rm tests`. The configured test database is disposable/tmpfs; do not substitute the unidentified port-5432 service.
2. To reproduce the Playwright suite after installing Python 3.12 (the README's Windows example): run `py -3.12 -m venv backend\.venv`, then `backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt -c backend\constraints-test.txt -r backend\requirements-test.txt`, then `backend\.venv\Scripts\python.exe -m playwright install chromium`, and finally `backend\.venv\Scripts\python.exe frontend\browser_tests\test_dashboard_browser.py` from the project root. Do not install into or change the system Python environment.
3. If authenticated browser coverage is needed, use a dedicated disposable SQLite database and a one-time bootstrap token to create a test-only admin. Do not use personal or exchange credentials.
4. Verify the owner and intended exposure of the `postgres.exe` listener on port 5432 before using or changing it.
