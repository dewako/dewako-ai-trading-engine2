# Phase 3.3 Hardening Audit

Result: **PHASE 3 INCOMPLETE** (17 real-FastAPI tests, CI and Docker still unexecuted). Live trading remains disabled (and now hard-locked). Real-FastAPI HTTP tests, Docker and CI were written but could not be executed here.

## Environment and limits of this evidence
- Ubuntu 24.04 container, Python 3.13, PostgreSQL 16.15 (server binaries, throw-away cluster on 127.0.0.1:55432). SQLite 3 (stdlib).
- **Not pytest.** `pytest` is not installed and could not be (package index unreachable: DNS failure / 403). All counts below come from a small custom runner that imports each test module and calls each `test_*` function (it emulates `pytest.raises`/`skip`). Its exit status is not meaningful; counts are. The same files are written for real pytest and CI uses it, but that has not been run.
- **FastAPI/httpx not installable.** `test_api_testclient.py` (17 tests) is written against the real `fastapi.testclient.TestClient` and was **never executed**.
- **No Docker daemon.** Compose `test` profile and the `docker` CI job are unexecuted.
- **No exchange of any kind was contacted.** Everything below is a deterministic fake or the paper adapter.
- Python 3.12 (the Dockerfile base) was not tested.

## Baseline reproduced
`python3 -m compileall -q backend/dewako backend/tests` -> OK. Custom runner, SQLite: 195 passed. PostgreSQL 16: 185 passed, **12 failed** (all in `test_risk_reservations`). Re-run at the start of the follow-up session (before any change): SQLite 217 passed, PostgreSQL 219 passed, 0 failed, compileall OK.
Root cause: not product code. A single long process created a connection pool per test and never closed them, exhausting PostgreSQL's 100-connection limit ("sorry, too many clients already"); the 8-thread race tests then died with `BrokenBarrierError`. Fix: `Database.close_all_idle()` plus a `conftest.py` autouse fixture (and the runner) closing idle connections after every test. No assertion was weakened; the tests pass alone and together.

## Final results (exact)
| Run | Command | Result |
|---|---|---|
| SQLite | `cd backend/tests && python3 <runner>` (exit 0) | **238 passed, 0 failed**, 19 skipped (2 PostgreSQL-only, 17 HTTP) |
| PostgreSQL 16 | `DEWAKO_TEST_DB=postgres DEWAKO_TEST_PG_URL=postgresql://dewako:dewako_test_pw@127.0.0.1:55432/dewako python3 <runner>` (exit 0) | **240 passed, 0 failed**, 17 skipped (HTTP) |
| Static | `python3 -m compileall -q backend/dewako backend/tests` | OK. No linter ran (ruff/pyflakes unavailable). |
| Secret scan | `python3 scripts/scan_secrets.py` | 0 findings (heuristic) |

By file (PostgreSQL / SQLite): core 23/23, manage 20/20, market data 20/20, strategies 27/27, hardening orders 15/15, safety 19/19, platform 26/26, lifecycle+failure injection 15/15, crash+recon 7/5, **crash matrix (separate-process SIGKILL) 12/12**, **risk reservations (concurrency) 13/13**, **adapters+fills 17/17**, **config+migrations 5/5**, HTTP 0/0 (17 skipped).

## Requirement table
Status | Sources | Verified by | Limitation | Blocks live?

