# Dewako AI Trading Engine Phase 4.5 — Independent Verification

**Verification date:** 2026-10-28 (safety-gap remediation and regression rerun)  
**Phase 5 gate:** **READY FOR PHASE 5 DEVELOPMENT IN PAPER/SIMULATION MODE**  
**Testnet gate:** **NOT READY**  
**Live-trading gate:** **NOT READY — LIVE MUST REMAIN LOCKED**

## Executive verdict

The two confirmed safety gaps from the prior audit have now been fixed in the decision and execution paths. Current verification on 2026-10-28: the full local backend suite passed **404 tests, 0 failed, 3 skipped**; the real-app Chromium suite passed **13/13**; the frontend Node suite passed **9/9**; and the Docker PostgreSQL 16 suite passed **403 tests, 0 failed, 4 skipped**. The three local skips are PostgreSQL-only; the four Docker skips are repository-wide checks unavailable in the backend-only image. The final backend/Node/browser runs total **426 passed, 0 failed, 3 skipped**, while PostgreSQL is reported separately.

Phase 5 work that stays inside the existing paper/simulation boundary may proceed. Execution now also requires explicit quote-guard configuration; absent or partial spread/deviation/quote-age values block submissions. This is not authorization for testnet or live trading. No real exchange order endpoint was called, no real credentials were used, and no real order was submitted.

The original paper/live separation and durable fake-exchange crash tests remain useful evidence, but they are not exchange evidence. On 2026-10-28 the final code passed the PostgreSQL 16 Compose suite (403 passed, 4 scope skips) and the SQLite suite (404 passed, 3 PostgreSQL-only skips). The test image used Python 3.12.16, Pydantic 2.14.1, FastAPI 0.162.0, and pytest 9.2.0. The installed system Python 3.13.15 full-suite run used Pydantic 2.13.5, below the backend manifest minimum 2.14.1; the Docker run supplies dependency-minimum-compatible full-suite evidence. These results do not verify exchange execution or make the system eligible for testnet/live trading.

## Scope, identity, and environment

- Workspace: extracted project at `C:\Users\USER\Desktop\Dewako_AI\dewako_phase4_5_extracted\dewako`.
- Git: `git status --short --branch` and `git rev-parse HEAD` both reported “not a git repository.” Branch, commit, and Git working-tree status are therefore unavailable.
- A read-only comparison against the previous audited archive (`dewako_phase4_5_audited.zip`, SHA-256 `cfe82c214a8da29e66b3beff662f8b7d8526fcf7c147a6c1c1557cbf540d4dec`) was part of the prior audit; this continuation has no Git metadata for a commit-level comparison. The 2026-10-28 changes intentionally modify Python safety behavior and tests, add quote/freshness configuration documentation, and refresh four browser screenshot outputs. No database or user configuration file was overwritten; no `.env` remains.
- Operating system: Windows 11. Final local full-suite Python: 3.13.15 (Pydantic 2.13.5, below the backend minimum); bundled `.venv`: Python 3.14.7/Pydantic 2.14.1 (Python 3.14 is not the documented supported test version). Docker test environment: Python 3.12.16/Pydantic 2.14.1.
- Node.js `v24.19.0`, npm `11.17.0`.
- The pinned test dependencies installed successfully into a temporary directory, not into the project or the user’s global Python environment. Relevant resolved versions: FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.1, cryptography 50.0.2, pytest 9.1.1, httpx 0.28.1, Uvicorn 0.54.0, Playwright 1.49.1.
- Docker Desktop `4.97.0`, Engine `29.9.2` (Linux/amd64), and Compose `v5.6.1` were available for this continuation. Test containers used Python `3.12-slim` and PostgreSQL `16.15`. Temporary `.env` files contained only test-only database values and were removed after each run. No exchange credentials, bootstrap token, or live-trading configuration was provided.

## Architecture summary

- Python/FastAPI backend in `backend/dewako`: API/authentication, market-data providers, strategy and risk evaluation, a durable execution state machine, reconciliation, safety gates, and SQLite/PostgreSQL persistence.
- PostgreSQL is implemented using the project’s standard-library wire client. Production environment configuration requires `DEWAKO_DATABASE_URL=postgresql://...` and fails rather than falling back to SQLite. Development can use SQLite.
- `ExecutionEngine` persists decisions, intent, order state, and audit records before submission. Ambiguous writes enter UNKNOWN/RECONCILING and are looked up rather than blindly resent. The order-state, reservation, and crash tests use deterministic fakes; the paper adapter uses simulated balances and prices.
- Candle freshness is validated from candle-open timestamps, timeframe, closure, sequence, and current time. Execution requires explicit quote thresholds and validates fresh top-of-book bid/ask twice (before intent, then before SUBMITTING); depth-based market impact is not supported.
- Authentication includes scrypt password hashes, hashed opaque sessions, expiry/revocation, lockout/rate limiting, and server-side RBAC. Credential values are Fernet-encrypted with versioned-key rotation. The audit log is scrubbed and hash-chained with database guards; an off-host chain-head anchor is not implemented.
- Binance trading adapter supports only a permission-check request; Bybit and OKX trading adapters are stubs. The public market-data parsers have fake-transport unit tests, not live-service verification. The live/testnet verified-adapter allowlists are empty and TESTNET order execution is refused.
- The dashboard is a prototype. Its global/demo banners explicitly label most pages as simulated. Live Activity and Strategy Manager use backend data; other dashboard pages use generated demo content.

## Verification of previous claims

