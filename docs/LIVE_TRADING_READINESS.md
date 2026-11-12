# DEWAKO Live Trading Readiness

**Live trading: LOCKED. Overall status: NOT READY.** Reviewed as a whole at Phase 3.4. On 2026-11-13 (`main` = `9172eb2`) only the rows marked *updated 2026-11-13* were re-verified against CI evidence; every other row is unchanged and was not re-verified since Phase 3.4.
Phase 3.4 (final pass). Evidence: `PHASE3_4_FINAL_HARDENING_AUDIT.md` and `docs/evidence/`. Earlier counts (247/249) in `PHASE3_4_HARDENING_AUDIT.md` are superseded.
Every exchange in the evidence is a fake or a test double; a fake passing is not evidence about a real exchange.
Status words: PASS, PARTIAL, FAIL, NOT TESTED. "Blocks live" = must be resolved before any real-money order.

Commands used at Phase 3.4 (author's environment, Python 3.13; superseded counts: hosted CI on `da950cb` ran 874 tests per backend, run 43299863383):
- SQLite: `cd backend/tests && <runner>` -> 249 passed, 0 failed, 25 skipped, exit 0 (skips: 22 HTTP tests that need FastAPI, 3 PostgreSQL-only).
- PostgreSQL 16.15: `cd backend/tests && DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16 DEWAKO_TEST_PG_URL=postgresql://dewako:...@127.0.0.1:55432/dewako <runner>` -> 252 passed, 0 failed, 22 skipped (the HTTP tests), exit 0.
- `<runner>` is a small custom runner with a pytest shim (pytest and FastAPI could not be installed: package index unreachable). The same files are written for `python -m pytest`, which was NOT run.

| Area | Status | Source | Evidence (test / command) | Limitation | Blocks live |
|---|---|---|---|---|---|
| Live activation policy (turning live ON) | PASS at service level; HTTP NOT TESTED. Now also requires a human-ADMIN TESTNET_VERIFIED attestation (an attestation, not proof) | `safety/controller.py activation_readiness/enable_live`, `execution/engine.py _live_capable` | `test_live_activation.py` (9 tests, SQLite + PG). Server evaluates trading state, audit integrity, open incidents, per-exchange gate, engine live lock, worker health; caller input cannot influence it; evaluation error refuses | Route mapping to 409 verified only through a stand-in (below), not real HTTP | no (for the policy) |
| `POST /safety/live/enable` returns 409 when not ready | PASS over real HTTP (*updated 2026-11-13*) | `api/main.py live_enable` | `test_enable_live_returns_409_with_reasons_and_never_claims_live_is_active` runs against the FastAPI app and is a required test in the SQLite and PostgreSQL CI jobs (green on `da950cb`, run 43299863383 x3) | fakes behind the route | no |
| `POST /auth/logout` requires auth (401) | PASS over real HTTP (*updated 2026-11-13*) | `api/main.py logout` | `test_logout_requires_authentication_and_is_not_a_public_route`, required in CI (as above) | none known | no |
| Readiness states | PASS at service level | `safety/controller.py readiness_state/record_evidence` | LOCKED/NOT_READY/PAPER_VERIFIED/TESTNET_VERIFIED/LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION; no automatic live state | not exposed over HTTP | no |
| PostgreSQL bounded waits | PASS | `persistence/pgwire.py` | lock/statement/idle-tx timeouts; held-lock test | DNS resolution not bounded | no |
| PostgreSQL restart recovery | PARTIAL | `scripts/pg_restart_check.py` | one immediate-restart scenario passes (`docs/evidence/`) | not restart during order update/incident/recovery; not in CI | yes |
| Protective-order verification, account identity on real exchange, candle-level freshness | NOT IMPLEMENTED | | | | **yes** |
| Database | PARTIAL | `persistence/db.py pgwire.py migrations.py` | PG 16 + SQLite pass; 5 migrations, clean + upgrade; production refuses SQLite | custom stdlib PG driver; DB restart/reconnect mid-connection not tested | yes |
| Order state machine | PASS | `execution/state_machine.py order_store.py engine.py` | transitions, write-ahead SUBMITTING, illegal transitions rejected | fakes | no |
| Idempotency | PASS | UNIQUE intent/client_order_id, `order_store.py` | independent PG connections racing one signal -> one order | exchange-side client-id dedupe unverified | no |
| Positions | PARTIAL | `portfolio/*` | spot long-only; futures rejected | no futures | no (spot) |
| Order reconciliation | PARTIAL | `reconciliation/*` | orders, positions, fills, persisted findings, human-only resolution, incidents | fakes only; real adapters report no fills/balances | yes |
| Balance reconciliation | PARTIAL | `reconciliation/*` | snapshots, unexplained-change detection, no auto-fix | only fakes/paper give snapshots | yes |
| Kill switch | PASS | `safety/controller.py` | durable, ADMIN release, rechecked before send, restart and cross-process tested | none known | no |
| Authentication | PARTIAL | `auth/*` | hashing, expiry, revocation, lockout at service level; logout stand-in | HTTP NOT TESTED; no MFA | yes |
| RBAC | PARTIAL | `auth/*`, `api/main.py` | matrix at service level | route enforcement over HTTP NOT TESTED | yes |
| Credential security | PARTIAL | vault, `exchanges/manager.py _redact` | Fernet, rotation, error-text redaction | env-held key, no KMS | testnet no; production yes |
| Worker lifecycle + lease | PARTIAL | `runtime/*` | supervisor, persisted health, fencing epoch checked before every LIVE send | fakes; single host | no |
| Market-data freshness | PARTIAL | `safety/controller.py`, `runtime/application.py` | per-symbol, 120 s, part of `live_gate` (tests in `test_audit_lease_freshness.py`) | "fresh" = provider call succeeded, not that the newest candle is recent | yes |
| Crash recovery | PASS (scoped), see `docs/CRASH_RECOVERY.md` | `tests/test_crash_matrix.py` | 9 required boundaries + extra, real SIGKILL, SQLite + PG; bounded by timeouts | hang reported by user NOT reproduced (order experiments: forward/reverse/shuffled, repeated, SQLite+PG: clean); power loss, PG death mid-connection, real exchange NOT TESTED | no for these; unverified for the rest |
| Paper/live isolation | PASS | engine live lock | adapter-environment match, spy test, engine refuses LIVE to any adapter that is not `live_execution_verified`/`test_double` | none known | no |
| Strategy integrity | PASS | fingerprints | changed strategy blocks | none known | no |
| Audit log | PARTIAL | `persistence/audit.py` | hash chain, append-only triggers, broken chain -> CRITICAL incident, gate closes | head hash not anchored off-host: not tamper-proof against a DB owner | yes |
| PG concurrency | PASS | reservations | 13 tests + races, PG 16 | none known | no |
| Failure injection | PARTIAL | `test_lifecycle_failure.py` | DB outage, timeouts, 500s, worker crash via fakes | no real network/DB fault injection | no |
| Exchange adapters | FAIL for live | `exchanges/*`, `docs/EXCHANGE_ADAPTERS.md` | see capability matrix: none can submit, look up or reconcile on a real exchange | no testnet verification | **yes** |
| Instrument constraints | PARTIAL | `execution/validation.py` | filters validated against adapter metadata | fakes | yes |
| Startup reconciliation | PASS on fakes | `runtime/application.py` | failure blocks live | real exchange NOT TESTED | yes |
| Unresolved items block | PASS on fakes | `live_gate` | any unresolved order/fill/balance/position blocks | fakes | no |
| Risk limits | PASS on fakes (spot) | durable reservations | concurrent workers cannot exceed limits | fakes | no |
| CI | PASS (*updated 2026-11-13*) | `.github/workflows/ci.yml`, `security.yml`, `paper-qualification.yml` | hosted runs; required tests, skip policy and minimum counts enforced; see `docs/GITHUB_CI_FINAL_VALIDATION_REPORT.md` | GitHub-hosted runners only; `main` protection is a repository setting | no |
| Docker | PARTIAL (*updated 2026-11-13*) | `backend/Dockerfile`, `docker-compose*.yml` | CI builds the image, runs the full suite in the test image against PostgreSQL 16, and smoke-tests the production compose (health, migrations, restart, graceful stop, refusal on a missing or invalid setting) | base images by tag, not digest; not run on production infrastructure | yes |
| MFA, off-host audit anchor, alerting, backups drill | NOT BUILT / NOT TESTED | | | | yes |

## Overall Status
NOT READY. Live trading remains LOCKED by: the activation policy (refuses while any check fails), the independent engine live lock (no adapter is live-verified), and the per-order live gate.
