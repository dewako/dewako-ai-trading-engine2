# DEWAKO Live Trading Readiness

**Live trading: LOCKED. Overall status: NOT READY.** Phase 3.4 (final pass). Evidence: `PHASE3_4_FINAL_HARDENING_AUDIT.md` and `docs/evidence/`. Earlier counts (247/249) in `PHASE3_4_HARDENING_AUDIT.md` are superseded.
**Phase 4.5 evidence addendum is at the bottom of this file; the table below is the preserved Phase 3.4 assessment.**
Every exchange in the evidence is a fake or a test double; a fake passing is not evidence about a real exchange.
Status words: PASS, PARTIAL, FAIL, NOT TESTED. "Blocks live" = must be resolved before any real-money order.

Commands used (author's environment, Python 3.13):
- SQLite: `cd backend/tests && <runner>` -> 249 passed, 0 failed, 25 skipped, exit 0 (skips: 22 HTTP tests that need FastAPI, 3 PostgreSQL-only).
- PostgreSQL 16.15: `cd backend/tests && DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16 DEWAKO_TEST_PG_URL=postgresql://dewako:...@127.0.0.1:55432/dewako <runner>` -> 252 passed, 0 failed, 22 skipped (the HTTP tests), exit 0.
- `<runner>` is a small custom runner with a pytest shim (pytest and FastAPI could not be installed: package index unreachable). The same files are written for `python -m pytest`, which was NOT run.

| Area | Status | Source | Evidence (test / command) | Limitation | Blocks live |
|---|---|---|---|---|---|
| Live activation policy (turning live ON) | PASS at service level; HTTP NOT TESTED. Now also requires a human-ADMIN TESTNET_VERIFIED attestation (an attestation, not proof) | `safety/controller.py activation_readiness/enable_live`, `execution/engine.py _live_capable` | `test_live_activation.py` (9 tests, SQLite + PG). Server evaluates trading state, audit integrity, open incidents, per-exchange gate, engine live lock, worker health; caller input cannot influence it; evaluation error refuses | Route mapping to 409 verified only through a stand-in (below), not real HTTP | no (for the policy) |
| `POST /safety/live/enable` returns 409 when not ready | PARTIAL: stand-in only | `api/main.py live_enable` | Stand-in direct-route calls: 409 + structured reasons, switch stays off (14/14, incl. logout). Real-HTTP tests `test_enable_live_*` written, NOT EXECUTED | FastAPI TestClient never ran here | yes |
| `POST /auth/logout` requires auth (401) | PARTIAL: stand-in only | `api/main.py logout` | Stand-in: 401 for no/empty/garbage token, replay 401, other sessions unaffected; reverting the guard makes 4 checks fail. Real-HTTP tests NOT EXECUTED | as above | yes |
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
| CI | NOT TESTED | `.github/workflows/ci.yml` | structural validation only (YAML parses, report names match tests) | never ran on a runner | yes |
| Docker | NOT TESTED | `backend/Dockerfile`, `docker-compose*.yml` | `docker compose config` validates both compose files; no daemon, nothing built or run | image never built | yes |
| MFA, off-host audit anchor, alerting, backups drill | NOT BUILT / NOT TESTED | | | | yes |

## Overall Status
NOT READY. Live trading remains LOCKED by: the activation policy (refuses while any check fails), the independent engine live lock (no adapter is live-verified), and the per-order live gate.


## Phase 4.5 evidence addendum (does not replace anything above)
**Live trading: LOCKED. Overall status: NOT READY. Phase 3: INCOMPLETE. No exchange is verified.** Evidence: `PHASE4_5_REPORT.md`, `docs/evidence/phase4_5_*`.
Environment: author sandbox, Python 3.13.16, custom runner + pytest shim, no FastAPI/httpx/pytest/Docker, local PostgreSQL 16.15.

| Area | Status after 4.5 | Evidence | Still blocks live |
|---|---|---|---|
| Full suite on SQLite | PASS: 351 passed, 0 failed, 33 skipped (22 old + 8 new HTTP tests need FastAPI; 3 PostgreSQL-only) | `phase4_5_sqlite.log` | n/a |
| Full suite on PostgreSQL 16 | PASS: 354 passed, 0 failed, 30 skipped (the 30 HTTP tests) | `phase4_5_pg16.log` | n/a |
| FastAPI routes over real HTTP (incl. 4.4 routes) | **NOT VERIFIED** (30 tests exist, never ran; pip blocked) | `phase4_5_browser_real_BLOCKED.log` | yes |
| Dashboard e2e vs real app | **NOT RUN / BLOCKED**; stand-in layer: 13/13 pass (labelled stand-in) | `phase4_5_browser_standin.log` | yes |
| UI policy | PARTIAL: no `unsafe-inline` for scripts, verified in Chromium against the stand-in using the shared constants; headers on the real app NOT VERIFIED; `style-src-attr 'unsafe-inline'` exception documented | `docs/LIVE_ACTIVITY.md` | no |
| Integration invariants (activity read-only, no order route, verified_at writer, no live env var, fake-only tooling) | PASS (static + runtime checks) | `test_phase45_invariants.py` | no |
| Unknown order stays UNKNOWN until reconciliation | PASS at service level (engine/reconciliation tests, activity tests); HTTP version written, NOT RUN | `test_api_phase44.py` | yes (real exchange) |
| CI, Docker, Windows | **NOT RUN** (workflow extended with an e2e job, still never executed) | | yes |
| Testnet lifecycle, protective orders, account identity, balance/position reconciliation on a real exchange, operator authorization drill, MFA, off-host audit anchor | unchanged: NOT DONE | Phase 3.4 table above | yes |