| Prior claim | Current evidence and status |
|---|---|
| Previous 381 backend passes, 3 skips | **Current rerun: PASS.** 404 passed, 3 skipped, 0 failed on Python 3.13.15. The Docker Python 3.12.16/Pydantic 2.14.1 PostgreSQL run also passed. |
| 13 real-app Chromium tests | **PASS, rerun 2026-10-28.** All 13 passed against local FastAPI/Uvicorn after installing pinned Playwright into a temporary directory; no real exchange endpoints were involved. |
| 9 frontend Node tests | **PASS, rerun 2026-10-28.** All 9 passed. |
| Previous 403 combined passes | **Current result: 426 passed, 0 failed, 3 skipped**, computed from backend 404 + browser 13 + Node 9. PostgreSQL results are separate and not double-counted. |
| Dependency audit clean | **PASS, reproduced** against the temporary pinned dependency set with pip-audit: no known vulnerabilities. |
| No PostgreSQL test evidence | **Correction:** current source includes historical PostgreSQL 16 logs. `docs/evidence/phase4_5_pg16.log` records 354 passed, 0 failed, 30 skipped using the standalone runner; the skips include all 30 HTTP tests. `docs/evidence/pg_restart_check_2026-10-15.txt` records a PostgreSQL 16.15 immediate-restart/reconnect check as PASS. These are historical records, not a current run, and do not establish full PostgreSQL HTTP-suite coverage. |
| PostgreSQL/Docker unavailable | **Correction:** current PostgreSQL Compose suite reran with 403 passed, 4 skipped, 0 failed. Previous SQLite/PostgreSQL deployment smoke tests remain historical evidence. |
| Exchange/testnet execution unverified | **CONFIRMED.** No real exchange or testnet endpoint was contacted. The adapters and execution allowlists substantiate the limitation. |

## Test results and exact commands

All commands below were run from the project root unless noted.

### Current safety-fix rerun (2026-10-28)

| Command | Actual result |
|---|---|
| `Set-Location backend\tests; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; python -m pytest -q .; $rc=$LASTEXITCODE; Remove-Item Env:\DEWAKO_REQUIRE_HTTP_TESTS; exit $rc` | Exit 0; **404 passed, 3 skipped, 0 failed** in 120.48s. Python 3.13.15, pytest 9.1.1, FastAPI 0.162.0, Pydantic 2.13.5. The three skips are PostgreSQL-only. System Pydantic is below the project's `>=2.14.1` minimum; the Docker result below provides the dependency-compatible PostgreSQL run. One Starlette/httpx deprecation warning. |
| `docker compose -f docker-compose.test.yml run --build --rm tests` | Exit 0; **403 passed, 4 skipped, 0 failed** in 87.52s against isolated PostgreSQL 16. The four skips are repository-wide UI/CI checks excluded from the backend-only image. Image runtime: Python 3.12.16, Pydantic 2.14.1, FastAPI 0.162.0, pytest 9.2.0. One Starlette/httpx deprecation warning. Docker emitted an orphan-container notice for existing deployment services; they were not removed. |
| `python -m pytest -q test_market_execution_guards.py` (from `backend\tests`) | Exit 0; **23 passed, 0 failed, 0 skipped** in 0.86s. Includes candle and quote guard regressions plus BUY/SELL checks that the paper adapter's advertised ask/bid matches its simulated execution price. |
| Targeted: `python -m pytest -q test_market_execution_guards.py test_market_data.py test_audit_lease_freshness.py test_strategies.py test_hardening_orders.py test_risk_reservations.py test_crash_and_recon.py test_crash_matrix.py test_lifecycle_failure.py test_hardening_safety.py test_adapters_and_fills.py` (from `backend\tests`, HTTP tests required) | Exit 0; **185 passed, 3 skipped, 0 failed** in 45.70s (earlier targeted run before the final paper quote consistency regression was added). Skips are PostgreSQL-only. |
| `node --test frontend/tests/activity.test.js` | Exit 0; **9 passed, 0 failed, 0 skipped**. |
| `DEWAKO_E2E_SERVER=real python frontend\browser_tests\test_dashboard_browser.py` with temporary pinned Playwright 1.49.1 on `PYTHONPATH` | Exit 0; **13 passed, 0 failed** against actual FastAPI/Uvicorn. No exchange endpoints. The temporary dependency directory was removed. |
| Initial browser invocation before the temporary install | Exit 1; `ModuleNotFoundError: No module named 'playwright'`. Pinned Playwright was installed only into a temporary directory, after which the same browser suite passed. |
| `python -m compileall -q backend\dewako backend\tests` | Exit 0. |
| `python scripts\scan_secrets.py` | Exit 0; `secret scan: 0 finding(s)`. |
| Final targeted Ruff check for changed Python files | **NOT RUN:** `ruff` executable was unavailable; not installed because no dependency change or missing-dependency test failure required adding tooling. |
| Earlier targeted iteration and correction history | Initial targeted run: exit 1, 97 passed/4 failed because old market-data test fixtures used future candles and bot fixtures supplied no controllable current clock. Updated fixtures to use valid deterministic timestamps/clocks; subsequent targeted run passed. A later broad targeted run: exit 1, 183 passed/3 skipped/1 failed because an exit-order fixture retained a 100 reference after the simulated paper price changed to 90. Updated the fixture's approved reference to 90; final targeted run above passed without weakening assertions. |

### Previous audit run (2026-10-27)

