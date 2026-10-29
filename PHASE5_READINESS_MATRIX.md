# Dewako AI Trading Engine — Phase 5 Readiness Matrix

**Assessment date:** 2026-10-29 (Windows Phase 5.2 acceptance run)  
**Overall Phase 5 gate:** **READY TO CONTINUE PAPER/SIMULATION DEVELOPMENT WITH DOCUMENTED LIMITATIONS**  
**Testnet:** **NOT READY**  
**Live trading:** **NOT READY — LIVE MUST REMAIN LOCKED**

## Superseding Phase 5.2 final verification addendum

The detailed Windows final verification is documented in `docs/PHASE5_2_FINAL_VERIFICATION_REPORT.md`. It supersedes the earlier current-run counts and infrastructure limitations below; those original entries remain historical acceptance evidence.

| Gate | Current final evidence |
|---|---|
| Paper/simulation development | **PASS WITH DOCUMENTED LIMITATIONS** — final SQLite suite: 445 passed, 3 PostgreSQL-only skipped; isolated PostgreSQL 16 suite: 444 passed, 4 skipped; Node 9/9; real-app Playwright dashboard suite passed; Docker image build passed. |
| Environment | New Python 3.13 venv resolves the declared Pydantic minimum (2.14.1); `pip check`, compile, and dependency audit passed. Full Ruff still fails with 979 repository findings. |
| Execution hardening | Malformed fills, unsafe exchange metadata/quantity rounding, and adapter-error redaction were fixed with regressions; the independent send-time quote/risk checks remain. Quote-based sizing and market-depth impact protection are still absent. |
| PostgreSQL and Docker | Current disposable PostgreSQL 16 Compose integration passed; Docker image built and test containers were removed after use. Production Compose was not run. |
| Testnet | **NOT READY** — TESTNET order environment remains disabled and there is no verified testnet adapter. |
| Live trading | **NOT READY — LOCKED** — no live-verified adapter or real-exchange lifecycle evidence; operational controls remain outstanding. |

The detailed stage-by-stage matrix below records earlier acceptance evidence and has not been silently rewritten. For any disagreement, use the final verification report for current results and treat the original counts as historical.

## Phase 5 start gate

| Gate | Status | Evidence and conditions |
|---|---|---|
| A. Paper/simulation development | **PASS WITH DOCUMENTED LIMITATIONS** | Current Windows run: 426 backend tests passed, 0 failed, 3 PostgreSQL-only skips; 9 Node tests passed; Google Chrome 156 navigated all 17 items with no console exceptions and passed the responsive-width checks. The login form's empty-submit validation was fixed and verified in Chrome. Pydantic 2.13.5 is below the declared 2.14.1 minimum; Docker/PostgreSQL and authenticated browser workflows remain unverified in this run. Quote-based sizing remains open and safely blocks at the executable-price backstop. |
| B. Exchange testnet readiness | **NOT READY** | TESTNET is excluded from enabled order environments; no verified testnet adapter or testnet execution path is implemented. No testnet calls or credentials were used. |
| C. Future live-trading eligibility | **NOT READY** | All real-exchange submission adapters are absent/stubs; allowlists are empty. Quote guard is top-of-book only, with no depth-based market impact verification. Application-level PostgreSQL interruption/restart coverage, MFA, off-host audit anchoring, external alerting, and restore drills remain outstanding. Live must remain locked. |

## Stage-by-stage matrix

Status definitions are used literally: PASS means the stated test ran and met its criteria. BLOCKED/NOT RUN/NOT IMPLEMENTED/UNVERIFIED are not passes.