| Requirement | Status | Source files | Evidence (above runs) | Remaining limitation | Blocks live |
|---|---|---|---|---|---|
| PostgreSQL 16 integration | PASS (for what ran) | `persistence/db.py`, `pgwire.py`, `migrations.py` | whole suite on PG 16; clean migrate; constraints asserted server-side | custom wire driver; not run in CI/Docker; "database restart and reconnection" not tested | yes (driver + CI unverified) |
| Migration upgrade from previous schema | PASS | `migrations.py` | `test_upgrade_from_previous_schema_keeps_data_and_adds_new_tables` (SQLite, PG) | only v3->v4 tested | no |
| Production never silently falls back to SQLite | PASS | `db.database_target_from_env`, `Config.from_env`, `Runtime` | `test_production_requires_a_postgres_url...`, `test_unreachable_postgres_makes_startup_fail_closed...` | | no |
| Real HTTP auth/RBAC/validation tests | **NOT TESTED** | `api/*`, `tests/test_api_testclient.py` | written, 17 tests, never run | FastAPI not installable | **yes** |
| Rate limiting | PARTIAL | `api/security.py`, `auth/service.RateLimiter` | limiter logic unit-tested earlier; HTTP behaviour NOT TESTED | in-process only; bucket by client address (see defects) | no |
| Security headers / CORS | PARTIAL | `api/http_policy.py`, `api/main.py` | policy functions unit-tested; middleware over HTTP NOT TESTED | | no |
| CSRF | N/A | bearer-token auth, no cookies | | revisit if cookies are ever used | no |
| Durable risk reservations, concurrent limits | PASS | `execution/reservations.py`, `engine.py`, migration 4 | 13 tests, 8-way races with independent connections: per-order, exposure, balance, position count, same signal, same funds, rollback release, restart persistence | spot only; fills partially double-count conservatively | no |
| Process-level crash matrix | PASS for 10 scenarios on fakes | `tests/test_crash_matrix.py`, `sim_worker.py` | SIGKILL of a separate worker at each boundary 1-9 (+ hung worker, durable incident); SQLite and PG | no power-loss/kernel/disk test; no PG-server-death-while-connected test | no |
| Durable incident on unrecoverable ambiguity | PASS | `engine.py`, `safety/controller.py`, migration 4 | `test_10_*`, `test_9a_*` | | no |
| Reconciliation of orders/positions/balances | PARTIAL | `reconciliation/service.py` | earlier tests + new | fakes only | yes |
| Reconciliation of fills (late, duplicate, external, missing) | PASS on fakes | `reconciliation/service._fills` | 9 tests; findings only, books untouched, human-only resolution | only adapters declaring `fill_history`; none of the real adapters do | yes |
| Adapter capability reporting + contract tests | PASS (contract); real adapters mostly unimplemented | `exchanges/base.py`, `stubs.py`, `binance.py`, `paper_exchange.py` | capability honesty tests | see `docs/EXCHANGE_ADAPTERS.md` | yes |
| Live lock | PASS | `engine._refusals` | adapter that is not a test double and not `live_execution_verified` can never receive an order, even with live switch ENABLED, env flags and a confirmation phrase; Binance/Bybit/OKX blocked | | no (it IS the lock) |
| Paper/live isolation | PASS | engine | earlier spy tests | | no |
| Kill switch persistent and respected | PASS | `safety/controller.py`, engine | restart tests; `test_kill_switch_activated_between_creation_and_send_stops_the_send` | | no |
| Worker lease / ownership | PASS (single database, fakes) | `runtime/lease.py`, `runtime/application.py`, `engine._send_refusal`, migration 5 | `test_audit_lease_freshness.py`: exclusive acquire, expiry hand-over with epoch increment, stale worker refused at the last moment (checked against the DATABASE before every LIVE send), no duplicate order, release, local deadline, 8-way concurrent acquire on PG, application acquires/blocks second process/releases at shutdown | clock skew between processes shortens or lengthens ownership (documented); a worker stuck inside an exchange call when the lease expires can still complete that one call | no |
| Market-data freshness in the live gate | PASS for what it claims (PARTIAL in meaning) | `safety/controller.live_gate`, `runtime/application.market_data worker`, `engine` | per-symbol: never-confirmed blocks, stale (>120 s) blocks, other symbols do not inherit, re-checked immediately before send, failing provider leaves symbol stale | **"fresh" means the market-data worker's provider call for that symbol succeeded recently. It does not inspect the newest candle's own timestamp.** | no, but fix before a live review |
| Audit-log integrity (tamper evidence) | PARTIAL | `persistence/audit.py`, migration 5, `safety/controller.verify_audit` | SHA-256 hash chain; append-only triggers (SQLite and PG); alter/delete detected after triggers removed; concurrent writers never fork (6 connections); rollback leaves no gap; broken chain opens a CRITICAL incident, closes the live gate, blocks orders; verified at startup and by a worker; head hash in `/health/detailed` | a database owner can disable triggers and recompute the whole chain; deleting the newest rows leaves a valid shorter chain; head hash is not yet exported anywhere the DB owner cannot edit | yes until an external anchor exists |
| Strategy change with open orders | PARTIAL | `strategies/switching` | policy required for open positions (tested earlier) | unresolved *orders* not specifically covered | no |
| Local stop-loss presented as exchange protection | PASS (by absence) | - | no exchange-side stop is ever claimed; nothing records a local stop as "protected" | revisit when live adapters exist | no |
| MFA | NOT IMPLEMENTED | | | | yes |
| CI workflow | NOT TESTED | `.github/workflows/ci.yml` | YAML parses; never run | | yes |
| Docker test profile | NOT TESTED | `docker-compose.yml` | never run | | no |

## Defects found and fixed in this phase
1. Test harness exhausted PG connections (above).
2. Rate-limit bypass: the request limiter was keyed by the first 16 characters of the caller-supplied token, so inventing a new token on every request evaded it. It is now keyed by client address and by a hash of the token. (HTTP test written, not run.)
3. Exchange error text could echo credentials (fixed in 3.2; still covered).
4. Orders submitted/open were not re-synced after a crash (fixed in 3.2; still covered).
5. (follow-up) The new audit chain failed on PostgreSQL because timestamps come back as text in a different format (`2026-10-14 10:10:18+00`); the hash now uses one canonical form. Found by the PostgreSQL run, not on SQLite.
6. (follow-up) Closed the three FAILs from review: audit integrity, market-data freshness, worker lease (see table).

## Evidence classes
1. Unit tests: core, manage, market data, strategies, config.
2. HTTP integration: **none executed**.
3. PostgreSQL 16 integration: everything marked above, custom driver.
4. Process-level recovery: crash matrix + `test_sigkill_*`.
5. Fake-exchange contract tests: `test_adapters_and_fills.py`.
6. Exchange testnet verification: **none**.
7. Unverified production behaviour: all real exchange behaviour, network faults, PG server death, power loss.

## Recommendation
Do not proceed to Phase 4 as "Phase 3 complete". The Phase 4 work (research/backtesting) does not touch execution, so it can proceed in parallel at your discretion, but the unexecuted HTTP/CI/Docker checks, MFA, and an external anchor for the audit head must be closed before any controlled testnet or live review.