| Command | Result |
|---|---|
| `Push-Location backend\tests; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; py -3.13 -m pytest -q .; $rc=$LASTEXITCODE; Pop-Location; exit $rc` | Exit 0; **381 passed, 3 skipped, 0 failed**, 114.21 s. Three skips were PostgreSQL-only. One Starlette/httpx `TestClient` deprecation warning. The required-HTTP flag prevents silent HTTP-test skips. |
| `node --test frontend/tests/activity.test.js` | Exit 0; **9 passed, 0 failed, 0 skipped**. |
| `py -3.13 -m pip install --target "<temporary-deps-dir>" -r backend\requirements.txt -c backend\constraints-test.txt -r backend\requirements-test.txt` | Exit 0. The complete declared test set, including Playwright, installed at the project’s pinned versions. A transient package-index timeout was retried successfully. |
| `$env:PYTHONPATH="<temporary-deps-dir>;backend"; $env:DEWAKO_E2E_SERVER='real'; py -3.13 frontend\browser_tests\test_dashboard_browser.py` | Exit 0; **13 passed, 0 failed** against actual local FastAPI/Uvicorn and SQLite. Scripted fakes only. |
| `Push-Location backend\tests; $env:DEWAKO_REQUIRE_HTTP_TESTS='1'; py -3.13 -m pytest -q test_api_testclient.py; $rc=$LASTEXITCODE; Pop-Location; exit $rc` | Exit 0; **22 passed, 0 failed**, 12.41 s, after the test-module docstring-only update. One Starlette/httpx deprecation warning. These tests are already included in the 381 backend total. |
| `py -3.13 scripts\scan_secrets.py` | Exit 0; `secret scan: 0 finding(s)`. |
| `py -3.13 -m compileall -q backend\dewako backend\tests` | Exit 0. |
| `py -3.13 -m pip check` | Exit 0; `No broken requirements found.` |
| `py -3.13 -m pip install --target "<temporary-tools-dir>" pip-audit ruff` | Exit 0. The audit/lint tools were installed outside the project. |
| `py -3.13 -m pip_audit --path "<temporary-deps-dir>"` | Exit 0; `No known vulnerabilities found`. |
| `py -3.13 -m ruff check backend\dewako --select E9,F63,F7,F82` | Exit 0; `All checks passed!` (Ruff 0.17.3 installed in a temporary tool directory). |
| PyYAML `safe_load` for `.github/workflows/ci.yml`, `docker-compose.yml`, `docker-compose.test.yml` | All three parsed. This is structural parsing only, not CI or container execution. |
| `docker compose -f docker-compose.test.yml config --quiet` | Exit 0. |
| `docker compose -f docker-compose.test.yml run --build --rm tests` (first Docker run) | Exit 1; **379 passed, 4 skipped, 1 failed**. The failing invariant test resolved its source path as `/backend/dewako/api/main.py` in the backend-only image. No assertions were weakened. |
| `py -3.13 -m pytest -q test_phase45_invariants.py::test_no_activity_or_package_route_can_submit_cancel_or_retry_an_order` (from `backend\tests`) | Exit 0; **1 passed** after fixing the test's backend-root path calculation. |
| `docker compose -f docker-compose.test.yml run --build --rm tests` (after fix) | Exit 0; **380 passed, 4 skipped, 0 failed**, 129.25 s, one Starlette/httpx deprecation warning. The four skips are repository-wide UI/CI checks whose frontend or `.github` inputs are absent from this backend-only build context; the HTTP suite was required. |
| Default deployment Compose with a temporary empty `.env` | Exit 0; backend `/health` returned `READY` in the default SQLite configuration; clean shutdown completed. |
| PostgreSQL deployment profile with a test-only `.env` and `docker-compose.postgres.yml` | Exit 0 after the startup-order fix; Compose waited for PostgreSQL health, `/health` returned `READY`, and an in-container probe reported `database_dialect=postgresql initialized_tables=34`. `pg_isready` succeeded. |
| Prototype Compose service on alternate localhost port 18080 | Exit 0; HTTP status 200. The configured host port 8080 could not be bound on this machine, so the complete all-services launch was not run on its default port. |
| Hosted GitHub Actions/CI | **NOT RUN.** |

For the successful browser run, `<temporary-deps-dir>` resolved to `C:\Users\USER\AppData\Local\Temp\dewako-phase45-verification-d54573ecbf824a38a1e60ac5a26e1b83`. The pip-audit/Ruff `<temporary-tools-dir>` resolved to `C:\Users\USER\AppData\Local\Temp\dewako-audit-tools-4b8295a20afa4c88873e75e3e709e7e8`; pip-audit scanned the pinned dependency directory above. The tools ran with `$env:PYTHONPATH="<temporary-tools-dir>;<temporary-deps-dir>"`. These temporary directories were used only for the audit and removed afterward.

One initial browser command using the system Python exited 1 because that environment lacked Playwright. The declared pinned test dependencies were then installed to a temporary directory and the real-app suite passed. No test was skipped to hide that initial missing prerequisite.

## Stage results

### PostgreSQL integration

**Current run: PASS with explicit scope skips.** `docker compose -f docker-compose.test.yml run --build --rm tests` built the backend image, waited for isolated PostgreSQL 16 to become healthy, and ran **403 passed, 4 skipped, 0 failed** with `DEWAKO_TEST_DB=postgres`, `DEWAKO_EXPECT_PG_MAJOR=16`, and required HTTP tests. The four skips are UI/repository-wide checks whose inputs are not included in the backend-only image; they are not counted as passes. One initial failure in an earlier audit run exposed a source-path assumption in a test and was fixed without weakening assertions.

Historical evidence is more than “none”: the repository contains a PostgreSQL 16 standalone run of **354 passed, 0 failed, 30 skipped**, and a PostgreSQL 16.15 immediate-restart check that reports reconnect success, audit-chain verification, and rollback of an uncommitted transaction. The 30 skips in the standalone log include the real HTTP suites. The restart log explicitly limits its scope; it does not test a restart during order update, incident creation, or recovery. These records support partial prior PostgreSQL coverage, but they do not satisfy the requested current full-suite gate.

The reproducible Docker test command is:

```powershell
docker compose -f docker-compose.test.yml run --rm tests
```

The test compose file starts PostgreSQL 16 on an internal network with a throwaway data volume and runs pytest from the backend directory. It does not include `frontend/` or `.github/`, so four repository-wide tests explicitly skip in that image. CI remains responsible for complete repository-layout checks. The production-style PostgreSQL profile was separately started with `docker-compose.postgres.yml`; it uses a PostgreSQL health check and waits before starting the backend. The backend connected to PostgreSQL, completed migrations (34 tables), and returned `READY`. No live or testnet endpoints were used.

### Order lifecycle and crash recovery

