# Phase 3.4 audit: final defect repair and verification

**Status: PHASE 3 INCOMPLETE. Live trading: LOCKED.**

## What could not be verified
- Real FastAPI HTTP execution (package index unreachable): the 22 HTTP tests, including the 5 new regressions, are **NOT EXECUTED**. Handler logic was exercised through a direct-call stand-in only (`logout`, `live/enable`: 14/14 pass; reverting the logout guard fails 4 checks). That is not HTTP evidence.
- CI and Docker never ran (no runner, no Docker daemon). Compose files and workflow were validated structurally (`docker compose config`, YAML parse).
- The user's crash-matrix hang was not reproduced.

## Failure A: unauthenticated `POST /auth/logout` returned 204
Root cause: the route read the bearer header itself and had no auth dependency, so a missing/garbage token was a silent no-op 204. Fix: `current_actor` dependency; 401 for missing, invalid, expired or revoked tokens; revokes exactly the presented session. Tests: `test_logout_requires_authentication_and_is_not_a_public_route`, `test_logout_revokes_only_the_presented_session_and_cannot_be_replayed` (not executed over HTTP; stand-in passes).

## Failure B: `POST /safety/live/enable` did not return 409 when readiness failed/unverified
Root cause: activation consulted only per-order gate pieces; there was no authoritative activation policy and the route mapped refusals to a generic error. Fix: one server-side `SafetyController.activation_readiness()` (trading state, audit integrity, open incidents, per-exchange gate, engine live lock via `_live_capable`, worker health; any evaluation error refuses), used by `enable_live`; route returns 409 `{error: live_readiness_failed, reasons, live_trading}`; non-ADMIN 403. Nothing client-supplied is consulted (signature test). Refusals are audited. The engine live lock is unchanged and independent. Tests: `test_live_activation.py` (7, SQLite + PG, executed) and HTTP `test_enable_live_*` (not executed).

## Crash-recovery hang
See `docs/CRASH_RECOVERY.md`. Not reproduced; unbounded waits removed; diagnostics and watchdog added.

## Also found and fixed
- `docker-compose.yml` was structurally broken: the postgres service body sat inside `tests`, producing a duplicate `environment` key that would have dropped `DEWAKO_TEST_DB`, i.e. a silent SQLite run. Tests moved to `docker-compose.test.yml`.
- CI: `assert_no_skips.py` now supports `--require`, `--min-tests`, `--allow-skip`; PG job pins server major 16 and requires the identity test, HTTP regressions and the nine crash tests; SQLite job allows only PostgreSQL-only skips.
- Fixture note: `make_live_ready` installs stand-ins for worker health and the live-capability hook; the policy itself is tested separately.

## Results (author's environment, custom runner with pytest shim; `python -m pytest` NOT run)
| Category | Command | Exit | Passed | Failed | Skipped |
|---|---|---|---|---|---|
| SQLite, all | `cd backend/tests && run_tests.py` | 0 | 247 | 0 | 24 (22 HTTP need FastAPI, 2 PG-only) |
| PostgreSQL 16.15, all | same with `DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16 DEWAKO_TEST_PG_URL=...` | 0 | 249 | 0 | 22 (HTTP) |
| Crash matrix + recon, SQLite x5 / PG x1 | `run_tests.py crash` | 0 | 14 (matrix) / 21 | 0 | 0 |
| Activation | `run_tests.py live_activation` | 0 | 7 | 0 | 0 |
| HTTP | pytest + FastAPI | not run | 0 | 0 | 22 NOT EXECUTED |
| CI / Docker | | not run | | | NOT TESTED |

## Remaining production blockers
HTTP tests unexecuted; CI/Docker unrun; no real or testnet exchange verification (adapters cannot submit or reconcile); custom PostgreSQL driver; no MFA; audit head not anchored off-host; env-held encryption key; "market data fresh" is call-success not candle recency; no power-loss/PG-death testing; hang cause unconfirmed.