| Stage | Area | Status | Evidence / limitation |
|---|---|---|---|
| 1 | Git branch, commit, working-tree diff | **BLOCKED** | Supplied folder has no `.git`; branch, commit, and Git diff cannot be recovered. |
| 1 | Dependencies and supported test environment | **PASS WITH LIMITATION** | Python 3.13.15 satisfies `requires-python >=3.11`; README's Windows example recommends 3.12, which is not installed. Installed Pydantic 2.13.5 is below declared `>=2.14.1`. Tests passed without changing dependencies. No project virtual environment was present. |
| 1 | Full SQLite/backend suite | **PASS** | Current Windows run: 426 passed, 0 failed, 3 PostgreSQL-only skips. `DEWAKO_REQUIRE_HTTP_TESTS=1` was set. |
| 1 | Real FastAPI/HTTP/SSE tests | **PASS** | Included in the current backend suite with HTTP tests required. |
| 1 | Browser UI integration | **PASS WITH LIMITATIONS** | Installed Google Chrome 156.0.8078.48 exercised all 17 navigation items, the live-mode warning, login validation, and 1920×1080, 1366×768, and 390×844 widths. No authenticated account was configured, so actual activity records and strategy-package operations were not accessed. |
| 1 | Frontend Node tests | **PASS** | 9/9. |
| 1 | Secret scan / compilation | **PASS** | Final scan reported 0 findings; compileall exited 0. |
| 1 | Ruff lint | **NOT RUN CURRENTLY** | Ruff is not installed in the active Python environment. Historical Phase 5.1 Ruff evidence remains in `docs/evidence/phase5_1_ruff.log`; it does not cover this Windows run. |
| 1 | Dependency audit | **NOT RUN CURRENTLY** | `pip-audit` is not installed. A prior audit result is historical only. |
| 2 | PostgreSQL current backend pytest/HTTP run | **BLOCKED** | Docker Desktop daemon was unavailable. A `postgres.exe` listener later appeared on port 5432, but its service/database identity and test credentials were not verified; it was not contacted. The earlier 403-passed/4-skipped Docker result remains historical evidence only. |
| 2 | Historical PostgreSQL 16 execution evidence | **UNVERIFIED** | Repository log records 354 passed, 0 failed, 30 skipped using standalone runner; 30 skips include HTTP tests. Historical PG 16.15 restart check is scoped to one immediate restart/reconnect. This is historical partial evidence, not a current full-suite pass. |
| 2 | Production configuration fails closed without PostgreSQL | **PASS** | `test_production_requires_a_postgres_url_and_never_falls_back_to_sqlite`; unreachable PostgreSQL startup test returns degraded/no database and blocks live. Exercised locally using injected invalid DB targets, not a running PG server. |
| 2 | Schema migration, transaction and concurrency behaviors | **PASS in SQLite; PostgreSQL NOT RUN CURRENTLY** | The SQLite suite exercises migrations, transactions, crash/recovery and concurrency paths. The previous report's real PostgreSQL results are historical; no PostgreSQL restart/interruption drill was run in this Windows session. |
| 3 | Request validation and risk rejection | **PASS** | Invalid/unapproved signals, order limits, and unsupported environments refuse before adapter call in fake/paper tests. |
| 3 | Intent, SUBMITTING, accepted/rejected outcomes | **PASS** | Durable write-ahead and order transitions covered in `test_hardening_orders.py` and crash matrix with fakes. |
| 3 | Partial/full fill, cancellation, balance/position reconciliation | **PASS** | Incremental fills, atomic position/trade changes, cancel confirmation, and reconciliation findings covered locally with fakes. |
| 3 | Timeout, connection loss, ambiguous response | **PASS** | Fake-adapter unknown outcomes are reconciled; tests assert no blind duplicate submission. No real venue behavior inferred. |
| 3 | Crash during submission / after exchange acceptance / restart | **PASS** | Child processes are killed at bounded crash points against a file-backed fake; recovery checks order/trade/position/reservation invariants. Does not emulate real power loss or exchange behavior. |
| 3 | Duplicate local requests and concurrent submissions | **PASS** | Unique intent/client IDs and concurrency tests. Exchange-side idempotency is **UNVERIFIED**. |
| 3 | Delayed exchange response | **NOT RUN** | No dedicated delayed-response exchange test found in current executed suite. |
| 3 | End-to-end exchange rate-limit response to order write | **UNVERIFIED** | Error normalization and generic unknown-outcome paths are tested; real endpoint rate-limit semantics are not. |
| 4 | Binance trading adapter | **NOT IMPLEMENTED** | Signed permission check only; no order submission, cancel, account snapshot, fills, status lookup, positions, or testnet lifecycle. Permission/signature tests use fake transport. |
| 4 | Bybit trading adapter | **NOT IMPLEMENTED** | Stub; all trading methods refuse. Public market-data parser is unit-tested with fake transport only. |
| 4 | OKX trading adapter | **NOT IMPLEMENTED** | Stub; all trading methods refuse. Public market-data parser is unit-tested with fake transport only. |
| 4 | Testnet / production exchange integration | **NOT RUN** | No exchange endpoint contacted. Testnet order execution disabled; verified adapter allowlists empty. |
| 5 | Candle parse, closed bars, gaps, provider failover | **PASS** | Provider parser/data-quality tests use injected transports; no live provider call. |
| 5 | Candle-age freshness gate | **PASS for local code paths** | Freshness uses actual open timestamps and timeframe-derived close time. State builder, API state paths, bot ticks, and live gate validate closure, age, skew, sequence order, gaps, and OHLCV. Regression cases prove successful-but-stale provider data cannot create a signal or intent. Real provider behavior remains unverified. |
| 5 | NaN/invalid price boundaries | **PASS** | Market-data parsers and signal/order boundaries reject tested non-finite values. |
| 5 | Position, daily loss, drawdown, leverage/size, exposure and kill switch | **PASS** | Risk and reservation tests pass locally. No guarantee under real market conditions. |
| 5 | Configured spread and price-deviation guard | **PASS for normalized fake/paper quotes; UNVERIFIED against venues** | Required settings bound spread in percent of mid and deviation from approved reference to executable ask/bid. Missing, stale, invalid, wide, or moved quotes block; quote is rechecked before SUBMITTING. No real adapter quote contract has been verified. |
| 5 | Depth-based slippage / market impact | **NOT IMPLEMENTED** | Adapter model does not provide verified executable market depth to the execution guard. Top-of-book thresholds do not guarantee fill price or model market impact. |
| 5 | Active position/disconnect, unresolved state, safety halt | **PASS** | Covered fake failure paths block risk-increasing orders and preserve unresolved reservations; real exchange behavior is unverified. |
| 6 | Authentication, authorization, expiry, revocation, lockout | **PASS** | HTTP and service tests pass. Single-tenant model; MFA not implemented. |
| 6 | Credential encryption/rotation/redaction | **PASS** | Fernet/versioned-key tests, secret scan, and leak tests pass. Key management is environment-based; no KMS. |
| 6 | Audit-chain integrity | **PASS** | Tampering/append-only tests pass. Off-host head anchoring is not implemented; DB owner could rewrite the chain. |
| 6 | Backups, restore, monitoring/alerts | **NOT IMPLEMENTED** | Backup is operator-owned; no restore drill. Alerts are logs/audit plus optional in-process handlers; external delivery is not configured. |
| 7 | Docker image / Compose runtime | **BLOCKED CURRENTLY** | Docker CLI and Compose are installed; Compose configuration validation passed, but the Docker Desktop engine was unreachable, so no image build or container test ran in this session. Earlier Docker results are historical. |
| 7 | Hosted CI | **NOT RUN** | Workflow YAML parsed only; no GitHub Actions run. |
| 8 | Live vs simulated dashboard state | **PASS WITH LIMITATIONS** | Current Chrome run confirmed the 15 demo pages are labeled simulated and Live Activity/Strategy Manager require backend login. No user account was available for authenticated browser data. |
| 8 | UI order action / UNKNOWN display | **PASS** | No real-order route is exposed in the tested UI surfaces; UNKNOWN is not rendered as success and has no retry control. |
| 9 | Tautological assertions/races/skip behavior | **PASS** | Current suites ran with HTTP required; no tests were weakened. SSE checkpoint assertion passes. `test_api_testclient.py` had a stale “never run” docstring, corrected separately; assertions remain unchanged. |