The full backend suite passed the deterministic order/failure tests. The evidence supports correct local state handling, not exchange behavior:

| Scenario | Injected condition / expected behavior | Observed local result and evidence |
|---|---|---|
| Request validation and risk approval/rejection | Invalid, unsigned, over-limit, or unapproved decisions must not reach an adapter. | **PASS** in the fake/paper suite (`test_core.py`, `test_hardening_orders.py`, `test_hardening_safety.py`). |
| Durable intent and SUBMITTING | Persist before external send; rejection stays rejected; acceptance is recorded. | **PASS** (`test_hardening_orders.py`, `test_crash_matrix.py`). |
| Partial/full fills and cancellation | Apply fills atomically/incrementally; cancellation is not final until confirmed. | **PASS** (`test_partial_then_full_fill_updates_position_incrementally`, `test_cancel_open_order`, crash matrix tests 5–7). |
| Timeout, HTTP 500, ambiguous response | Unknown write outcome is reconciled; do not submit twice. | **PASS with fakes** (`test_timeout_then_exchange_has_order_is_not_resent`, `test_http500_and_malformed_and_wrong_id_go_to_unknown_path`, `test_exchange_timeout_http500_and_credential_failure_never_double_send`). |
| Connection/process loss after exchange acceptance | Persisted SUBMITTING is recovered through lookup; no second create call. | **PASS with file-backed fake exchange** (crash matrix tests 3–4 and `test_sigkill_after_exchange_accepts_then_recovery_does_not_resend`). |
| Duplicate requests/concurrent workers | Same signal/intent is unique; reservations and order creation do not double count. | **PASS locally** (`test_same_signal_submitted_by_many_workers_makes_one_order_and_one_reservation`, concurrent-engine tests, database uniqueness test). Historical PG16 log also records its PostgreSQL test run, not repeated now. |
| Restart with unresolved/UNKNOWN order; remote positions/orders diverge | Keep capacity reserved, reconcile, create durable findings/incidents, and block risk increases. | **PASS with fakes** (crash matrix tests 8–10; `test_crash_and_recon.py`; `test_risk_reservations.py`). |
| Database unavailable | Fail closed and send nothing. | **PASS** (`test_unreachable_postgres_makes_startup_fail_closed_not_fall_back`, startup/outage and crash matrix tests); current injected PostgreSQL outage tests use an unreachable local address, not a real server interruption. |
| Delayed exchange response | No dedicated controlled delayed-response adapter scenario was found in the executed suite. | **NOT RUN** as a distinct order lifecycle case. The bounded worker/hang tests are not equivalent to a late exchange response. |
| Rate-limited order write / temporary exchange outage | Error normalization is tested; timeout/unavailable reconciliation is tested. | **UNVERIFIED** end-to-end: classification and generic failures pass locally, but no dedicated real-exchange rate-limit contract or provider behavior was verified. |

Client IDs are deterministic and the database enforces local uniqueness. A client ID alone does not establish remote idempotency; all real-exchange duplicate handling remains **UNVERIFIED**. The crash harness kills real local child processes against a scripted file-backed fake exchange. It does not simulate power loss, kernel/disk failure, or a real exchange.

### Exchange adapter capability audit

“Locally tested” below means parser/signing/capability behavior against fixtures or fake transports, not a live service.

| Capability | Binance | Bybit | OKX |
|---|---|---|---|
| Authentication / permission check | Signed HMAC permission-check code exists; signature/response shape tested with fake transport. It was not called against Binance. No account trading authentication path. | Trading adapter is a stub; no authentication implementation. | Trading adapter is a stub; no authentication implementation. |
| Public market data | Public REST parser/provider exists; unit tests use injected fake transport. Live endpoint behavior is unverified. | Public REST parser/provider exists; unit tests use injected fake transport. Live endpoint behavior is unverified. | Public REST parser/provider exists; unit tests use injected fake transport. Live endpoint behavior is unverified. |
| Trading symbol metadata, lot/minimum/precision rules | Missing from the trading adapter; pre-trade validation helper is separately unit-tested. | Missing. | Missing. |
| Submit/cancel/status lookup/fills/balance/positions/reconciliation | Not implemented; inherited stub methods refuse. | Stub methods refuse. | Stub methods refuse. |
| Official testnet through this adapter | **NOT IMPLEMENTED.** The permission URL is hard-coded to Binance production API; no tested testnet order path. | **NOT IMPLEMENTED.** | **NOT IMPLEMENTED.** |
| Testnet/live verified allowlist | No adapter is allowed. Global allowlists are empty. | No adapter is allowed. | No adapter is allowed. |
| Safe conclusion | Not usable for order execution. No testnet or production trading. | Not usable for order execution. No testnet or production trading. | Not usable for order execution. No testnet or production trading. |

No exchange endpoint was contacted. To test a future adapter safely, it must first have an explicit testnet-only base URL/configuration and code path, separate testnet credentials created by the operator, withdrawal disabled, a testnet-only account and symbol metadata, and tests asserting the request host is the testnet host. The engine schema/environment allowlist and reconciliation flow would need reviewed support before testnet execution could be enabled. Do not reuse production credentials or enable LIVE as part of this verification.

### Market data, risk controls, security, and UI

