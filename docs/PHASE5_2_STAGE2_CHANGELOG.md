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
Evidence: hosted CI run 41413833110 on `0ae200a4` (covers WP5 + WP4), all 9 jobs green, PG16 546 tests, 0 skipped, gate 50/50; SQLite 546 tests, 3 allowed skips, gate 38/38; local SQLite 507 passed, PG16 510 passed, 0 failed.

## WP1 - Executable-price-aware risk sizing (branch `stage2-wp1-risk-sizing`, stacked on WP4)

Moved into the Risk Engine, as the Stage 2 brief requires (the Stage 1.1 version lived in the execution layer):
- `risk/sizing.py` (new, pure arithmetic): `executable_entry_quantity` scales the approved quantity by (reference - stop) / (executable - stop), never up; every invalid or inconsistent input raises `SizingError`.
- `RiskEngine.size_for_executable_entry(decision, executable_price=...)`: final sizing approval from the executable price (the execution engine supplies the validated quote; the Risk Engine does no I/O). Returns a refused decision with reasons instead of raising; audited as `executable_sizing`.
- `RiskEngine.final_entry_problem(...)`: the last gate before an order exists. After exchange rounding, the quantity must not exceed the approved quantity and its loss at the stop must not exceed the approved loss. Found and fixed during testing: the first version let NaN/infinity through (every comparison with NaN is False); all figures are now checked finite first.
- `ExecutionEngine` takes an optional `risk_engine` and calls both methods for BUY ENTRY only; exits are untouched. `execution/validation.executable_risk_quantity` remains as a thin compatible wrapper.
- Behaviour is unchanged for valid entries (all 507 existing tests pass unmodified); the new guarantee is the final gate and the fail-closed handling.

Already handled elsewhere and verified, not duplicated: quote staleness / invalid / unavailable quotes (quote guard), quantity step rounding (rounds DOWN), minimum/maximum quantity and minimum notional (`validate_order`, from the exchange's market rules), hard exposure/position caps under the reservation lock.

**95 % headroom (`SIZING_HEADROOM`) - reassessment, decision: KEEP, document.** Grid evidence (`test_loss_at_the_stop_and_notional_never_exceed_what_was_approved`, over 4 reference prices x 41 ask premiums x 5 stop distances): after scaling, the notional at the executable price is never above the notional the Risk Engine sized from the reference price, so the ask premium alone can no longer breach the position cap; the headroom's original reason is now covered by the scaling. It is not removed because (a) removing it changes sizing for every entry and several verified limit tests rely on it, and (b) it still gives a 5 % margin for fees and balance rounding. Removing it is a separate, explicit change if wanted.

Known limitations: `RiskEngine.audit` is an in-memory list; the engine's instance grows by one record per entry for the life of the process (same pattern as the bot runner's instance). Equity-based position caps are still enforced by the reservation backstop at the executable price, not inside `size_for_executable_entry`.

Files: `risk/sizing.py` (new), `risk/engine.py`, `execution/engine.py`, `execution/validation.py`; tests `test_stage2_wp1_risk_sizing.py` (15); CI `--require` +3 names.
Local: SQLite 522 passed / 0 failed / 39 skipped; PG16 525 passed / 0 failed / 36 skipped; mutation check (sizing returns the unscaled quantity) fails 9 tests. Hosted CI: pending.