## High-priority blockers and next actions

1. **Preserve the locks and complete database recovery evidence:** run the full-layout CI job and add application-level PostgreSQL interruption/restart coverage for order updates, audit records, and reconciliation.
2. **Keep exchange development sandboxed:** build only a testnet-specific adapter path and verify it against an official sandbox with operator-supplied test credentials. Add depth/market-impact controls before claiming slippage protection.
3. **Complete operational controls:** MFA, off-host audit anchoring, actionable alert delivery, backup/restore drills, and formal operator authorization remain prerequisites for any future live eligibility.


## Phase 5.1 update (2026-10-28) — does not replace the matrix above
Overall gates unchanged: **paper/simulation development only; testnet NOT READY; live NOT READY and LOCKED.**

| Stage | Area | Status | Evidence / limitation |
|---|---|---|---|
| 5 | Pre-trade notional, balance, exposure and reservation use one conservative executable-price basis (BUY max(ref, ask), SELL min(ref, bid)) | **PASS on fake/paper; UNVERIFIED against venues** | `test_executable_price_accounting.py` (17 tests, 11 fail if the old reference basis is restored); SQLite 391 passed, PG16 394 passed. Top-of-book only. |
| 5 | Quote revalidated immediately before submission; BUY not sent if the ask rose above the reserved price | **PASS on fakes** | Same file. Phase 5.2: a SELL's minimum notional and lot size are re-checked at the send-time bid (`RULES_REVALIDATION_FAILED`, order FAILED, no retry, no duplicate); 5 regression tests in the same file. Fake adapters only. |
| 5 | RiskEngine sizing vs executable-price backstop | **OPEN (operational)** | Sizing at the reference up to exactly the cap is now refused after slippage; needs sizing headroom or quote-based sizing (NOT IMPLEMENTED). Two fixtures were adjusted and disclosed. |
| 3 | Exactly one `ORDER_SUBMITTED` audit event per successful submission; replay adds none; ambiguous outcome records none | **PASS on fakes/paper** | Duplicate was NOT reproduced in the code; regression tests added. Distinct from the submission count (one adapter call). |
| 1 | Full SQLite / PostgreSQL 16 suites | **PASS** | 391/0/33 and 394/0/30 (custom runner; the skips are HTTP tests not run here and 3 PG-only). |
| 1 | Real FastAPI HTTP/SSE tests, browser e2e, Node tests, Docker, Windows, CI | **NOT RUN in this environment** | No FastAPI/pytest/httpx installable here; earlier PASS results in this matrix were produced elsewhere and are not re-verified by this rerun. |
| 2 | Exchange testnet / live readiness | **NOT READY** | Unchanged: no verified adapter, empty allowlists, TESTNET refused, no real-exchange idempotency/reconciliation evidence, no MFA/off-host audit anchor/alert delivery/backup drill. |