| Area | Status and evidence |
|---|---|
| Candle parsing, confirmed/closed-candle handling, gaps, pagination, provider fallback | **PASS in fixture tests** (`test_market_data.py`); not verified against public providers. Provider parsers reject non-finite numeric data and drop forming candles. |
| Candle timestamp freshness in market-state construction, bot ticks, and live gate | **IMPLEMENTED; PASS in regression tests.** The newest candle open timestamp plus timeframe determines close and age. Sequence order/continuity, timezone, alignment, OHLCV values, future skew, closed status, and maximum age are validated. Provider success alone does not mark data fresh. See Finding 1. |
| Non-finite signal/reference prices | **PASS in tested boundaries**: SignalEngine rejects a NaN entry signal, public parsers reject non-finite prices, and order validation rejects a non-finite reference price. No direct exchange order is involved. |
| Position, open-order, daily loss, drawdown, correlated exposure, risk reservations, kill switch | **PASS in local deterministic tests.** Limits and failure states are exercised with SQLite and fake/paper adapters; no guarantee against all market conditions is implied. |
| Spread and reference-price deviation | **IMPLEMENTED; PASS with fake/paper quote tests.** Required configured percent limits and quote-age policy validate top-of-book and revalidate before submission. This is not depth-based market-impact protection and does not verify any real venue adapter. See Finding 2. |
| Unknown position, disconnect, active order, strategy change, safety halt | **PASS for covered local fake paths** where assertions exist; real venue state and timing remain unverified. |
| Authentication/RBAC/session expiry/revocation/rate limiting | **PASS locally**, including real FastAPI/TestClient cases in the current backend suite. The system is single-tenant; per-user data ownership is not provided. MFA is not implemented. |
| Credential protection and audit log | Fernet versioned-key encryption/rotation, scrubbing, hash-chain integrity, and tamper tests **PASS locally**. Keys are environment-held; no KMS or off-host audit-head anchor. A database owner can rewrite the full chain without an external anchor. |
| Alerting and backup/restore | **NOT IMPLEMENTED.** Alerts currently write to application/audit logs and optional in-process handlers; no external delivery is configured. Backup/restore is documented as operator-owned; no restore drill was run (**NOT RUN**). |
| Dashboard | **PASS for the tested prototype paths.** The real browser suite checks auth, unknown-order display, reconnect/dedup, denied states, CSP, and demo labels. Most pages remain generated demo content, explicitly labelled in the UI. |
| Docker/CI deployment | Backend image build and both default SQLite and PostgreSQL Compose backend paths **PASS**; PostgreSQL health ordering and migrations were verified. Prototype returned HTTP 200 on an alternate localhost port. The all-services run using configured port 8080 was **BLOCKED** by the host refusing that port. Hosted CI was **NOT RUN**. |

## Findings

### 1. Candle freshness was based on fetch time rather than data time — fixed

- **Severity:** Medium (high-priority blocker before any testnet/live decision-making).
- **Status:** **CONFIRMED and fixed** on 2026-10-28.
- **Previous unsafe behavior:** A successful state-provider call refreshed the live-gate marker using wall-clock fetch time even when its newest candle was old. `build_market_state` accepted stale, future, unordered, or malformed candle sequences.
- **Change and evidence:** Added `backend/dewako/market_data/freshness.py`. Candles must have timezone-aware, timeframe-aligned timestamps; be strictly ordered and gap-free; contain valid finite OHLCV values; be closed; and fall within a configurable age measured from the newest candle's close. Future opens beyond configurable clock skew fail. `build_market_state` and the API state/signal/recommendation paths validate the data. `BotRunner` revalidates custom state-provider output before strategy evaluation or signal/intent persistence. The application market-data worker now validates state and records the actual candle open timestamp and timeframe; `live_gate` rechecks that timestamp rather than provider completion time. No historical timestamp is rewritten.
- **Regression evidence:** `test_provider_success_with_stale_candles_is_blocked_before_intent_creation`, `test_missing_candle_timestamp_fails_closed`, `test_future_dated_candle_fails_closed`, `test_out_of_order_candle_sequence_fails_closed`, `test_fresh_valid_closed_candle_sequence_is_accepted`, `test_missing_market_data_fails_closed`, `test_freshness_validation_exception_blocks_tick_and_creates_no_intent`, plus live-gate worker regressions. The stale-provider case confirms zero signals, intents, and adapter submissions.
- **Remaining limitation:** Provider network behavior and timestamps are not verified against real venues. Gap-free history is intentionally required. Clock and age policy remain operator-configurable within validated bounds.

### 2. No spread or price-deviation threshold was enforced — fixed

- **Severity:** Medium (live-readiness blocker).
- **Status:** **CONFIRMED and fixed** on 2026-10-28 for the normalized adapter quote interface.
- **Previous unsafe behavior:** Execution validated exchange filters, balance, and quantity, but did not require a current bid/ask, bound spread, or compare the executable side with the approved reference price.
- **Change and evidence:** Execution now requires explicit `DEWAKO_MAX_SPREAD_PCT`, `DEWAKO_MAX_PRICE_DEVIATION_PCT`, and `DEWAKO_MAX_QUOTE_AGE_SECONDS` configuration. Missing/partial configuration, missing or malformed quotes, non-positive/non-finite/crossed bid-ask, stale/future timestamps, excessive spread, or excessive deviation block before intent creation. Spread is `(ask-bid)/mid * 100`; market BUY uses ask, SELL uses bid; deviation is `abs(executable-reference)/reference * 100`. `_send` fetches and revalidates the quote before persisting SUBMITTING and calling the adapter. Strategy output is constrained to the newest validated candle close so plugins cannot choose a favorable reference. No XAUUSD-specific limit is hard-coded.
- **Regression evidence:** `test_quote_within_configured_spread_and_deviation_executes`, excessive-spread/deviation, stale/missing/invalid/non-finite quote tests, `test_quote_movement_before_submission_is_revalidated`, `test_missing_quote_guard_configuration_fails_closed`, and the BUY/SELL-parametrized `test_paper_quote_matches_its_simulated_executable_price`. Initial quote failures create no intent and no adapter call; a changed pre-submit quote creates no exchange submission. The paper adapter's bid/ask now matches its own slippage-adjusted fill calculation.
- **Remaining limitation:** This is top-of-book only. The adapter contract does not supply verified executable depth, so market impact/depth-based slippage is **NOT IMPLEMENTED**. No real venue adapter or quote timestamp is integration-tested; no fill-price guarantee is implied. Operators must set instrument/venue-specific values; documented examples are illustrative only.

