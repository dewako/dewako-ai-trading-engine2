# Dewako AI Trading Engine Phase 4.5 — Independent Audit

**Audit date:** 2026-10-27  
**Readiness verdict:** **NOT READY**  
**Input archive SHA-256:** `DBF313E949B1106E2981F88E017A42BAA4DA9773F0C6B7B7DF966F226108AF5B`  
**Source provenance:** Extracted project had no `.git` metadata; the audit covered 213 project files excluding the bundled virtual environment and caches.

## Executive summary

The project is a Python/FastAPI trading-engine backend with a vanilla-JavaScript dashboard. SQLite, authentication/authorization, order-state persistence, risk gates, crash-recovery paths, and real FastAPI HTTP/SSE behavior were exercised locally. The initial pinned environment exposed failing activity-feed tests and 15 dependency advisories. The failures were traced to test assumptions/harness behavior, not to an unsafe change in stream checkpoint semantics; the tests were corrected to assert the documented at-least-once contract. The dependency pins were upgraded to versions with no findings in the final `pip-audit` checks.

Final local test totals: **403 passed, 0 failed, 3 skipped** (381 backend, 13 real-app browser, 9 Node). The three backend skips are PostgreSQL-only. PostgreSQL, Docker/Compose execution, hosted CI, and all real exchange/testnet operations were unavailable or unverified. No real credentials were used and no real orders were submitted.

## Architecture summary

- `backend/dewako` contains market-data providers, strategy/signal logic, risk controls, order execution, persistence, authentication, reconciliation, and FastAPI routes.
- Persistence supports SQLite and PostgreSQL through a project-owned PostgreSQL wire client and shared migrations. Production configuration requires PostgreSQL and does not fall back to SQLite.
- Authentication uses scrypt password hashes, opaque bearer sessions stored as SHA-256 hashes, server-side role/permission checks, expiry, revocation, lockout, and rate limiting. Exchange credentials are Fernet-encrypted server-side with versioned-key rotation.
- The order path records decisions and intents durably, writes `SUBMITTING` before network submission, and treats ambiguous outcomes as `UNKNOWN` for reconciliation rather than resending. The risk engine and safety controller fail closed on uncertainty.
- Audit entries are scrubbed and hash-chained, with database guards against ordinary update/delete. The chain head is not anchored off-host, so a database owner could rewrite the full chain.
- Exchange adapters are not production-ready: Binance has a permission-check path but no verified order submission; Bybit and OKX are stubs. The reviewed live/testnet adapter allowlists are empty and TESTNET order execution is disabled. Fake exchanges in tests are test doubles, not evidence of exchange integration.
- The frontend is a prototype, not the planned React application. Live Activity and Strategy Manager use backend data; other pages are labelled simulated/demo.

## Prioritized findings and disposition

### Critical

None confirmed in the reviewed source or current dependency set.

### High — readiness blockers

1. **No verified exchange or testnet lifecycle.** There is no verified live/testnet adapter, and TESTNET execution is explicitly refused. All exchange submission/reconciliation test evidence uses in-process fakes. This is a deliberate fail-safe, but blocks testnet or live eligibility.
2. **PostgreSQL production path not verified in this environment.** The complete SQLite suite passed, but no PostgreSQL server, `psql`, or Docker daemon was available. Three PostgreSQL-only tests were skipped. Do not treat SQLite results as PostgreSQL evidence.
3. **Operational controls for real-money deployment remain incomplete.** Existing readiness documentation identifies MFA, an off-host audit-head anchor, alerting, backup/restore drills, and real account/order reconciliation as incomplete or untested.

### Medium — residual engineering risks

1. **Market-data freshness is incomplete for live decisions.** Existing readiness notes say the freshness gate records a successful provider call, not necessarily the age of the latest candle. This remains a live-readiness blocker until freshness is based on market timestamps.
2. **CSP retains `style-src-attr 'unsafe-inline'`.** This is documented and limited to style attributes; it is not an executable-script permission. Dynamic dashboard values are escaped and browser CSP tests passed.
3. **Dependency advisories in the original constraints were fixed during this audit.** `pip-audit` found 15 advisories in the old pinned versions of `cryptography`, `Starlette`, and test-only `pytest`. Direct minimums and constraints were raised; the final pinned environment and runtime requirement resolution returned no known vulnerabilities.

### Low