## Phase 5.2 update

| Item | Status | Notes |
|---|---|---|
| SELL send-time revalidation (bid, min notional, lot size) | **PASS on fakes** | Post-change focused suite passed 89/89; the 3 targeted SELL tests passed with 19 deselected. A bid drop changes notional from 5.01 to 4.99998, is blocked before submission, records persisted `ORDER_BLOCKED` with `stage=pre-send`, leaves position unchanged, and replay creates no second submission. Full SQLite suite: 426 passed, 3 PostgreSQL-only skips. Earlier mutation check: disabling revalidation fails 3 tests. |
| Quote-based / max-quantity risk sizing | **NOT IMPLEMENTED - OPEN** | Evaluated and deferred. The RiskEngine sizes at the reference price; a size at the 10% cap can be refused by the executable-price backstop after slippage. Failure mode is a safe BLOCK, never an oversize. A fix needs quantity reduction inside the execution engine after reading equity/reservations under the scope lock, and must only ever reduce quantity and keep the independent checks. Not done in this phase. |

### Phase 5.2 verification results

| Check | Result | Evidence |
|---|---|---|
| Full SQLite/backend suite | **PASS** 426 passed, 0 failed, 3 skipped | Current Windows run; required HTTP tests enabled. PostgreSQL-only tests skipped. |
| Focused execution safety and package suite | **PASS** 89 passed, 0 failed, 0 skipped | Re-run after adding the persisted pre-send audit assertion; executable-price, market-execution, reservations, live activation, and strategy-package tests. |
| Targeted SELL send-time regressions | **PASS** 3 passed, 0 failed, 19 deselected | Confirms below-minimum send-time bid is blocked, audited, not submitted or retried; passing-threshold control case also passed. |
| Frontend Node tests | **PASS** 9/9 | `node --test frontend/tests/*.test.js` |
| JavaScript syntax checks | **PASS** | `node --check` passed for `app.js`, `activity.js`, and `packages.js`. |
| Real Google Chrome | **PASS WITH LIMITATIONS** | 17 navigation items, three viewport sizes, live-mode lock, login validation, no console exceptions or failed requests. Authenticated Live Activity/Strategy Manager content not accessed. Evidence: `docs/evidence/phase5_2_windows_chrome_results.json` and `docs/evidence/phase5_2_windows_chrome_dashboard.png`. |
| Empty-login regression | **FIXED AND VERIFIED IN CHROME** | Username/password now use browser-required validation; empty and partial submissions produced no `/auth/login` request. Added a browser regression case; Playwright suite itself could not run because Playwright is absent. |
| Compilation / secret scan | **PASS** | `compileall` succeeded; secret scan reported 0 findings. |
| Docker Compose configuration | **PASS (configuration only)** | `docker compose -f docker-compose.test.yml config --quiet`; the Docker daemon was unavailable, so no container started. |
| PostgreSQL integration suite | **BLOCKED / NOT RUN** | Docker daemon unavailable; no safe, dedicated PostgreSQL test endpoint was configured. |
| Playwright suite | **NOT RUN** | Python `playwright` and its browser test dependencies are not installed. Standalone Google Chrome was used for browser validation instead. |
| Ruff / pip-audit | **NOT RUN CURRENTLY** | Neither tool is installed in the active environment; historical Phase 5.1 results remain historical only. |
| Quote-based sizing | **NOT IMPLEMENTED (open)** | Executable-price backstop safely blocks cap breaches; quantity reduction from live quote is not implemented. |
| Testnet / live / Phase 3 | **NOT READY / INCOMPLETE** | No exchange endpoint was contacted; testnet and live execution remain locked. |

For the complete current Windows results, see [`docs/PHASE5_2_WINDOWS_ACCEPTANCE_TEST_REPORT.md`](docs/PHASE5_2_WINDOWS_ACCEPTANCE_TEST_REPORT.md).
