# Phase 3.4 final hardening audit (latest results)

**Status: PHASE 3 INCOMPLETE. Live trading: LOCKED. Exchange adapters: all UNVERIFIED.**
Earlier records are preserved: `PHASE2_*`, `PHASE3_2_*`, `PHASE3_3_*`, and `PHASE3_4_HARDENING_AUDIT.md` (the first Phase 3.4 pass, whose results are superseded by the numbers below). Evidence files: `docs/evidence/`.
Environment: Linux container, Python 3.13, PostgreSQL 16.15 (local server on 127.0.0.1:55432). No Docker daemon, no GitHub runner, package index unreachable (no pytest, no FastAPI), no outbound exchange access.

## What this pass did, and what it could not do
| Request | Outcome |
|---|---|
| Reproduce and root-cause the order-dependent crash-suite hang | **NOT REPRODUCED. Root cause NOT ESTABLISHED.** See section 1. |
| Real PostgreSQL 16 integration | PARTIAL: full suite and crash suite pass on real PG 16.15 (below). Restart recovery checked once. No Docker/CI run. |
| CI repair | Statically repaired and validated (YAML parses, report/test names exist). **NOT RUN.** |
| Docker build/test | **NOT VERIFIED** (no daemon). Compose files fixed and checked with `docker compose config`. |
| Section 6: real exchange execution/reconciliation (Binance/Bybit/OKX) | **NOT IMPLEMENTED in this pass.** It needs exchange network access and testnet credentials that this environment does not have; building it blind would produce exactly the unverified code the prompt forbids. All real adapters remain UNVERIFIED and engine-locked. |
| Section 7: readiness gate and report | IMPLEMENTED and tested (service level): readiness states, human-ADMIN testnet attestation required before live can be enabled, no automatic live state. |
| Section 8: security review | PARTIAL: confirmed findings fixed (below); no claim of complete review. |

## 1. Crash/recovery hang
- Original problem (reported): the complete crash/recovery sequence hangs order-dependently; DB-outage and durable-incident tests pass alone.
- Reproduction attempts (commands and results in `docs/evidence/crash_order_experiments_2026-10-15.txt`): one process, forward, reversed and shuffled orders, repeated 2-3 times, SQLite and PostgreSQL 16.15, plus fresh-process runs. 0 hangs, 0 failures, `threading.active_count()==1` after every repetition, no leftover `sim_worker`/`crash_worker` processes. Last-completed-test/timing data for a hang therefore does not exist; I did not see one.
- Candidate causes found by code review (unconfirmed as the cause of your hang):
  1. Unbounded waits: PostgreSQL advisory/row locks had no server-side limit (only a 30 s socket timeout). **Fixed**: connection startup now sets `lock_timeout` (20 s), `statement_timeout` (60 s), `idle_in_transaction_session_timeout` (120 s), env-overridable (`pgwire.py`). Regression: `test_pg_a_held_lock_fails_closed_within_a_bound_instead_of_hanging` (held chain lock -> audit write fails in ~1.5 s, no half-written row, recovers when released).
  2. `crash_worker` ran with `subprocess.run(timeout=)` (kills only the direct child). **Fixed** (`tests/bounded.py` process-group kill).
  3. No bound or diagnostics on in-process waits. **Fixed**: SIGALRM watchdog with stack dump on every crash test; failure messages include command, crash point, exit status, stdout/stderr tails.
  4. `pgrep -f` cleanup check matched its own command line (found because my new test failed). **Fixed** with a unique marker.
  5. `socket.create_connection` DNS resolution is not covered by the connect timeout (getaddrinfo can block); NOT FIXED, documented.
- Not run: `python -m pytest` ordering plugins (pytest unavailable), so pytest-specific fixture interaction in your environment is untested. If the hang recurs, the watchdog now names the blocked line.
- Acceptance: "the suite completes without hanging" is met in this environment only; per the prompt, an unreproduced hang is reported as **unresolved**.

## 2. PostgreSQL 16
| Item | Result |
|---|---|
| Full suite on PG 16.15 | `run_tests_standalone.py`, `DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16`: **252 passed, 0 failed, 22 skipped** (all 22 are HTTP tests needing FastAPI), exit 0, 63 s |
| Server identity | `test_pg_is_really_postgresql` asserts "PostgreSQL" and major 16; tmp_db() cannot fall back to SQLite |
| Migrations | clean + upgrade tested (`test_config_and_migrations.py`); advisory-locked; repeated-run idempotent |
| Concurrency/idempotency | independent-connection race tests pass |
| DB outage during safety writes | `test_9b` (startup fails closed, nothing sent), lifecycle tests; held-lock test above |
| Restart/reconnect | `scripts/pg_restart_check.py`: immediate restart; open tx not committed; next write reconnected; chain verifies. One scenario only (`docs/evidence/pg_restart_check_*.txt`) |
| Custom wire client | retained (no driver installable). Tests: auth, transactions, parameter binding, error paths via suite. Not reviewed for TLS verification behaviour beyond the code path; NOT independently audited. Blocks live |
| Credentials in logs | errors report host:port and class only (`PGConnectionError`); no URL is logged |