### 3. PostgreSQL Compose startup raced the database (fixed)

- **Severity:** Medium (deployment availability; live execution remained fail-closed).
- **Status:** **CONFIRMED and fixed** for the supplied PostgreSQL profile.
- **Evidence:** Before the fix, the profile started backend and PostgreSQL concurrently; backend logged `startup step database failed: PGConnectionError` and `/health` reported `DEGRADED_NO_DATABASE`. PostgreSQL became ready afterward, but application startup did not recover. Added a PostgreSQL health check and `docker-compose.postgres.yml` with `depends_on: service_healthy`.
- **Reproduction:** `docker compose --profile postgres-port -f docker-compose.yml -f docker-compose.postgres.yml up --build -d backend postgres redis` with a test-only `.env`. After the fix, Compose showed PostgreSQL healthy before starting the backend, `/health` reported `READY`, and an in-container probe reported PostgreSQL and 34 migrated tables.
- **Impact:** Without ordering, a startup race could leave the API alive but database-degraded until restarted.
- **Fix:** Use the PostgreSQL Compose override for that profile. The default SQLite service is unchanged.

### 4. Real exchange and testnet execution do not exist in verified form

- **Severity:** High for testnet/live eligibility.
- **Status:** **NOT IMPLEMENTED**, by design; no testnet execution was run.
- **Evidence:** Binance adapter only implements a permission check; Bybit/OKX adapter methods inherit refusing stubs. The live/testnet verified allowlists are empty and TESTNET is excluded from enabled order environments.
- **Reproduction steps:** Inspect `exchanges/binance.py`, `exchanges/stubs.py`, `exchanges/registry.py`, and `exchanges/contract.py`; the full backend suite passes adapter refusal/allowlist tests.
- **Impact:** No test or local fake proves authentication, filters, order lifecycle, fills, balances, positions, or reconciliation on an exchange.
- **Recommended fix:** Build a dedicated testnet adapter and reconcile it end-to-end against the official sandbox using testnet-only credentials. Keep production URLs/credentials inaccessible to the test process and do not change the live allowlist as part of development.

### 5. Real-money operational controls remain incomplete

- **Severity:** High for any future live authorization.
- **Status:** MFA, off-host audit anchoring, and external alert delivery are **NOT IMPLEMENTED**; backup/restore drills were **NOT RUN**.
- **Evidence:** `docs/LIVE_TRADING_READINESS.md` marks MFA, off-host audit anchor, alerting, and backup drills as not built/not tested. `SafetyController.alert` records logs/audit events and calls optional in-process handlers, but does not deliver email/webhooks. `docs/BACKUP_RECOVERY.md` states backup is operator-owned and requires restore testing.
- **Impact:** A compromised account, database owner, or silent operational failure could evade controls or go undetected; recoverability is not demonstrated.
- **Recommended fix:** Require MFA for sensitive roles, anchor audit heads off-host, configure and test actionable alerts, and run documented backup/restore plus reconciliation drills before live eligibility.

### 6. Test-module status text is stale

- **Severity:** Low.
- **Status:** Confirmed documentation defect.
- **Evidence:** The `test_api_testclient.py` module docstring still says the HTTP tests have “never run” and asks users to treat the first run as a debugging session, despite the current required-HTTP suite passing and the previous audit recording real HTTP results.
- **Impact:** Readers may incorrectly conclude the HTTP suite is skipped or unverified.
- **Recommended fix:** Remove the stale one-time status and link the current verification report. The test behavior and assertions are unchanged.

### 7. Backend invariant test assumed the repository-root image layout (fixed)

- **Severity:** Low.
- **Status:** **CONFIRMED and fixed** in `backend/tests/test_phase45_invariants.py`.
- **Evidence:** The first Docker PostgreSQL suite run failed because `parents[2]` resolved the package path to `/backend/dewako` inside the backend-only image. The test now derives `PKG` from the backend directory containing the test.
- **Reproduction:** `docker compose -f docker-compose.test.yml run --build --rm tests`; first run had 1 failure, the corrected run passed that test and the full suite.
- **Impact:** The safety invariant was not evaluated in the Docker image, even though it passed on the host.
- **Fix:** Resolve the package root independently of whether tests run from a full repository or the backend-only image. The assertion itself is unchanged.

## Blocked and not-run items

- **PASS with scope skips:** Current PostgreSQL 16 Compose pytest/HTTP suite: 403 passed, 4 skipped, 0 failed. UI/repository-wide checks are not present in the backend-only image.
- **PASS:** Default SQLite Docker backend and PostgreSQL deployment profile; PostgreSQL migrations and backend health verified.
- **BLOCKED:** Complete all-services Compose launch on the configured prototype host port 8080; Windows refused the bind. The prototype alone returned HTTP 200 on port 18080.
- **NOT RUN:** Hosted GitHub Actions/CI.
- **NOT RUN:** PostgreSQL process restart/connection-interruption drill against the application.
- **NOT RUN:** Real or testnet exchange authentication, market data, account snapshot, order submission, cancellation, fills, or reconciliation. Production trading endpoints were deliberately not contacted.
- **NOT RUN:** External backup/restore drill, off-host alert delivery, MFA exercise, power-loss/kernel/disk failure, or real database/network interruption during a live exchange order.
- **NOT RUN:** A dedicated delayed-exchange-response order scenario and an end-to-end rate-limited order-write scenario.
- **UNVERIFIED:** Exchange-side client-order-id idempotency and actual exchange behavior for all adapter contracts.

## Changed files

No application Python behavior or test assertion was weakened. This audit changed:

