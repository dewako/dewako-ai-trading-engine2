# Crash recovery

`ExecutionEngine.recover()` (also called by startup) resolves SUBMITTING/UNKNOWN/RECONCILING orders without ever resending.

| Scenario | Test | Result |
|---|---|---|
| A: order placed, process dies, restart | `test_crash_after_persisting_submitting_recovers_without_duplicate` | order recovered as FILLED, exchange saw 1 create call |
| B: timeout, then restart | `test_timeout_then_exchange_has_order_is_not_resent`, `test_inconclusive_reconcile_stays_reconciling_and_never_resends` | UNKNOWN -> RECONCILING -> resolved; no duplicate |
| C: exchange has position, local DB stale | `test_position_on_exchange_but_local_db_stale_blocks_trading` | MISMATCH, RECONCILIATION_REQUIRED, live BLOCKED, next order BLOCKED |
| D: kill switch then restart | `test_kill_switch_survives_restart_and_needs_admin_to_release` | still ON |
| E: paper restart | `test_paper_restart_keeps_cash_positions_and_orders` | cash and positions intact |

Scenario F (Phase 3.2): `test_sigkill_after_exchange_accepts_then_recovery_does_not_resend` sends an order from a **separate process** that is killed with SIGKILL by the exchange stub right after the exchange accepted the order. A fresh process finds the order in SUBMITTING, looks it up by client id, records FILLED, and the same signal is then a duplicate. Run on SQLite and PostgreSQL 16.
Scenario G: `test_fill_position_trade_are_atomic_and_recovery_completes_them`: failure between position and trade rolls all three back; `recover()` re-syncs the SUBMITTED order from the exchange and writes order state, position and trade together.

Not covered: death in the middle of a database transaction (relies on the database's own atomicity), power loss, and any real exchange. "Restart" in scenarios A-E means a new `Database`/engine object on the same database.

## Phase 3.3/3.4: process-level crash matrix (`backend/tests/test_crash_matrix.py`)
A controller launches `sim_worker.py` as a separate process, SIGKILLs it at a named boundary, then starts a fresh worker. The fake exchange keeps its state in a file. Run on SQLite and PostgreSQL 16.

| # | Required boundary | Test | Result (fake exchange) |
|---|---|---|---|
| 1 | before intent persistence | `test_1_crash_before_intent_commit` | PASS: no order, no reservation |
| 2 | after intent, before submission | `test_2_crash_after_intent_commit_before_submission` | PASS: no duplicate |
| 3 | during submission | `test_3_crash_during_submission_request_never_reached_exchange` | PASS |
| 4 | remote accepted, no local ack | `test_4_crash_after_exchange_accepts_before_local_ack` | PASS: found by client id, never resent |
| 5 | after a partial fill | `test_5_crash_after_partial_fill` | PASS: fills not lost or doubled |
| 6 | fill received, local commit missing | `test_6_crash_after_fill_received_before_local_transaction_commits` | PASS: atomic order/position/trade |
| 7 | during cancellation | `test_7_crash_during_cancellation` | PASS |
| 8 | during reconciliation | `test_8_crash_during_reconciliation` | PASS |
| 9 | DB outage / network timeout | `test_9a_...`, `test_9b_database_outage_fails_closed_and_sends_nothing` | PASS (simulated outage, not a real server death) |
| 10 | unrecoverable ambiguity | `test_10_...` | PASS: durable incident, live blocked |

Assertions covered: no duplicate orders; fills, fees and positions consistent; ambiguity stays UNKNOWN until reconciled; unknown state blocks the live gate; durable incident or safe completion. NOT TESTED: power loss, kernel panic, PostgreSQL server dying while connected, any real exchange.

### The reported hang (Phase 3.4)
Not reproduced: the matrix completed in ~20 s on SQLite (5 consecutive runs) and on PostgreSQL 16, with no leftover processes. Code review found unbounded waits where a hang could originate: `crash_worker` was run with `subprocess.run(timeout=)` which kills only the direct child; no bound on in-process waits (SQLite lock wait 30 s, PostgreSQL socket 30 s, advisory-lock waits); failures carried no child output. Fixes (`tests/bounded.py`): process-group SIGKILL in `finally`, diagnostics in the failure, and a SIGALRM watchdog with stack dump on every crash test. Tests: `test_hung_worker_failure_carries_diagnostics_and_leaves_no_group_behind`, `test_in_process_watchdog_turns_a_stuck_wait_into_a_failure_with_stacks`. Root cause of the user's hang remains UNCONFIRMED; if it recurs, the failure now names where it waited.

### Windows (Phase 3.4 follow-up)
Reported on Windows / Python 3.14.2 / pytest 9.1.1: all 14 crash-matrix tests failed with `AttributeError: module 'os' has no attribute 'killpg'` and one watchdog test failed ("watchdog did not fire"). Root causes: (1) `bounded.run_process` used POSIX-only `os.killpg`; the workers and two tests also used `signal.SIGKILL` and `pgrep`, which do not exist on Windows; (2) the SIGALRM watchdog is POSIX-only and silently did nothing on Windows, while the test assumed it interrupts. Fix: `tests/bounded.py` (see `docs/TESTING.md`). Verified on Linux only; **Windows: NOT VERIFIED by the author** (no Windows environment). Run `python -m pytest -q test_process_cleanup.py test_crash_matrix.py test_crash_and_recon.py` in `backend/tests` and report any failure.
Crash scenarios were not changed: the same nine boundaries, the same assertions on database state, exchange state and invariants; only the kill mechanism and the process cleanup were made portable.
