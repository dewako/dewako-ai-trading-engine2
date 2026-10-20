# Phase 3.2 Hardening Audit

Starting point: the first hardening pass (150 tests, SQLite only, verdict NOT READY). This audit is the status **after** Phase 3.2.
Note on process: the working environment was reset mid-phase and the work was rebuilt from the session record before the final run, so
this document was finalised at the end rather than before the first change. The "start" column records what was true before 3.2.

## Implemented, and how it is tested

| Area | Before 3.2 | Now | Evidence |
|---|---|---|---|
| Database | SQLite only | SQLite **and PostgreSQL 16** (real server, stdlib wire client `persistence/pgwire.py`), dialect layer, 3 migrations with advisory-lock, constraints | whole suite run on both backends |
| Idempotency | DB unique constraints (SQLite) | same constraints enforced by PostgreSQL; `ON CONFLICT DO NOTHING`; no catch-and-continue inside a PG transaction | `test_pg_two_independent_connections_race_one_intent_one_order` (4 independent connections), `test_concurrent_*` |
| Atomicity | order create only | order event + state, fill + position + trade, risk decision + intent + order in single transactions | `test_fill_position_trade_are_atomic_and_recovery_completes_them`, `test_transaction_rolls_back_whole_change` |
| Startup lifecycle | function, not wired | `runtime/application.py`: config -> DB -> schema -> safety -> fingerprints -> recover orders -> recover positions -> reconcile -> risk state -> workers -> READY / READY_LIVE_BLOCKED; graceful shutdown | `test_lifecycle_failure.py` (15) |
| Worker supervision | rows in safety_state | `worker_health` table: name, status, last_heartbeat, last_success, last_error, restart_count; restart policy; critical worker failure blocks the live gate; workers disabled blocks the live gate | lifecycle tests |
| Recovery | SUBMITTING/UNKNOWN/RECONCILING | plus **SUBMITTED/OPEN/PARTIALLY_FILLED/CANCEL_REQUESTED re-synced from the exchange** (gap found in 3.2 by the atomicity test), never resends | `sync_active_orders` |
| Crash test | simulated by reopening the DB | **real SIGKILL** of a separate process after the exchange accepted an order; fresh process recovers; same signal afterwards is a duplicate | `test_sigkill_after_exchange_accepts_then_recovery_does_not_resend` (SQLite and PostgreSQL) |
| Reconciliation | orders and positions | explicit state machine (HEALTHY/RECONCILING/RECONCILIATION_REQUIRED/RESOLVED/FAILED), persisted findings, persisted account snapshots, balance reconciliation with unexplained-change detection, human-only resolution (ADMIN + written reason, audited), balances never auto-fixed | `test_unexplained_balance_change_*`, `test_balance_explained_*`, state-machine test |
| Secrets | scrubbed in audit | exchange error text redacted before it is logged or returned (**a leak was found and fixed in 3.2**) | `test_secret_values_never_appear_*` |
| Health | minimal | `/health` (public, coarse) and `/health/detailed` (authenticated) | route code; detailed content tested via `health_report` |

## Partial
- **HTTP layer.** FastAPI could not be installed (the package index returns 403 here). Routes, auth dependencies and 401/403 behaviour were never executed through real FastAPI. A hand-written stand-in was used during development and is not part of the repository and is not evidence of correct FastAPI behaviour. `tests/test_api_testclient.py` is written and is reported as **skipped**.
- **PostgreSQL driver.** A purpose-written client (SCRAM-SHA-256, extended query). Verified against PG 16 by this suite only. Production should use psycopg 3.
- **Crash recovery.** One real-kill scenario (death after exchange accept). Death during a database transaction, and power loss, are covered only by transaction rollback tests.
- **Failure injection.** Fake exchange timeouts/500s, DB unreachable at startup/execute, worker exceptions, reconciliation read failure, credential decrypt failure, fingerprint mismatch. No real network fault injection or database kill during a run.
- **Positions.** Spot, long-only. Futures are rejected as NOT_IMPLEMENTED.
- **Balance reconciliation** works for adapters that implement `get_account_snapshot`; only fakes and the paper adapter do.

## Missing
- Live order adapters for Binance, Bybit and OKX (intentional in this phase; live stays DISABLED/BLOCKED).
- Docker-based test run (no Docker daemon available). The compose `test` profile is written, not executed.
- MFA, distributed rate limiting, dependency audit, external penetration test.
- A dependency lock file (index unreachable).

## What blocks production-grade paper trading
1. The HTTP layer is unverified. This is the main blocker.
2. Docker/compose path unverified.
3. Workers run only when the application is started with workers enabled; the default API process path wires them through the application lifecycle, but this has been exercised with fake adapters and no real market data.

## What to fix first
Install FastAPI/httpx in a networked environment and run `tests/test_api_testclient.py` (extend it to expired/revoked sessions, 401 vs 403 for each role). Then run `docker compose --profile test run --rm tests`.