| File | Reason |
|---|---|
| `PHASE4_5_VERIFICATION_REPORT.md` | Updated findings, regression evidence, current command results, remaining limits, and gate verdict. |
| `PHASE5_READINESS_MATRIX.md` | Updated Phase 5 A/B/C gate and stage-by-stage statuses. |
| `backend/tests/test_api_testclient.py` | Updated only its stale module docstring to stop claiming the HTTP tests have never run; test functions/assertions are unchanged. |
| `backend/tests/test_phase45_invariants.py` | Fixed backend package path resolution so the unchanged safety invariant runs inside the backend-only Docker image. |
| `docker-compose.yml` | Added PostgreSQL health checking and clarified default SQLite vs PostgreSQL deployment usage. |
| `docker-compose.postgres.yml` | Added a PostgreSQL-profile override that starts the backend only after PostgreSQL reports healthy. |
| `docs/TESTING.md` | Documents current Docker/PostgreSQL results and commands, quote guard configuration and units, illustrative-only settings, and top-of-book limitations. |
| `frontend/browser_tests/shots/activity_live.png` | Refreshed automatically by the real-app browser suite. |
| `frontend/browser_tests/shots/activity_unknown_order.png` | Refreshed automatically by the real-app browser suite. |
| `frontend/browser_tests/shots/import_accepted.png` | Refreshed automatically by the real-app browser suite. |
| `frontend/browser_tests/shots/import_rejected.png` | Refreshed automatically by the real-app browser suite. |
| `backend/dewako/market_data/freshness.py` | New timestamp/sequence/closure/age validator for candles, used by state building, bot ticks, and live-gate freshness. |
| `backend/dewako/market_data/state_builder.py` | Rejects untrusted or stale candles before building strategy input. |
| `backend/dewako/api/main.py` | Passes the requested timeframe into market-state validation and returns service errors for invalid/stale data. |
| `backend/dewako/runtime/bot_runner.py` | Revalidates provider output before signal, risk decision, or intent creation; uses an injectable clock. |
| `backend/dewako/runtime/application.py` | Validates actual returned candles before updating live market-data freshness; loads explicit quote guard config. |
| `backend/dewako/safety/controller.py` | Stores actual candle timestamp/timeframe and rechecks them in the live gate rather than freshness of fetch time. |
| `backend/dewako/strategies/signal_engine.py` | Prevents a strategy/plugin from substituting a favorable entry reference unrelated to the newest validated close. |
| `backend/dewako/execution/validation.py` | Adds validated quote policy and fail-closed top-of-book spread, freshness, and price-deviation checks. |
| `backend/dewako/execution/engine.py` | Requires quote config; validates quote before intent and revalidates immediately before the adapter submission. |
| `backend/dewako/exchanges/base.py` | Documents the normalized bid/ask/timestamp quote contract. |
| `backend/dewako/paper_trading/paper_exchange.py` | Exposes a timestamped simulated bid/ask consistent with its slippage-adjusted fill price, so the guard checks the paper adapter's actual modeled executable side. |
| `backend/tests/test_market_execution_guards.py` | Adds candle/quote negative cases, no-intent/no-submit assertions, movement, config, plugin-reference, and BUY/SELL paper quote-to-fill consistency regressions. |
| `backend/tests/hardening_helpers.py`, `backend/tests/sim_worker.py`, `backend/tests/test_adapters_and_fills.py`, `backend/tests/test_hardening_orders.py`, `backend/tests/test_hardening_platform.py`, `backend/tests/test_lifecycle_failure.py`, `backend/tests/test_hardening_safety.py`, `backend/tests/test_live_activation.py`, `backend/tests/test_risk_reservations.py` | Configure deterministic quote policies and quotes for existing order, concurrency, crash, and safety regressions. No safety assertion was weakened. |
| `backend/tests/test_market_data.py`, `backend/tests/test_audit_lease_freshness.py` | Use valid deterministic candle times and prove the safety gate tracks candle data rather than request completion. |
| `.env.example` | Documents optional freshness settings and leaves execution thresholds explicitly unset by default. |

## Prioritized remediation

1. Keep testnet/live disabled. Before considering either, add application-level PostgreSQL interruption/restart coverage for order update, incident creation, and recovery, and run the complete repository-layout CI checks.
2. Keep paper/simulation work isolated while developing a testnet-only adapter and venue-specific quote validation; verify it only against an official sandbox with operator-supplied test credentials. Depth/market-impact controls remain unimplemented.
3. Complete MFA, off-host audit anchoring, actionable external alerting, backup/restore drills, and formal operator authorization before considering any live eligibility.


---
## Phase 5.1 update (2026-10-28): two code-review findings

Base: `dewako_phase4_5_corrected.zip` (SHA-256 `867f8d41a8edd6b93b3065717bf38784fccf53754aa3e11ebe2c3bfd98209fd8`). Not a Git repo, so no commit id. Everything below ran against fake/paper adapters only; no exchange, testnet or credential was used; live and testnet stay locked.

