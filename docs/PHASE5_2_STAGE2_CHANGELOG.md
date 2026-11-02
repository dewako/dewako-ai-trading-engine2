# Phase 5.2 Stage 2 - Changelog

Paper trading only. Live trading stays LOCKED. Status of Stage 2 as a whole: **IN PROGRESS** (not passed). Each work package is recorded here with its evidence; nothing is counted done without a hosted CI run on a recorded SHA.

## WP5 - Time-based exit protection (branch `stage2-wp5-time-stop`)

Behaviour:
- The time stop stays DISABLED by default. The effective setting is now stored in `safety_state` (key `time_stop_config`) in the same transaction as its audit record.
- First start stores the configured value (or "disabled"). Unchanged restart: no change, no extra audit row.
- A different configuration on restart **while any position is not CLOSED** is refused: the stored value stays in force, `TIME_STOP_CONFIG_REFUSED` is audited, and `/risk/limits` shows `pending_change_refused`. With nothing open the change is adopted and audited (`TIME_STOP_CONFIG_CHANGED`).
- `/risk/limits` `controls.time_stop` reports `NOT_ENFORCED` with a reason when disabled, `ENFORCED` with `max_hold_seconds` when enabled.
- Invalid values still refuse to start (unchanged from Stage 1.1). The exit reason `MAX_HOLD` is recorded on the exit signal (unchanged).

Consequence to know: to change the time stop you must restart with no open positions. This is deliberate, and there is no runtime API to change it in this work package.

Files: `runtime/time_stop.py` (new), `runtime/application.py`, `risk/controls.py`, `api/main.py`, tests `test_stage2_wp5_time_stop.py` (8) and one assertion added to `test_stage1_http.py`; CI `--require` list +3 names.

Evidence so far (local, not hosted): SQLite 499 passed / 0 failed / 39 skipped (36 HTTP not runnable in the sandbox + 3 PG-only); PostgreSQL 16 502 passed / 0 failed / 36 skipped (all HTTP). Mutation check: removing the open-position guard makes the two restart-refusal tests fail. Hosted CI: pending.

## WP4 - Worker health and public readiness (branch `stage2-wp4-heartbeat-readiness`, stacked on WP5)

Correction to the Stage 2 plan: heartbeat freshness WAS already part of readiness (`WorkerSupervisor.status()` marks a heartbeat older than 3 x interval + 5 s as FAILED). The real defect was different: `run_once` wrote HEALTHY (with a heartbeat) at the **start** of every iteration, so a critical worker that had never completed an iteration in this process counted as healthy, and `/health` could report READY before the exit monitor had proven anything.

Behaviour now:
- A worker stays `STARTING` until an iteration has completed in this process; after failures it stays `DEGRADED` until an iteration succeeds. Only a completed iteration earns `HEALTHY`.
- Consequence: `/health` is 503 `SYSTEM_DEGRADED` until every critical worker has completed its first iteration after a start or restart (normally within the first cycle). A healthy-looking row left by a previous process never counts.
- Unchanged: critical vs non-critical (`worker_health.critical`), heartbeat expiry, `/health/live` independent of readiness, `/health/detailed` content.

Files: `runtime/workers.py` (+ tests `test_stage2_wp4_readiness.py`, 8 tests; CI `--require` +3 names).
Evidence so far: see the run recorded below once hosted CI completes.