No additional confirmed low-severity source vulnerability. The final FastAPI/Starlette test run emits one deprecation warning about Starlette's `TestClient` integration with `httpx`; tests still pass. Revisit this when updating the test client stack.

## Security dependency findings (resolved)

The security sub-review found no concrete source-code vulnerability in its reviewed scope. The dependency audit separately found these vulnerable historical pins; they are included here for traceability. The displayed severity is a prioritization for this project, not a CVSS score (the `pip-audit` output used here did not provide CVSS scores).

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🟡 MEDIUM | backend/constraints-test.txt | 9 | Resolved: prior `cryptography==44.0.0` matched 7 advisories (`PYSEC-2026-35`, `PYSEC-2026-1284`, `PYSEC-2026-2141`, `PYSEC-2026-3552`, `PYSEC-2026-3553`, `PYSEC-2026-3554`, `GHSA-537c-gmf6-5ccf`); upgraded to `50.0.2`. | 10/10 |
| 2 | 🟡 MEDIUM | backend/constraints-test.txt | 32 | Resolved: prior `starlette==0.41.3` matched 7 advisories (`PYSEC-2026-161`, `PYSEC-2026-249`, `PYSEC-2026-248`, `PYSEC-2026-1942`, `PYSEC-2026-1941`, `PYSEC-2026-2281`, `PYSEC-2026-2280`); upgraded to `1.10.0` with compatible FastAPI. | 10/10 |
| 3 | ⚪ LOW | backend/constraints-test.txt | 27 | Resolved: prior test-only `pytest==8.3.4` matched `PYSEC-2026-1845`; upgraded to `9.1.1`. | 10/10 |

Final checks: `pip-audit --path <pinned-site-packages>` and `pip-audit -r backend/requirements.txt` both reported **No known vulnerabilities found**.

## Confirmed bugs and fixes

1. **SSE resume test stopped before its cursor checkpoint.** `ActivityService.poll` advances per-source watermarks and `event_stream` attaches the checkpoint only to the final activity frame in a poll batch. Disconnecting before that frame can replay part of the batch; this is the documented at-least-once contract and clients de-duplicate by `event_id`. The old HTTP test stopped after an arbitrary event count plus any heartbeat, then incorrectly required no replay. It also checked only a subset of authoritative activity rows. The regression now waits for the latest delivered activity frame to carry a cursor, verifies that cursor covers every delivered event, retains the no-replay assertion after that checkpoint, and checks streamed IDs against the complete stored activity history. No server-side cursor behavior was weakened or changed.
2. **Package activity summaries omitted package identity.** Structured events already carried the package name/version, but the human-readable summary showed only the actor and internal package ID. Summaries now include the validated package name/version, with a regression assertion.
3. **Real-app browser setup had event-loop and timing races.** The browser test helper called `asyncio.run()` on the Playwright control thread; real-mode seeding now runs on a bounded worker thread and propagates worker errors. The pause test now waits for initial history before taking its baseline count, and browser polling uses locator waits compatible with the strict CSP. CSP itself remains enabled.
4. **Tautological assertions masked regressions.** Removed `or True` from the activity ordering and hostile-archive path checks, replacing them with real ordering and non-disclosure assertions. Removed a vacuous nonnegative-count assertion.

## Test totals and commands

### Final test totals

| Suite | Passed | Failed | Skipped | Notes |
|---|---:|---:|---:|---|
| Backend pytest | 381 | 0 | 3 | Python 3.13.15, patched constraints, `DEWAKO_REQUIRE_HTTP_TESTS=1`; SQLite and real FastAPI/TestClient/Uvicorn tests executed. Three PostgreSQL-only tests skipped. |
| Real-app Chromium dashboard | 13 | 0 | 0 | `DEWAKO_E2E_SERVER=real`; actual FastAPI app under local Uvicorn, SQLite database, scripted fake exchange only. |
| Frontend Node tests | 9 | 0 | 0 | `node --test frontend/tests/activity.test.js`. |
| **Total** | **403** | **0** | **3** | Additional repeated SSE integration checks are not added to these totals. |

All collected local tests ran. PostgreSQL, Docker/Compose execution, remote CI, real testnet, and real exchange integration are **not run**, not passed.

### Commands and actual results

Commands were run from the extracted project, using Windows PowerShell.