## 3. CI and Docker
- `ci.yml` jobs: static (compile, ruff errors, secret scan, pip-audit), sqlite (3.12/3.13, HTTP required, only PG-only skips allowed), postgres16 (service postgres:16, zero skips, named required tests), crash-repeat (3 fresh runs per backend), docker (compose test file + image build). Job and step timeouts set; no `continue-on-error`; least-privilege `contents: read`.
- Status: **NOT RUN** on a runner. Static validation only. The sqlite/pg jobs invoke real `pytest`, which I could not run here; they use my custom runner nowhere.
- Found and fixed: `docker-compose.yml` had merged the postgres service into `tests` (duplicate `environment`, silent-SQLite risk). Tests moved to `docker-compose.test.yml`; `docker compose config` passes for both files.
- Dependencies are lower-bounded, not pinned (`requirements.txt`); no lock file because the index was unreachable. Base image `python:3.12-slim` and `postgres:16` are floating tags. **Unresolved.**

## 4. Live-readiness gate (section 7)
- `SafetyController.readiness_state()` -> LOCKED / NOT_READY / PAPER_VERIFIED / TESTNET_VERIFIED / LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION. No `LIVE_ENABLED` state. Hard faults (kill switch, unhealthy state, open incident, audit not OK) force NOT_READY.
- `record_evidence()`: human ADMIN only, written note required, audited, never automatic. It is an attestation, not proof.
- `activation_readiness()` now also requires TESTNET_VERIFIED attestation. Tests: `test_live_activation.py` (9, executed on SQLite and PG).
- Not implemented: exchange-side protective-order verification, account-identity verification against a real exchange, candle-level freshness, per-strategy/daily-loss limits audit (existing risk engine unchanged). These remain gaps; see the readiness table.

## 5. Results (custom runner `scripts/run_tests_standalone.py` + pytest shim; `python -m pytest` NOT run)
| Category | Command | Exit | Passed | Failed | Skipped |
|---|---|---|---|---|---|
| SQLite, all | `cd backend/tests && run_tests_standalone.py` | 0 | 249 | 0 | 25 (22 HTTP, 3 PG-only) |
| PostgreSQL 16.15, all | + `DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16 DEWAKO_TEST_PG_URL=...` | 0 | 252 | 0 | 22 (HTTP) |
| Route stand-in (not HTTP) | `standin_34.py` | 0 | 14 | 0 | 0 |
| HTTP (22 tests) | pytest + FastAPI | not run | | | NOT EXECUTED |
Also: crash/recovery order experiments above; PG restart check PASS.

## 6. Tests added this pass
`test_hung_worker_failure_carries_diagnostics_and_leaves_no_group_behind`, `test_in_process_watchdog_turns_a_stuck_wait_into_a_failure_with_stacks`, `test_pg_a_held_lock_fails_closed_within_a_bound_instead_of_hanging`, `test_testnet_evidence_is_required_attested_by_a_human_admin_and_never_automatic`, `test_readiness_states_and_there_is_no_automatic_live_state` (all executed); HTTP: `test_logout_requires_authentication_and_is_not_a_public_route`, `test_logout_revokes_only_the_presented_session_and_cannot_be_replayed`, `test_enable_live_returns_409_with_reasons_and_never_claims_live_is_active`, `test_enable_live_stays_refused_with_real_adapters_registered`, `test_the_http_job_is_really_running_on_the_requested_backend` (NOT EXECUTED).

## 7. Remaining blockers / follow-up
Real HTTP tests; real CI and Docker runs; hang cause unconfirmed; no exchange execution, lookup, fills, balances or protective-order verification for any real exchange (UNVERIFIED, no testnet evidence); custom PG driver; unpinned dependencies/base images; no MFA; audit head not anchored off-host; env-held encryption key without KMS; DNS-resolution timeout; candle-level freshness; PG restart tested in one scenario only.


