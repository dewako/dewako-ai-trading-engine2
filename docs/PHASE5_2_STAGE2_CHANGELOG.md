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
Local: SQLite 522 passed / 0 failed / 39 skipped; PG16 525 passed / 0 failed / 36 skipped; mutation check (sizing returns the unscaled quantity) fails 9 tests. Hosted CI: run 41438701345 on `fa1c5f07` (WP5 + WP4 + WP1), all 9 jobs green; PG16 561 tests, 0 skipped, gate 53/53; SQLite 561 tests, 3 allowed skips, gate 41/41.

## WP2 - Reliable stop-loss and take-profit exit handling (branch `stage2-wp2-exit-spread`, stacked on WP1)

Stage 1.1 had already implemented the policy (separate exit-spread tolerance `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT`, deferral with a CRITICAL incident, entries blocked, no fabricated fill). WP2 adds what the Stage 2 brief still required: the test matrix and the documentation. **No application code changed.**

- Tests (`test_stage2_wp2_exit_spread.py`, 10 tests, 7 sub-cases): normal, abnormal and extreme spreads; the boundary just inside and outside the tolerance; no tolerance configured; take-profit; malformed quotes (crossed, zero, negative, NaN, infinite, missing bid/ask: no order, no guessed price, entries blocked); a gap through the stop fills at the quoted bid (not the stop price); nothing closed before the fill; recovery clears the incident once and entries resume. All 7 malformed-quote cases already failed closed: no defect found.
- Docs: `docs/PAPER_VS_LIVE.md` now has "Stop-loss and take-profit exits: paper behaviour versus real-market guarantees".
- Mutation check: ignoring the tolerance in the exit monitor fails 5 tests.

Decision recorded, not changed: the exit tolerance applies to every trigger including take-profit and time stop. A less urgent trigger could reasonably wait for a tighter spread; that would be a policy change and is not made here.
Evidence: local SQLite 532 passed / PG16 535 passed, 0 failed; hosted CI run 41445224976 (WP5 + WP4 + WP1 + WP2), all 9 jobs green on `35f1c921`; PG16 571 tests, 0 skipped, gate 56/56; SQLite 571 tests, 3 allowed skips, gate 44/44.

## WP2b - Stop-loss and take-profit exit policy split (branch `stage2-wp2b-stop-immediate`, stacked on WP2) - POLICY CHANGE, requested by the owner

Owner decision: a stop-loss exits at market with zero wait; a take-profit keeps the bounded tolerance and may wait for a favourable spread.

Behaviour change (this REVERSES the Stage 1.1 rule that a stop in a wide spread is deferred):
- **STOP:** never deferred for spread. `ExitMonitor` sends it at once; `ExecutionEngine.execute(..., spread_exempt=True)` (SELL EXIT only) sets `QuoteGuardConfig.enforce_spread=False` for that one order. Audited as `EMERGENCY_EXIT_WIDE_SPREAD` with `policy=STOP_LOSS_IMMEDIATE`, `emergency_limit=None`. No `EXIT_BLOCKED_WIDE_SPREAD` incident can arise for a stop.
- **TAKE_PROFIT and MAX_HOLD (time stop):** unchanged (normal limit, then `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT`, else deferred with a CRITICAL incident). The time stop was not named in the decision; it was left on the existing tolerance rules.
- Still enforced for every exit, stops included: quote present, positive, finite, uncrossed and fresh. Unchanged: entries, the normal spread limit, price-deviation limit.
- Env var name `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT` is kept for compatibility although it no longer applies to the emergency (stop) exit; documented in `.env.example`, `Config` and `PAPER_VS_LIVE.md`. A rename is a possible later clean-up.

Risk accepted with this policy (documented in `PAPER_VS_LIVE.md`): the stop triggers on the bid, and the spread check no longer filters a bad quote for stops, so a single bogus low bid tick (feed glitch) that is fresh, positive and uncrossed will sell the position at that bid. An optional hard ceiling on stop spreads was NOT added (it would contradict "zero wait"); it is a possible later safeguard.

Tests: 11 tests failed when the policy changed because they encoded the old rule for STOP (5 in `test_stage1_1_p1.py`, 6 in `test_stage2_wp2_exit_spread.py`). They were moved to a take-profit trigger with the same assertions (6 Stage 1.1 tests converted, including one that still passed; one existing take-profit test became identical to a converted one and was removed, 29 -> 28; the 6 WP2 tests were converted). New `StopLossIsNeverDeferredForSpread` class (6 tests) covers 0.1 %-40 % spreads with no tolerance, audit record, tolerance not delaying a stop, no double sell, entries not blocked after, stale quote still blocks. Mutation checks: restoring stop deferral fails 7 tests; making take-profit immediate fails 11.
Files: `execution/exit_monitor.py`, `execution/engine.py`, `execution/validation.py`, config comment, `.env.example`, docs, CI `--require` names (one renamed, three added).
Evidence: local SQLite 549 passed / PG16 552 passed, 0 failed. Hosted CI run 41594490033 (WP5 + WP4 + WP1 + WP2 + WP2b + WP2c), all 9 jobs green on `8413da2a`: PG16 588 tests, 0 skipped, gate 62/62; SQLite 588 tests, 3 allowed skips, gate 50/50. The first run (41593881022) failed only the skip gate: three required test names had been renamed; the requirement list was updated to the new names (none dropped).