### 5. Risk accounting used the strategy reference price, not the executable price — CONFIRMED and fixed
- **Severity:** Medium (risk limits could be passed at a price the order could not actually get).
- **Previous behavior:** `ExecutionEngine.execute` discarded the `QuoteCheck` returned by `validate_executable_quote`. Minimum notional, available balance, position/exposure limits and the reservation were all computed at `sig.entry_price` (the reference), while the order would fill near the ask (BUY) or bid (SELL).
- **Change:** `validation.conservative_risk_price` (BUY = max(reference, ask); SELL = min(reference, bid); missing/non-finite/non-positive inputs raise `QuoteGuardError`) is the single price basis for `validate_order` (min notional, balance incl. fee), `check_limits` (position %, exposure %, balance after reservations) and the reservation. The order row keeps the approved reference (the deviation guard anchors on it). The quote is still revalidated immediately before submission, and a BUY whose ask has since risen above the reserved unit price is **not sent** (order FAILED, reservation RELEASED).
- **Regression tests** (`backend/tests/test_executable_price_accounting.py`): reference passes but executable ask violates the 10% position cap -> BLOCKED (the test first proves `check_limits` at the reference returns no violation); same for available balance; reservation and exposure state equal qty x ask; SELL rejected at the bid below min notional though the reference passes (plus a positive control); ask rising before submission -> not sent, reservation released; ask falling -> still sent; NaN ask on revalidation -> blocked; missing ask -> nothing created; helper rejects None/0/negative/NaN/inf/bool/wrong-type/invalid side; paper adapter: reservation, position price and cash movement all agree with the modelled fill.
- **Check that the tests bite:** temporarily reserving at the reference again made 11 of the 17 tests fail (including the pre-send guard blocking every order, which shows mixed bases are caught); engine restored afterwards.
- **Consequences, disclosed:** (a) `test_paper_restart_keeps_cash_positions_and_orders` used a size exactly ON the 10% cap at the reference (1000.00); at the paper ask it is 1000.50 and is now refused. Quantity changed 10.0 -> 9.9; the limit was not changed. (b) `test_bot_tick_end_to_end_paper_and_is_idempotent` failed for the same reason (RiskEngine sizes at the reference up to exactly 10%; refused at 1000.47). Its bot now sizes with `RiskLimits(max_position_pct=9.5)`; the 10% backstop is unchanged. **Operational note:** any bot whose RiskEngine sizing cap equals the backstop cap will have cap-sized entries refused once slippage lifts them above it; configure sizing headroom. Sizing against the executable quote is NOT IMPLEMENTED.
- **Remaining limits:** top-of-book only; the pre-send guard compares the BUY ask to the reserved price and, since Phase 5.2, re-checks a SELL's exchange rules (minimum notional, lot size) at the send-time bid; fees are in the balance check but not in the reservation (unchanged); no real venue quote contract verified.

### 6. Duplicate `ORDER_SUBMITTED` audit event — NOT CONFIRMED
- `engine.py` has exactly one `audit.record("ORDER_SUBMITTED")` call (after the exchange acknowledgement). Executing a signal under nine adapter outcome modes (ok, partial, open, timeout-accepted, timeout-absent with and without lookup, reject, HTTP 500, malformed) gave: ok/partial/open -> exactly 1 audit event, 1 `SUBMIT_ATTEMPT`, 1 `EXCHANGE_ACK`, 1 adapter call; every other mode -> 0 audit events (nothing was acknowledged), 1 adapter call. Paper path: 1.
- Nothing was removed. Regression tests now pin it, kept separate from submission counts: one audit event per successful submission (live fake modes and paper); a replayed signal is DUPLICATE with one order, one adapter call and unchanged audit counts; an ambiguous (timed-out) submission is sent once, records no ORDER_SUBMITTED and one ORDER_UNKNOWN; the activity feed shows one `ORDER_EXCHANGE_ACK` and no audit-sourced `ORDER_SUBMITTED`.
- If the reviewer saw two rows, the likely source is the same submission appearing in both the audit log and `order_events` (the activity view already excludes the audit copy). If a concrete reproduction exists, send it; none was found here.

### Phase 5.1 verification (actual commands and results)
Environment: Linux sandbox, Python 3.13.16, **no pytest/FastAPI/httpx** (package index unreachable), custom runner `scripts/run_tests_standalone.py` with the repo's `scripts/pytest_shim` (now supports `parametrize`, `raises(match=)`, `approx`, `monkeypatch`); local PostgreSQL 16.15.
| Check | Result | Log |
|---|---|---|
| Baseline before changes, SQLite | 374 passed, 0 failed, 33 skipped | `docs/evidence/phase5_0_baseline_sqlite.log` |
| After changes, first run (SQLite / PG16) | 390 passed, **1 failed**, 33 skipped / 393 passed, **1 failed**, 30 skipped (the bot-tick test above) | `phase5_1_*_first_run_1_failure.log` |
| Final SQLite full suite | **391 passed, 0 failed, 33 skipped**, 61 s | `phase5_1_sqlite_final.log` |
| Final PostgreSQL 16 full suite | **394 passed, 0 failed, 30 skipped**, 106 s | `phase5_1_pg16_final.log` |
| Affected: execution, quote guard, risk reservations, lifecycle, audit | all in the full runs above (market_execution_guards 23, executable_price_accounting 17, hardening_orders 15, risk_reservations 13, lifecycle_failure, audit_lease_freshness) | same |
| Ruff `--select E9,F63,F7,F82` (the CI rule set) on `backend/dewako backend/tests scripts` | PASS, ruff 0.16.8 (was NOT RUN in the earlier report because it was not installed) | `phase5_1_ruff.log` |
| compileall, secret scan | OK / 0 findings | — |
Skips: 30 HTTP tests need FastAPI/httpx/uvicorn (not installed here; they were reported passing in the corrected package's own environment, but were **NOT RUN here**) plus 3 PostgreSQL-only cases on SQLite. NOT RUN here: real FastAPI HTTP/SSE tests, the real-app browser suite, Node tests, Docker, Windows, hosted CI, real pytest.

### Files changed in 5.1
`backend/dewako/execution/validation.py` (new `conservative_risk_price`), `backend/dewako/execution/engine.py` (use the QuoteCheck; one basis; pre-send reserved-price guard), `backend/tests/test_executable_price_accounting.py` (new, 17 tests), `backend/tests/test_hardening_safety.py` and `backend/tests/test_hardening_platform.py` (the two disclosed fixture adjustments), `scripts/run_tests_standalone.py` and `scripts/pytest_shim/pytest.py` (runner compatibility only), `docs/RISK_ENGINE.md`, `docs/evidence/phase5_*`, this report, `PHASE5_READINESS_MATRIX.md`.

## Phase 5.2 update
SELL send-time revalidation implemented (see PHASE5_READINESS_MATRIX.md, "Phase 5.2 verification results", for commands, counts and logs). Risk sizing deferred and kept OPEN. The prototype `index.html` gained a doctype, `lang` and viewport meta; checked in headless Chromium against the stand-in server only, not the real FastAPI app.
