# Dewako AI Trading Engine — Phase 5.2 Final Verification Report

**Assessment date:** 2026-10-29  
**Workspace:** `C:\AI_Trading\dewako_phase5_2_reviewed`  
**Scope:** Windows verification, code-level execution/security audit, deterministic fake/paper tests, isolated PostgreSQL 16 integration, and dashboard browser tests. No testnet or production exchange was contacted.

## 1. Executive summary

**Paper/simulation development: PASS WITH DOCUMENTED LIMITATIONS.** The current full SQLite/backend suite passed **445 tests**, with **3 PostgreSQL-only tests skipped** and no failures. The isolated PostgreSQL 16 suite passed **444 tests**, with **4 conditional skips** and no failures. Frontend Node tests passed **9/9**. The backend Docker image built successfully. Ruff's full-repository check did not pass (979 findings); pip-audit found no known vulnerabilities in the declared runtime requirements.

Four safety defects were reproduced and fixed:

1. Malformed fill responses could be accepted and corrupt fill/position accounting.
2. Invalid exchange quantity/filter/balance metadata could allow unsafe validation results.
3. Binary floating-point tolerance could round a just-under-lot-boundary quantity up above the requested quantity.
4. Adapter error text could expose credential-like values in stored order/audit details.

Regression coverage was added. Current SQLite, PostgreSQL, Python 3.14 crash, frontend, Docker, and compilation results are recorded below. A real-exchange behavior remains unverified: every exchange test used local fakes or no exchange at all.

**Testnet: NOT READY. Live trading: NOT READY — LOCKED.** The evidence in this report does not authorize either.

## 2. Repository and working-tree baseline

- The project root and supplied [Windows acceptance report](./PHASE5_2_WINDOWS_ACCEPTANCE_TEST_REPORT.md) were inspected before editing. That report documents the pre-edit baseline; this final report does not replace its historical evidence.
- This workspace is an extracted source folder and has no `.git` directory. `git status --short --branch` returned exit code 128 (“not a git repository”). Branch, commit, and Git diff are unavailable; changes were reviewed by inspecting the affected files.
- The previous Windows acceptance report recorded Python 3.13.15 with global Pydantic 2.13.5, 426 backend passes/3 PostgreSQL skips, 9 Node passes, and PostgreSQL/Docker as blocked. These results are **historical** and are not combined with this report's current counts.
- This run created/used `backend\.venv` and `backend\.venv314`; neither system Python nor the unidentified local PostgreSQL listener on port 5432 was used for the integration suite.
- No databases were deleted or reset. PostgreSQL integration used only the repository's disposable Compose test service (tmpfs); it was shut down and removed after testing.

## 3. Environment and dependencies

| Item | Current observation |
|---|---|
| OS | Windows 11 Pro, build 26200 |
| System Python / pip | Python 3.13.15; pip 26.2.1 |
| Python 3.13 project venv | FastAPI 0.162.0; Pydantic 2.14.1; pytest 9.1.1; httpx 0.28.1; Uvicorn 0.54.0 |
| Python 3.14 crash-test venv | Python 3.14.7; same reported FastAPI/Pydantic/pytest/httpx/Uvicorn versions |
| Other tools/packages | cryptography 50.0.2; pytest-timeout 2.3.1; Ruff 0.6.9; pip-audit 2.7.3; Playwright 1.49.1 |
| Dependency health | `pip check` passed in both virtual environments. Pydantic 2.14.1 satisfies `requirements.txt` (`>=2.14.1`); the earlier global 2.13.5 discrepancy is resolved in the project venv. |
| SQLAlchemy | Not installed and not declared; PostgreSQL uses the project's standard-library `pgwire.py` implementation. |
| Node / npm | Node v24.19.0; npm 11.17.0. No frontend `package.json`; tests use Node's built-in runner. |
| Docker / Compose | Docker 29.9.2; Compose v5.6.1. Docker daemon was available for builds and isolated PostgreSQL testing. |
| Browser | Installed Google Chrome 156.0.8078.48 was used in the acceptance run; Playwright Chromium was used for the real-FastAPI dashboard suite. |
| Git | Unavailable in this extracted workspace. |
| Credentials/configuration | No production or testnet exchange keys were configured or used. The browser test generated disposable local credentials and database state. |