## WP2c - Exit policy: stop-loss at once, take-profit waits, time stop like take-profit (branch `stage2-wp2c-exit-policy`, stacked on WP2b)

Your decisions (2026-11-03): decouple stop-loss from take-profit; protect capital first. Built on WP2b (stop-loss never deferred for spread).

- **Stop-loss:** sent at once through any spread up to the suspect-quote limit. A spread above `DEWAKO_STOP_SUSPECT_SPREAD_PCT` (default 10 %, `off` disables) must persist for `DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES` consecutive cycles (default 3; 1 = no wait) before the stop is sent, because such a spread is far more likely a bad quote than a market; selling on a glitch price is exactly the damage a stop-loss exists to prevent. The hold is loud (CRITICAL incident, entries blocked, worker unhealthy, `EXIT_STOP_QUOTE_SUSPECT` audited). If the quote returns to normal first, the stop sells at the normal quote.
- **Take-profit and time stop:** wait for a fair spread (normal limit, then the optional tolerance). A wait is audited (`EXIT_WAITING_WIDE_SPREAD`, once per streak) and reported in the monitor report (`waiting`), but opens NO incident, keeps the worker healthy and does not block new entries, because the stop-loss on the same position is still enforced every cycle.
- Changed tests (deliberately, to the new policy; assertions are not weaker): the Stage 1.1 wide-spread tests now assert waiting-without-incident for take-profit; the WP2/WP2b spread-matrix tests were rewritten for the final policy; new classes `SuspectStopQuote` (hold, confirm, glitch clears, restart of streak, configurable, invalid values) and `ConfigurationPath` (defaults, env, refuse-to-start, wiring).
- Mutation checks: removing the suspect hold fails 6 tests; making a take-profit wait raise an unverified-protection event again fails 5.
- New settings are in `.env.example` and `docs/PAPER_VS_LIVE.md`.

Trade-offs you accepted or should re-check: a real flash crash also shows a wide spread, so a genuine stop can be delayed by up to two cycles (about 10 s at the default interval) when the spread is above 10 %; BTC/USDT's normal spread is a tiny fraction of a percent, so 10 % is already an extreme reading. Phase 9 note: all of these limits are human-set configuration; nothing here lets an AI change them.
Evidence: local SQLite 549 passed / PG16 552 passed, 0 failed. Hosted CI run 41594490033 (WP5 + WP4 + WP1 + WP2 + WP2b + WP2c), all 9 jobs green on `8413da2a`: PG16 588 tests, 0 skipped, gate 62/62; SQLite 588 tests, 3 allowed skips, gate 50/50. The first run (41593881022) failed only the skip gate: three required test names had been renamed; the requirement list was updated to the new names (none dropped).

## WP6 - Audit reliability: an audit failure is never silent and never changes what a trade does (branch `stage2-wp6-audit-reliability`, stacked on WP2c)

Problem found: audit writes in the order, exit, alert, AI and startup paths sat in bare `try/except: pass` (or could raise into trading code). A storage problem either hid evidence silently or could interrupt a trade.

- `AuditLog.record_safely()` replaces those calls. On failure the event is HELD in memory with the original exception (class, message), logged at CRITICAL, and counted. It is written later, in original order, exactly once (`audit_event_id` de-duplication), marked `late_write` with `original_failure`, then a durable `AUDIT_PERSISTENCE_RECOVERED` row records counts (never content). The next successful safe write and the `health_monitor` worker both flush.
- Bounded buffer (1000). On overflow the oldest events are dropped and counted as `lost`; the recovery marker says so, and the state stays DEGRADED until a named human calls `acknowledge_lost(actor, reason)` (audited `AUDIT_LOSS_ACKNOWLEDGED`).
- While degraded: new risk-increasing orders are blocked (`ExecutionEngine._entry_protection_refusals`), exits are never blocked, `/ready` reports `SYSTEM_DEGRADED`, `/health/detailed` shows `audit_persistence`.
- Unchanged by design: audit writes inside the caller's transaction stay atomic with the change (they roll back together).
- Tests: `tests/test_stage2_wp6_audit.py`, 17 tests. Covered: held/flush/order/once, flush failing again, overflow + acknowledgement, shared state per database, in-transaction rollback; fill still applied after an audit failure at ORDER_SUBMITTED; reconciliation still flagged after an audit failure at ORDER_UNKNOWN; wide-spread stop still sent after an audit failure; entries blocked / exits allowed / recovery reopens; readiness, health and the health worker; startup failure held; AI request failure held.
- Mutation checks (each fails a test, code restored): entries not blocked while degraded; readiness ignoring audit state; health worker not flushing; flush without de-duplication.
- Known limits (not hidden): held events live in memory only, so a crash while degraded loses them (the CRITICAL log line is then the only trace); a database-wide outage also blocks order creation, which is the intended fail-closed outcome; paper trading only.
- Evidence (local): SQLite 566 passed / PG16 569 passed, 0 failed; ruff real-error check clean. Hosted CI result is recorded below once the run finishes.