## Addendum 2026-10-20: Windows crash/recovery repair
User run (Windows, Python 3.14.2, pytest 9.1.1): 29 passed (API + activation), 250 passed (everything except the two crash modules), `test_crash_matrix.py` 14 failed in 43.55 s.
| Finding | Root cause | Fix | Verified |
|---|---|---|---|
| `os.killpg` AttributeError | POSIX-only cleanup in `bounded.run_process`; `signal.SIGKILL` and `pgrep` also POSIX-only (workers, 3 tests) | Cross-platform `bounded.py`: POSIX killpg; Windows Job Object (kill-on-close) + `taskkill /T /F /PID` fallback; `hard_kill_self()`/`KILLED_RC`; pid-based liveness | Linux: yes. **Windows: NOT VERIFIED** |
| "watchdog did not fire" | SIGALRM does not exist on Windows; a thread cannot interrupt the main thread; the test assumed interruption | Precise two-mode watchdog (interrupting on POSIX main thread; diagnostic on Windows/threads: stack dump, then fail when the body returns late; it never claims to stop a stuck body). Hard timeouts via `run_isolated()` | Linux: both modes tested (diagnostic mode via a non-main thread). Windows: NOT VERIFIED |
Also fixed: the standalone runner returned exit 0 on failures (now non-zero); its hard-coded paths are relative.
New tests (`test_process_cleanup.py`, 9): hang -> diagnostics + whole tree dead (child and grandchild pids checked through the OS), normal exit, non-zero/abrupt exit statuses, 5 MB output on both pipes (no deadlock), unrelated bystander process survives, hard timeout through isolation, watchdog guarantee per mode, fast body and nested outer deadline, diagnostic mode exercised on every platform. Two POSIX-only harness tests in `test_crash_matrix.py` were replaced by these stronger cross-platform ones; no crash scenario was skipped, weakened or deleted. The expected-hang test (`test_hung_worker_is_killed_by_the_controller_timeout`) now asserts the killed pid is dead instead of using `pgrep`.
Scenario audit: each crash test still kills a real worker at its named boundary (exit status must equal `KILLED_RC`, otherwise the test fails with stdout/stderr), then runs a fresh worker and asserts DB state, exchange state and `check_invariants`. The kill mechanism on Windows (`TerminateProcess`) is equivalent for these purposes (no `finally`, no `atexit`); Windows file-handle and SQLite-locking behaviour after such a kill is UNVERIFIED.
Linux results after the change (`scripts/run_tests_standalone.py`, exit 0): SQLite 256 passed / 0 failed / 25 skipped (22 HTTP, 3 PG-only), 42 s; PostgreSQL 16.15 259 passed / 0 failed / 22 skipped (HTTP), 79 s; `test_crash_matrix` 3 repeats 12/12, ~14 s each; no leftover worker processes. CI: new `windows-crash` job (3.13, 3.14; SQLite only) NOT RUN.
Phase 3 remains INCOMPLETE.


## Addendum 2026-10-20 (2): remaining Windows failures (3 of 278 on Windows 3.14.2 / pytest 9.1.1)
| Failure | Root cause (from code + the reported values) | Fix | Verified |
|---|---|---|---|
| PID mismatch (`PIDS 15268 19324` vs `WorkerHung.pid` 17600) | `WorkerHung.pid` is the process LAUNCHED. On a Windows venv `python.exe` is a launcher stub that starts the real interpreter as a CHILD, so the pid the interpreter prints (15268) differs from the launched pid (17600). The assertion `child == e.pid` encoded a POSIX-only assumption; a mismatch did not mean a survivor. A real risk was hiding behind it: the job assignment happened AFTER the stub had possibly spawned its child, which would then escape the job. | Windows start is now CREATE_SUSPENDED -> Job Object (kill-on-close) -> NtResumeProcess, so no descendant can exist outside the job. `WorkerHung.tree` = every descendant alive at the kill (ToolHelp snapshot on Windows, /proc or `ps` on POSIX). Cleanup is VERIFIED: every pid of {launched}+tree must be dead within 10 s, one per-pid retry, else `CleanupError`; idempotent. Test now asserts `child == e.pid or child in e.tree`, `grandchild in e.tree`, and all of them dead; a new test reproduces the stub layout on any platform | Linux yes (incl. stub-layout test, mutation: removing the snapshot fails 2 tests). **Windows: NOT VERIFIED** |
| `(none: nested watchdog)` x2 | My diagnostic mode used `faulthandler.dump_traceback_later`, which allows one pending dump per process, guarded by a GLOBAL flag. The outer `bound_all` wrapper (also diagnostic on Windows) held it, so inner watchdogs captured nothing. | Replaced by a per-call `threading.Timer` that snapshots `sys._current_frames()` with `threading.enumerate()` names at the deadline; no shared state, nesting safe. Output: `Thread <name> (ident=N):` + 25 frames incl. `in late`/`in stuck`. Wording unchanged: "NOT interrupted ... returned late". Hard timeouts remain `run_process`/`run_isolated` | Linux yes (main-thread, worker-thread and nested cases, nested forced into diagnostic mode). Windows: NOT VERIFIED |
Known limit: if the interpreter is blocked in a C call that holds the GIL the timer cannot run; the failure message says so instead of inventing stacks. Mutation note: with only the launched process killed, the verified-cleanup retry still killed the survivors (the safety net works), so these tests prove the end state, not which layer did it.
Linux results (`scripts/run_tests_standalone.py`, exit 0): SQLite 259 passed / 0 failed / 25 skipped (22 HTTP, 3 PG-only), 50 s; PostgreSQL 16.15 262 passed / 0 failed / 22 skipped (HTTP), 83 s; the 3 PG-only tests executed and PASS on PG 16.15; `crash_matrix + process_cleanup` x3: 24 passed each, ~33 s, no leftover processes. HTTP tests still NOT EXECUTED (FastAPI unavailable); CI not run. Phase 3 remains INCOMPLETE; live trading LOCKED.
