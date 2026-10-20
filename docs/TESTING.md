# Running the tests

## Local, no Docker (what the author used)
```bash
scripts/pg_test_cluster.sh start        # throw-away PostgreSQL 16 on 127.0.0.1:55432 (needs postgresql-16 server binaries; root or postgres user)
cd backend/tests
python -m pytest -q .                                                     # SQLite
DEWAKO_TEST_DB=postgres DEWAKO_EXPECT_PG_MAJOR=16 DEWAKO_TEST_PG_URL=postgresql://dewako:dewako_test_pw@127.0.0.1:55432/dewako python -m pytest -q .   # PostgreSQL
scripts/pg_test_cluster.sh stop
```
Each PostgreSQL test runs in its own schema (`?schema=`), dropped at exit. With `DEWAKO_TEST_DB=postgres`, `tmp_db()` reads `DEWAKO_TEST_PG_URL` directly: a missing or unreachable server raises, it never falls back to SQLite. `test_pg_is_really_postgresql` and `test_the_http_job_is_really_running_on_the_requested_backend` prove the backend identity; `DEWAKO_EXPECT_PG_MAJOR=16` also pins the server major version.
Run from `backend/tests`. `pip install -r backend/requirements.txt` provides pytest, fastapi and httpx. With `DEWAKO_REQUIRE_HTTP_TESTS=1` a missing FastAPI is a failure, not a skip.

## Bounded crash tests
`tests/bounded.py`: child processes get their own process group, a hard timeout and a group SIGKILL in `finally`; a failing/hung worker's error carries its command, crash point, exit status and stdout/stderr tails. Each crash test also has a SIGALRM watchdog (`DEWAKO_TEST_TIMEOUT`, default 240 s) that dumps thread stacks. Pytest additionally honours the CI job timeout (30 min).

## CI policy (`scripts/assert_no_skips.py`)
`assert_no_skips.py REPORT.xml --min-tests N [--allow-skip text] [--require test_name]...` fails on any failure, any skip not explicitly allowed, too few tests, or a required test that is absent/skipped. PostgreSQL job: zero skips and the identity test, both HTTP regressions and the nine crash boundaries are required. SQLite job: only "PostgreSQL-only" skips allowed.

## Docker (not executed by the author: no Docker daemon)
```bash
docker compose -f docker-compose.test.yml run --rm tests
```
Starts an in-memory PostgreSQL 16 and runs the suite with `DEWAKO_TEST_DB=postgres`. Only `docker compose config` validation was run. (A previous revision merged the postgres service into the tests service, which would have dropped the test environment variables; fixed in Phase 3.4.)

## What the tests do not use
No real exchange, no network, no real money. Exchanges are in-process fakes; the crash tests kill real child processes against a file-backed fake exchange.

## Order-dependence and restart checks (manual, Phase 3.4)
```bash
python scripts/crash_order_experiment.py <forward|reverse|shuffle> <repetitions> <seed>      # one process; set DEWAKO_TEST_DB / DEWAKO_TEST_PG_URL for PostgreSQL
DEWAKO_TEST_PG_URL=... python scripts/pg_restart_check.py "scripts/pg_test_cluster.sh restart"   # restarts the server: never run against shared infrastructure
```
Without pytest installed, `scripts/run_tests_standalone.py` needs a pytest shim on PYTHONPATH (not shipped); it is NOT a replacement for pytest in CI.
PostgreSQL waits are bounded server-side: `DEWAKO_PG_LOCK_TIMEOUT_MS` (20000), `DEWAKO_PG_STATEMENT_TIMEOUT_MS` (60000), `DEWAKO_PG_IDLE_TX_TIMEOUT_MS` (120000).

## Cross-platform process cleanup (Windows and POSIX) - `tests/bounded.py`
- `run_process()` runs a child and kills its whole tree on timeout or in `finally`. POSIX: own session + `os.killpg(SIGKILL)`. Windows: a Job Object with kill-on-close (`TerminateJobObject`), `taskkill /T /F /PID <that pid>` as fallback. Nothing is killed by name or pattern. Output is collected with `communicate(timeout=)` (no pipe deadlock). A hung child raises `WorkerHung` with pid, elapsed time and stdout/stderr tails.
- Windows limitation: a descendant spawned in the microseconds between CreateProcess and the job assignment would escape the job (the workers spawn nothing at start-up; `taskkill /T` is the second line of defence). Nested jobs need Windows 8 / Server 2012 or newer.
- `hard_kill_self()` / `KILLED_RC`: abrupt self-termination used by the crash workers. POSIX `SIGKILL` (exit status -9); Windows `os.kill(pid, 9)` = `TerminateProcess` (exit status 9; no `finally`, no `atexit`).
- `watchdog()` guarantees (and does not guarantee): POSIX main thread = it interrupts the blocked call; Windows / non-main thread = it cannot interrupt anything, it dumps all stacks at the deadline and fails the test when the body returns late. It never stops a body that never returns; hard bounds come from `run_process()`/`run_isolated()` and database-side timeouts.
- Liveness is checked by pid through the OS (`pid_alive`), not `pgrep`. Tests: `test_process_cleanup.py`.
- Windows: if pytest's `faulthandler_timeout` is configured it competes with the diagnostic watchdog (faulthandler allows one pending dump); do not set it for these tests.

Process identity (Windows): `WorkerHung.pid` is the launched process; with a venv `python.exe` it is a launcher stub and the interpreter is its child. Use `WorkerHung.tree` for descendants. Windows children start suspended, are placed in the Job Object, then resumed. The diagnostic watchdog uses a per-call timer thread (no global state).
