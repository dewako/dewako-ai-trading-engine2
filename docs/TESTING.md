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

## CI policy (`scripts/assert_no_skips.py`; updated 2026-11-13)
`assert_no_skips.py REPORT.xml --min-tests N [--allow-skip text] [--require test_name]... [--require-file FILE]...` fails on any failure,
any skip not explicitly allowed, too few tests, or a required test that is absent, skipped or failed. `scripts/test_assert_no_skips.py`
proves each rule turns a report red on its own (run in the `static` job).
Required tests are listed once, in `.github/required-tests/`: `common.txt` (every backend), `postgres-only.txt` (PostgreSQL 16 only),
`paper-qualification.txt` (the paper checklist). Renaming or deleting a listed test fails CI until the list is changed on purpose.

| Workflow / job | Runs | Policy |
|---|---|---|
| `ci.yml` static | compileall, ruff (errors), secret scan, skip-policy self-test, `node --check` of the dashboard JS | any failure |
| `ci.yml` sqlite 3.12 / 3.13 | full suite on SQLite | >= 870 tests; only "PostgreSQL-only" skips; `common.txt`; HTTP and Node UI tests must run |
| `ci.yml` postgres16 | full suite on PostgreSQL 16 | >= 870; zero skips; `common.txt` + `postgres-only.txt` |
| `ci.yml` crash-repeat | crash suites x3, fresh processes, SQLite + PG | >= 24 / 27 per repeat; 3 named crash tests |
| `ci.yml` windows-crash | crash suites x3 on SQLite, Python 3.13 (blocking) and 3.14 (informational) | >= 39 per repeat |
| `ci.yml` e2e-real-app | 16 browser checks against the real FastAPI app | all 16 recorded and passed; real server required |
| `ci.yml` docker | full suite in the test image against PostgreSQL 16 | >= 870; skips only "backend-only image" / "node is not installed"; `postgres-only.txt` |
| `ci.yml` compose-smoke | production image and compose file | see `scripts/compose_smoke.sh` |
| `security.yml` | pip-audit (pinned set, image, fresh install), gitleaks over history, image scan | findings fail; exceptions need a written reason |
| `paper-qualification.yml` | paper checklist on SQLite + PG16; bounded paper soak | nightly and manual; see `docs/GITHUB_CI_PAPER_TRADING_QUALIFICATION_PLAN.md` |
Every job keeps its JUnit (or log) as an artifact. `DEWAKO_REQUIRE_NODE=1` makes a missing Node a failure instead of a skip.
Replay (`backtesting/replay.py`) always uses a throw-away SQLite database by design, so the replay tests exercise SQLite even in the
PostgreSQL job; run records, the promotion gate and everything outside the replay run on both backends.

## Docker
```bash
docker compose -f docker-compose.test.yml run --rm tests      # test stage of the image + in-memory PostgreSQL 16; JUnit -> ci-out/
bash scripts/compose_smoke.sh                                   # production image + docker-compose.yml with a generated TEST .env
```
Both run in CI (`docker` and `compose-smoke` jobs). The production stage of `backend/Dockerfile` contains no tests.

## Paper soak (`backend/tests/paper_soak.py`)
```bash
cd backend/tests && PYTHONPATH=.. python paper_soak.py --cycles 3000 --restart-every 250 --report soak.json --junit soak.xml
```
The real Application with its workers and the PAPER adapter, restarts on the same database, invariants checked every cycle (see the
script header). Set `DEWAKO_TEST_DB=postgres` and `DEWAKO_TEST_PG_URL` for PostgreSQL.

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