Python 3.12 was not installed as a Windows interpreter; the repository's Docker image is based on `python:3.12-slim` and built successfully. The README's Windows example prefers 3.12 while `pyproject.toml` permits Python `>=3.11`; this run therefore validates Python 3.13 and the dedicated Python 3.14 crash path locally, plus the container's Python 3.12 dependency installation.

[`../backend/requirements.txt`](../backend/requirements.txt) contains minimum versions rather than a complete runtime lockfile. [`../backend/constraints-test.txt`](../backend/constraints-test.txt) pins the test environment's resolved packages; there is no comprehensive lockfile for all platforms. A fresh project venv was populated using the documented requirements/constraints/test-requirements method and passed `pip check`. This demonstrates a clean install in this environment, not byte-for-byte reproducibility on every platform.

## 4. Exact commands executed

Commands below ran from the project root or backend as indicated. Exit codes are recorded; test totals are not merged across environments.

| Check | Command | Status | Exit |
|---|---|---|---:|
| Fresh Python 3.13 venv dependencies | From `backend`: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints-test.txt -r requirements-test.txt` | PASS | 0 |
| SQLite/backend suite, HTTP tests required | From `backend`: `$env:DEWAKO_REQUIRE_HTTP_TESTS='1'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --timeout=300 tests` | PASS — 445 passed, 3 skipped | 0 |
| PostgreSQL 16 integration suite | From root: `docker compose -f docker-compose.test.yml run --build --rm tests` | PASS — 444 passed, 4 skipped | 0 |
| PostgreSQL skip-reason confirmation | From root: `docker compose -f docker-compose.test.yml run --build --rm tests python -m pytest -q -p no:cacheprovider -rs .` | PASS — confirms packaging-layout skips | 0 |
| Python 3.14 crash/recovery repeat | From `backend\tests`: `$env:PYTHONPATH='..'; ..\.venv314\Scripts\python.exe -m pytest -q -p no:cacheprovider test_process_cleanup.py test_crash_matrix.py test_crash_and_recon.py -k 'not pg_'` (three consecutive runs) | PASS — 29 passed, 3 deselected each run | 0 each |
| Frontend tests | From root: `node --test frontend/tests/*.test.js` | PASS — 9 passed | 0 |
| Python compilation | From `backend`: `.\.venv\Scripts\python.exe -m compileall -q dewako tests` | PASS | 0 |
| Dependency consistency | In each venv: `python -m pip check` | PASS | 0 |
| Ruff, entire backend | From `backend`: `.\.venv\Scripts\ruff.exe check dewako tests` | FAIL — 979 findings | 1 |
| Ruff, changed exchange validation module | From `backend`: `.\.venv\Scripts\ruff.exe check dewako\execution\validation.py` | PASS | 0 |
| Dependency audit | From `backend`: `.\.venv\Scripts\pip-audit.exe -r requirements.txt` | PASS — no known vulnerabilities | 0 |
| Docker backend image | From root: `docker build --no-cache -t dewako-backend-verification:local ./backend` | PASS | 0 |
| Real FastAPI Playwright dashboard | From root: `$env:DEWAKO_E2E_SERVER='real'; .\backend\.venv\Scripts\python.exe frontend\browser_tests\test_dashboard_browser.py` | PASS — 15 passed | 0 |
| Source secret-pattern review | PowerShell scan over 154 application, test, frontend, and script source files (excluding virtual environments) | PASS AFTER REVIEW — 3 synthetic test-fixture matches; no real credential identified | 1 (pattern matches) |
| Production Compose configuration | `docker compose --env-file .env.example --profile postgres-port -f docker-compose.yml -f docker-compose.postgres.yml config --quiet` | BLOCKED — required `.env` absent; no deployment services started | 1 |
| Git status | `git -C C:\AI_Trading\dewako_phase5_2_reviewed status --short --branch` | BLOCKED — supplied folder is not a Git repository | 128 |

One supplemental attempt to request PostgreSQL skip reasons added `--timeout=300` to the container's pytest command even though that image does not install `pytest-timeout`; pytest exited 4 before running tests. It was not counted as a test run. The normal repository Compose command then ran successfully.

## 5. Backend test results

- **SQLite, current final source:** `445 passed, 3 skipped`, 0 failures, 0 errors; exit 0; 131.83 seconds. `DEWAKO_REQUIRE_HTTP_TESTS=1` was set.
- The three SQLite skips are explicitly PostgreSQL-only tests: PostgreSQL identity, two-independent-connection intent race, and bounded held-lock behavior. Their skip message requires `DEWAKO_TEST_DB=postgres`.
- **PostgreSQL 16, current final source:** `444 passed, 4 skipped`, 0 failures; exit 0; 92.01 seconds. The actual service was the isolated Compose `postgres:16` test database. Skip reasons: one UI test requires dashboard files absent from the backend-only image; three tests require the full repository layout, also absent from that image. These are packaging-layout skips, not failed tests.
- One `StarletteDeprecationWarning` about `httpx`/`starlette.testclient` appeared during each full suite; it did not fail tests. The warning should be addressed when the project chooses a compatible test-client dependency.
- HTTP tests were required in the SQLite suite; missing HTTP dependencies would not silently convert that run into a pass.
- The production-database guard tests passed in the full suite: production configuration requiring PostgreSQL does not silently fall back to SQLite.

## 6. Frontend Node results

`node --test frontend/tests/*.test.js`: **9 passed, 0 failed, 0 skipped**, exit 0. Coverage includes activity ingestion/deduplication, bounded buffering, view states, SSE parsing, escaped rendering, filters, and strategy-package client behavior.

## 7. Browser results

- The earlier acceptance run exercised installed Google Chrome 156 at `http://127.0.0.1:8000/ui/`, all 17 visible navigation items, login form validation, and 1920×1080, 1366×768, and 390×844 layouts. It recorded no critical console exceptions or required asset failures. The served app returned 200 for `/health`, `/ui/`, and required CSS/JavaScript, 401 for unauthenticated protected routes, and 404 for an invalid API route. That Chrome run is documented in the [Windows acceptance report](./PHASE5_2_WINDOWS_ACCEPTANCE_TEST_REPORT.md).
- The Playwright suite was run in `DEWAKO_E2E_SERVER=real` mode against the actual FastAPI app under Uvicorn, with a temporary SQLite database, generated test-only admin/viewer credentials, fake exchange data, and no exchange keys. The final rerun after the last backend change passed **15 tests, 0 failures**. It covered authenticated real activity, unresolved-order display, viewer denial, strategy-package import/validation, CSP/security headers, login/logout, and simulated-page labeling. The expected unresolved-order alert remained visible; it was not treated as a successful order. No temporary test DB or credentials were retained.
- Four screenshots were emitted by that suite: [activity_live.png](../frontend/browser_tests/shots/activity_live.png), [activity_unknown_order.png](../frontend/browser_tests/shots/activity_unknown_order.png), [import_accepted.png](../frontend/browser_tests/shots/import_accepted.png), and [import_rejected.png](../frontend/browser_tests/shots/import_rejected.png). All account names, packages, and order events are synthetic fake-test data. In particular, the `LIVE`-labelled event in the first screenshot was produced only by a fake adapter under the demo harness; no real/testnet order was sent, and the dashboard banner states paper/no real money with live trading locked.
- The browser tests do not establish exchange connectivity or live readiness. No production/testnet account was used. UI states and simulated records are not presented as verified real-exchange results.
- Responsive viewport and actual Chrome coverage are from the acceptance browser run; the Playwright run uses bundled Chromium, not the standalone Chrome binary.

## 8. PostgreSQL and Docker results

- PostgreSQL 16 was launched only through `docker-compose.test.yml`. The database uses tmpfs and repository-supplied test-only credentials; no unidentified host PostgreSQL service was contacted.
- The full PostgreSQL run passed **444 tests**, skipped **4**, and exited 0. It exercised fresh schema initialization/migrations, transactions, constraints, concurrent intent creation, and fake-exchange recovery paths on a real PostgreSQL service.
- The Compose test service ran as UID 10001. `docker compose -f docker-compose.test.yml down --remove-orphans` stopped and removed the PostgreSQL service and network. A subsequent `docker ps` showed no remaining containers.
- `docker build --no-cache -t dewako-backend-verification:local ./backend` succeeded. The resulting image was present (300,709,614 bytes); the context was 254.71 kB. The Dockerfile creates and runs as non-root UID 10001.
- The Dockerfile has no container `HEALTHCHECK` instruction. The application health route was verified during the acceptance run, but container-orchestrator health behavior was not verified.
- Production Compose deployment was **not run**. Config validation requiring the absent `.env` file failed without starting services; no `.env` was created. Docker base tags (`python:3.12-slim`, `postgres:16`) are floating tags, so digest pinning remains an improvement.
- PostgreSQL integration does not prove real exchange behavior. Application-level loss of a live PostgreSQL connection while an actual venue order is in flight was not tested; simulated fault and crash tests use fakes.

## 9. Risk sizing and executable-price findings

- `RiskEngine` sizing starts from the approved signal/reference price, not a quote-driven quantity reduction. Quote-based resizing remains **NOT IMPLEMENTED**.
- Independently, the execution engine reads and validates the executable top-of-book quote, using ask for BUY and bid for SELL. Conservative accounting uses the less favorable of the approved reference and executable price (higher BUY/lower SELL). BUY orders whose current ask exceeds the unit price reserved for risk are refused at send time; the failure transition releases the reservation.
- BUY exchange validation checks available quote balance against notional including the configured/default fee. The application does not implement depth-based impact estimation and does not claim one. Operators must retain sizing headroom because a moved quote can cause a safe rejection rather than automatic resizing.
- Instrument quantity steps/precision, min/max quantity, tick size for non-market orders, minimum notional, fee values, available balances, and prices are validated. Invalid, missing, non-finite, zero/negative, or malformed values fail closed with validated quantity left at zero.
- Quantity rounding now uses `Decimal` with `ROUND_DOWN`; no epsilon is added before flooring. The requested quantity is never increased to reach a lot boundary.
- The existing final execution-side checks remain independent of strategy sizing and are not weakened by these changes.

## 10. SELL send-time validation

SELL uses the executable bid, then the conservative SELL price. Immediately before send, `_sell_rules_problem` re-fetches market rules and balances and validates the actual persisted submission quantity against current minimum notional and lot-step rules. A failed validation records a failed pre-send outcome and audit event, does not call `create_order`, and the repeated request is a duplicate rather than a retry. Targeted tests include SELL minimum-notional/quantity movement and no-send behavior; these also ran in the full SQLite and PostgreSQL suites.

The order remains a MARKET order: no claim is made that the top-of-book bid protects against depth, market impact, or realized slippage.

## 11. Order lifecycle, idempotency, and crash recovery

- Intent uniqueness and local concurrency protections are exercised in SQLite and PostgreSQL. Repeating the same request returns a duplicate; separate PostgreSQL connections race the same signal through the unique intent constraint.
- The engine persists `SUBMITTING` before adapter submission. Orders left in `CREATED` after the configured grace period are failed as never sent; `SUBMITTING`, `UNKNOWN`, and `RECONCILING` are looked up/reconciled, not resent.
- Fake-adapter tests cover accepted/rejected orders, partial/full fills, timeout then accepted/absent outcomes, malformed/wrong-ID responses, cancellation and reconciliation, duplicate/out-of-order updates, persistence/recovery boundaries, and no-blind-retry behavior.
- `UNKNOWN`/`RECONCILING` remain unresolved absent adequate evidence. A failed/inconclusive lookup does not become a confirmed trade. Exchange-side idempotency semantics remain **UNVERIFIED**.
- Python 3.14.7 Windows crash suite: **29 passed, 3 deselected** on each of **three consecutive runs**, exit 0 each. The deselected tests are the PostgreSQL-only cases in this SQLite crash command. Separate PostgreSQL suite passed as above.
- Real network timeouts, venue-specific cancellation races, power loss, and live exchange restart recovery remain unverified.

## 12. Market-data freshness and slippage

- Candle parsing, closed-bar freshness, stale/out-of-order/gap checks, and fail-closed signal paths have local tests using deterministic transports. The current backend suite passed these tests.
- Immediately-before-send quote checks validate bid/ask positivity, crossed markets, timestamp timezone/age/future skew, spread, price deviation, and executable side. They protect against a stale or invalid top-of-book quote, not the entire order book.
- Implemented: **top-of-book executable-price validation** and configured spread/deviation/age checks. Configured slippage cap is not independent of those top-of-book checks.
- Not implemented/verified: order-book depth sufficiency, market-impact estimation, venue-side slippage guarantees, or actual realized-slippage monitoring. Safe design recommendation: derive a maximum permitted price/impact from fresh depth levels and target quantity, revalidate immediately before send, and fail closed when depth or timestamp quality is insufficient; first implement and test entirely against deterministic snapshots/fakes.

## 13. Security findings

**PASS:** password hashing (scrypt), opaque hashed session tokens, expiry/revocation, generic login errors, service-level lockout, role checks, audit logging/redaction, live mode lock, no-withdraw key policy, and server-side authorization have tests. Final full suite required HTTP tests. The browser test used only generated temporary credentials.

**Observed limitations / UNVERIFIED:**

- MFA and password reset are **NOT IMPLEMENTED**.
- Login/request throttling is in-memory and per process, not a distributed limiter.
- Encryption keys are environment-held; no KMS/HSM integration. Anyone with both host environment and database access can decrypt stored credentials.
- Audit chain is local to the database; no off-host anchoring, and a privileged database owner can rewrite the chain.
- No external alert delivery; security/recovery notices are local audit/log events.
- CSRF posture needs deployment review if authentication is ever moved to cookies; current tokens are bearer tokens and require TLS at a deployment proxy.
- CORS is restricted to configured origins and same-origin dashboard routes; deployment origin/trusted-host/TLS policy was not exercised in a production reverse-proxy environment.
- Frontend error display tests verify stack traces are not surfaced, but production browser-storage and authenticated-session-expiry behaviors were not separately penetration-tested.
- `pip-audit -r requirements.txt`: no known vulnerabilities in the declared runtime dependency set at audit time. This does not cover every platform-resolved transitive dependency or container base-image CVEs.
- Source pattern scan had three matches in test files containing synthetic fixture strings. The locations were reviewed; no actual credential was identified. Virtual environments were excluded. No credential values are reproduced in this report.

No security check justified enabling testnet/live or weakening a control.

## 14. Exchange-by-exchange capability matrix

All classifications below are **NOT VERIFIED against an actual exchange**. Test doubles are not venue evidence.

### Binance

| Capability | Classification / evidence |
|---|---|
| Public market data | PARTIALLY IMPLEMENTED; public REST provider/parser exists, no real call verified |
| Signed requests / permission check | IMPLEMENTED AND TESTED WITH A FAKE; permission endpoint uses HMAC signing, never called against Binance |
| Account snapshots | NOT IMPLEMENTED |
| Instrument metadata / precision / minimum rules | NOT IMPLEMENTED by the trading adapter |
| Order submission | NOT IMPLEMENTED |
| Order status / client-ID lookup | NOT IMPLEMENTED |
| Cancellation | NOT IMPLEMENTED |
| Fills / partial fills | NOT IMPLEMENTED |
| Open positions / orders | NOT IMPLEMENTED |
| Rate limits / retry behavior | NOT VERIFIED; no venue request lifecycle |
| Idempotency / timeout reconciliation | NOT IMPLEMENTED |
| Testnet | NOT READY; `testnet_available` is metadata only, no testnet order environment is enabled |
| Actual exchange verification | NOT RUN |

### Bybit

| Capability | Classification / evidence |
|---|---|
| Public market data | PARTIALLY IMPLEMENTED; provider/parser exists, no real call verified |
| Signed requests / permission check | STUB / NOT IMPLEMENTED |
| Account snapshots | STUB / NOT IMPLEMENTED |
| Instrument metadata / precision / minimum rules | STUB / NOT IMPLEMENTED |
| Order submission | STUB / NOT IMPLEMENTED |
| Order status / client-ID lookup | STUB / NOT IMPLEMENTED |
| Cancellation | STUB / NOT IMPLEMENTED |
| Fills / partial fills | STUB / NOT IMPLEMENTED |
| Open positions / orders | STUB / NOT IMPLEMENTED |
| Rate limits / retry behavior | NOT VERIFIED |
| Idempotency / timeout reconciliation | NOT IMPLEMENTED |
| Testnet | NOT READY; no adapter lifecycle or enabled order environment |
| Actual exchange verification | NOT RUN |

### OKX

| Capability | Classification / evidence |
|---|---|
| Public market data | PARTIALLY IMPLEMENTED; provider/parser exists, no real call verified |
| Signed requests / permission check | STUB / NOT IMPLEMENTED |
| Account snapshots | STUB / NOT IMPLEMENTED |
| Instrument metadata / precision / minimum rules | STUB / NOT IMPLEMENTED |
| Order submission | STUB / NOT IMPLEMENTED |
| Order status / client-ID lookup | STUB / NOT IMPLEMENTED |
| Cancellation | STUB / NOT IMPLEMENTED |
| Fills / partial fills | STUB / NOT IMPLEMENTED |
| Open positions / orders | STUB / NOT IMPLEMENTED |
| Rate limits / retry behavior | NOT VERIFIED |
| Idempotency / timeout reconciliation | NOT IMPLEMENTED |
| Testnet | NOT READY; no adapter lifecycle or enabled order environment |
| Actual exchange verification | NOT RUN |

The engine capability gate refuses adapters that do not declare required order-submission, account-snapshot, instrument-metadata, and lookup features. The verified LIVE and TESTNET allowlists remain empty.

## 15. CI, packaging, and deployment

- [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) was inspected and adjusted: Windows Python 3.14 crash CI installs runtime requirements plus `pytest-timeout` rather than the Playwright-inclusive test set that caused a `greenlet` build failure. The SQLite job still installs the complete test requirements.
- The Windows crash suite's local Python 3.14 runs passed three times. **Hosted GitHub Actions was not run or inspected**, and no CI pass is claimed.
- Docker image build passed. It runs non-root; runtime health checks and production startup through the production Compose stack were not exercised.
- Test Compose requires PostgreSQL 16 and ran successfully. Production Compose config validation was blocked by the absent `.env`; deployment services were not started.
- Dependency requirements are minimum-based and lack a complete lockfile. Docker base images are tagged rather than digest-pinned. These are supply-chain reproducibility gaps.
- Dependency resolution differs across environments: the Windows venv used test-constrained pytest 9.1.1, while the unpinned Docker runtime requirements resolved pytest 9.2.0. Both satisfy the declared minimum; pinning/building from one reviewed lock/constraints set would improve consistency.
- `ruff check dewako tests` failed with **979 findings**. The modified `validation.py` passed targeted Ruff. Full-repository findings include existing broad formatting/import/test-fixture issues; they were not mass-fixed or suppressed in this safety pass.

## 16. Backup, monitoring, and recovery

| Capability | Status | Evidence / limitation |
|---|---|---|
| Database backup/restore automation | NOT IMPLEMENTED | Documentation provides operator-run `pg_dump`/`pg_restore` and SQLite backup guidance; no automated implementation or restore drill |
| Configuration/key recovery | PARTIAL | Documentation describes backing up `.env` and encryption keys separately; no KMS or automated recovery |
| Audit-log recovery | PARTIAL | Hash chain and local verification exist; off-host anchor is not implemented |
| External alert delivery | NOT IMPLEMENTED | No external notification integration |
| Disk-full behavior | UNVERIFIED | No dedicated disk-full drill |
| Database interruption | PARTIAL | Simulated database failures fail closed; no real PostgreSQL connection-loss/restore exercise during order submission |
| Process crash/restart | PASS WITH SCOPE LIMITATION | Deterministic fake-exchange process crash suite passed; no power-loss or real venue evidence |
| Startup health / unresolved-order detection | PASS WITH SCOPE LIMITATION | Health/recovery checks and unresolved order recovery are tested with fakes; actual venue reconciliation not available |
| Stale market data / repeated venue failures | PASS for local guards; actual venues UNVERIFIED | Freshness and failure gates covered using deterministic providers/fakes |

Acceptance plan for missing operations: schedule encrypted, access-controlled DB backups; restore into an isolated empty database; verify migration/schema, audit head, kill-switch state, and unresolved-order recovery; measure RPO/RTO; test disk-full and DB interruption in an isolated environment; and exercise alert delivery with a local sink before enabling any external notification.

## 17. Defects fixed and regression tests added

| Defect | Change | Regression evidence |
|---|---|---|
| Malformed fill response could be recorded as a successful fill; invalid/non-finite prices or absent partial-fill prices could affect accounting | Validate response status, client ID, cumulative quantity, fill price, and FILLED quantity before persistence/application | Added malformed-fill and out-of-order cumulative-fill cases in `test_hardening_orders.py`; full SQLite and PostgreSQL suites passed |
| Invalid/negative/non-finite exchange filters, quantities, balances, fees, or prices could produce unsafe validation output | Reject malformed filters and financial values; on any validation problem leave approved quantity at zero | Added parameterized invalid metadata/balance cases in `test_hardening_safety.py`; full suites passed |
| Epsilon-based float floor could round a quantity just below a lot boundary up to the boundary | Use decimal arithmetic with `ROUND_DOWN`, no positive epsilon | Added `test_quantity_rounding_never_exceeds_the_requested_size`; full suites passed |
| Adapter error text could leak API-key/secret-like text into order and audit detail | Apply existing redaction helper and bound persisted/returned text | Added `test_adapter_error_secrets_are_redacted_from_order_events_and_audit`; full suites passed |
| Docker build context included local venvs/caches | Added `backend\.dockerignore` | Build context reduced; final no-cache Docker image build passed |
| Python 3.14 Windows crash CI installed an unnecessary Playwright/greenlet test dependency | Narrowed only that job's install to runtime requirements plus `pytest-timeout`; SQLite remains on full test requirements | Local Python 3.14 crash suite passed three consecutive times; hosted CI not run |

No failing tests were weakened or removed.

## 18. Outstanding issues and blockers

1. Quote-based quantity sizing is not implemented. Price changes fail closed at the independent execution guard; automatic resizing is absent.
2. Exchange depth, market impact, and realized slippage are not modeled.
3. Binance has only fake-tested signed permission lookup; Bybit/OKX trading adapters are stubs. No venue order lifecycle is verified.
4. Testnet execution is disabled and no testnet adapter is verified.
5. Live execution remains locked; no verified live adapters exist.
6. MFA, KMS/HSM, off-host audit anchoring, external alert delivery, and tested backup/restore are absent.
7. Hosted CI was not executed. Full Ruff remains failing (979 findings).
8. Production Compose could not be validated/run without `.env`; database connection loss during an actual exchange order remains untested.
9. No Git metadata is available in this supplied directory.
10. Dependency lock/image digest pinning and reverse-proxy production security configuration need work.

## 19. Changed-file list

- [engine.py](../backend/dewako/execution/engine.py)
- [validation.py](../backend/dewako/execution/validation.py)
- [test_hardening_orders.py](../backend/tests/test_hardening_orders.py)
- [test_hardening_safety.py](../backend/tests/test_hardening_safety.py)
- [ci.yml](../.github/workflows/ci.yml)
- [.dockerignore](../backend/.dockerignore)
- [.gitignore](../.gitignore) (ignores generated `.venv*` directories)
- [RISK_ENGINE.md](./RISK_ENGINE.md)
- [PHASE5_READINESS_MATRIX.md](../PHASE5_READINESS_MATRIX.md)
- [PHASE5_2_FINAL_VERIFICATION_REPORT.md](./PHASE5_2_FINAL_VERIFICATION_REPORT.md) (this report)
- [activity_live.png](../frontend/browser_tests/shots/activity_live.png)
- [activity_unknown_order.png](../frontend/browser_tests/shots/activity_unknown_order.png)
- [import_accepted.png](../frontend/browser_tests/shots/import_accepted.png)
- [import_rejected.png](../frontend/browser_tests/shots/import_rejected.png)

The application, regression-test, CI, Docker-context, and environment-ignore files contain implementation/support changes. The three Markdown files document findings/readiness; the four PNGs are synthetic browser evidence. No report or screenshot contains credentials or real account data.

## 20. Reproduction instructions

From the project root, in PowerShell:

```powershell
Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed\backend'
$env:DEWAKO_REQUIRE_HTTP_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --timeout=300 tests
```

```powershell
Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed'
docker compose -f docker-compose.test.yml run --build --rm tests
docker compose -f docker-compose.test.yml down --remove-orphans
node --test frontend/tests/*.test.js
```

Python 3.14 Windows crash checks:

```powershell
Set-Location 'C:\AI_Trading\dewako_phase5_2_reviewed\backend\tests'
$env:PYTHONPATH = '..'
..\.venv314\Scripts\python.exe -m pytest -q -p no:cacheprovider test_process_cleanup.py test_crash_matrix.py test_crash_and_recon.py -k 'not pg_'
```

Do not run live/testnet commands or use exchange credentials to reproduce these checks.

## 21. Recommended next steps

1. Implement quote-aware quantity sizing (including fee and precision headroom) while retaining the independent send-time price/risk backstop; test quote movement between signal, reservation, and submission.
2. Build one exchange adapter only after defining a read-only/testnet verification plan, client-ID idempotency, status/fill/cancel/balance reconciliation, and safe fake-transport tests; keep the verified allowlists empty until actual authorized evidence exists.
3. Establish operational readiness: pinned dependency/image artifacts, production Compose validation, tested backups/restores, off-host audit anchoring, alert delivery, and a real PostgreSQL interruption/recovery drill.

## 22. Final readiness decision

| Area | Decision | Basis |
|---|---|---|
| Paper/simulation development | **READY TO CONTINUE WITH DOCUMENTED LIMITATIONS** | SQLite/PostgreSQL test suites, frontend tests, browser suite, and fake/paper execution safeguards passed. Do not equate this with venue or capital readiness. |
| Testnet | **NOT READY** | Testnet order environment is disabled; adapters lack verified submission, lookup, fills, cancellation, account/instrument handling, idempotency, and reconciliation evidence. Explicit future authorization and a controlled testnet plan are required. |
| Live trading | **NOT READY — LOCKED** | No live-verified adapter, real exchange lifecycle, market-depth/slippage safeguards, operational backup/alert/key-management controls, or production verification. No live activation/order execution is authorized by this report. |