| Command/check | Actual result |
|---|---|
| Initial `python -m pytest -q` using the supplied Python 3.14 environment | 379 passed, 2 failed, 3 skipped. Failures: package summary missing name; SSE test disconnected before receiving a cursor checkpoint. |
| `pip install -r requirements.txt -c constraints-test.txt -r requirements-test.txt` on Python 3.14 | Failed building the pinned `pydantic-core==2.27.1` and `greenlet==3.1.1`; those pins do not build on the available 3.14 interpreter. |
| Same pinned install in a clean Python 3.13.15 virtual environment | Succeeded. Initial dependency audit found 15 advisories; constraints and minimum versions were then upgraded. |
| Final pinned install after dependency updates | Succeeded; versions include FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.1, cryptography 50.0.2, pytest 9.1.1, httpx 0.28.1, and Uvicorn 0.54.0. |
| `DEWAKO_REQUIRE_HTTP_TESTS=1 python -m pytest -q` (final pins) | **381 passed, 3 skipped, 0 failed**, 1 deprecation warning, 117.70 seconds. |
| Three targeted package/SSE regressions | 3 passed. |
| Real-HTTP SSE resume regression repeated five times | 5/5 passed after checkpoint-aware assertions. |
| `DEWAKO_E2E_SERVER=real python frontend/browser_tests/test_dashboard_browser.py` (final pins) | **13 passed, 0 failed** against actual FastAPI/Uvicorn. |
| `node --test frontend/tests/activity.test.js` | **9 passed, 0 failed**. |
| `python scripts/scan_secrets.py` | `secret scan: 0 finding(s)`. |
| `python -m compileall -q backend/dewako backend/tests` | Exit 0. |
| `ruff check backend/dewako --select E9,F63,F7,F82` | `All checks passed!` |
| `pip-audit --path <final pinned site-packages>` | `No known vulnerabilities found`. |
| `pip-audit -r backend/requirements.txt` | `No known vulnerabilities found`. |
| Parse `.github/workflows/ci.yml`, `docker-compose.yml`, `docker-compose.test.yml` as YAML | All three parsed successfully; this is structural validation only. |
| `python -m playwright install chromium` | Completed successfully after mirror timeouts/resets and retry. |
| `docker version`; checks for `psql`, `pg_isready`, PostgreSQL service | Docker command unavailable; no PostgreSQL client or service found. |

The initial real-browser run also exposed the seeder/CSP test-harness failures described above; after correcting them, the complete 13-case real-app browser suite passed. No exchange endpoints were contacted during any test.

## Files changed

| File | Reason |
|---|---|
| `backend/dewako/activity/events.py` | Include package name/version in activity summaries. |
| `backend/tests/test_activity.py` | Assert package summary identity and canonical event ordering without a tautological expression. |
| `backend/tests/test_api_phase44.py` | Test complete SSE cursor checkpoints and compare delivery against all stored activity events. |
| `frontend/browser_tests/test_dashboard_browser.py` | Fix real-mode seeder event-loop conflict, use CSP-compatible element waits, and enforce non-tautological archive-path checks. |
| `backend/requirements.txt` | Raise minimum supported package versions past the discovered advisories. |
| `backend/pyproject.toml` | Keep package/dev install minimums aligned with the patched versions. |
| `backend/constraints-test.txt` | Record the resolved, pinned test dependency set. |
| `docs/TESTING.md` | Document resolved pins and Python 3.13 test environment constraint. |
| `docs/LIVE_ACTIVITY.md` | Document batch checkpoint/replay semantics and current verification status. |
| `PHASE4_5_REPORT.md` | Mark the original implementation report historical and point to current audit. |
| `PHASE4_5_INDEPENDENT_AUDIT.md` | This audit report. |

## Remaining blockers and verdict

- PostgreSQL 16 suite and restart/crash tests on PostgreSQL were not run because no server/client or Docker was available. SQLite crash/recovery and persistence tests passed.
- Docker image/Compose execution and GitHub Actions were not run; YAML parsing is not equivalent to either.
- No real exchange or testnet adapter is verified. Public market-data providers and the Binance permission-check path have not been independently proven against live exchanges in this audit.
- MFA, off-host audit-head anchoring, alerting, backup/restore drills, and real account/order reconciliation remain incomplete or unverified.
- Do not infer exchange connectivity, trading profitability, production readiness, or real-money safety from fake-exchange, synthetic-data, SQLite, or browser-test results.

**Verdict: NOT READY.** The engine's live lock and missing verified adapters intentionally prevent a readiness claim. No real orders were enabled or submitted.
