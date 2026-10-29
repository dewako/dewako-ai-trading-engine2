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

## Docker and PostgreSQL
```bash
docker compose -f docker-compose.test.yml run --build --rm tests
```
Builds the backend test image, waits for an isolated PostgreSQL 16 service to become healthy, and runs pytest with `DEWAKO_TEST_DB=postgres`. The backend-only build does not contain `frontend/` or `.github/`, so four repository-wide UI/CI checks explicitly skip; those checks run in the full-layout CI jobs. On 2026-10-28 this command completed with 403 passed, 4 skipped, 0 failed. The first run exposed a backend package path assumption in a safety invariant; the path was corrected without changing the assertion, and the rerun passed.

The default deployment compose uses SQLite. For the PostgreSQL deployment profile, create a local `.env` with test or operator-managed values for `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `DEWAKO_ENV=production`, and `DEWAKO_DATABASE_URL=postgresql://...`, then run:
```bash
docker compose --profile postgres-port -f docker-compose.yml -f docker-compose.postgres.yml up --build -d backend postgres redis
```
The PostgreSQL service has a `pg_isready` health check; the override waits for that health state before starting the backend. Keep the database URL and passwords private. Redis is included in the optional profile but is not currently used by the backend. `docker compose ... down` stops the services while retaining named data volumes; remove volumes only when their data is disposable.

The backend and prototype publish only to localhost. If the configured prototype host port 8080 is unavailable, choose an unused localhost port in a temporary Compose override rather than exposing it publicly.

## What the tests do not use
No real exchange, no network, no real money. Exchanges are in-process fakes; the crash tests kill real child processes against a file-backed fake exchange.

## Market-data and executable-quote gates
- Candle decisions require a non-empty, strictly ordered, gap-free sequence of valid OHLCV candles aligned to the requested timeframe. The newest candle must be closed and no older than `DEWAKO_CANDLE_MAX_AGE_INTERVALS` (default 2 intervals, configurable from 1 to 10 intervals, measured from candle close). Future candle opens beyond `DEWAKO_CANDLE_CLOCK_SKEW_SECONDS` (default 30 seconds) are refused. No timestamp is rewritten.
- Execution requires all of `DEWAKO_MAX_SPREAD_PCT`, `DEWAKO_MAX_PRICE_DEVIATION_PCT`, and `DEWAKO_MAX_QUOTE_AGE_SECONDS`. If they are absent or partially configured, order execution fails closed. Values are percent units (`0.25` means 0.25%); the maximum price deviation is measured from the approved reference price to the executable ask for BUY or bid for SELL. `DEWAKO_QUOTE_CLOCK_SKEW_SECONDS` defaults to 2 seconds.
- Values must be selected for the instrument, venue, and strategy by the operator. There is no universal profitable threshold, including for XAUUSD. The example environment leaves execution limits unset.
- Illustrative paper-only configuration (not a trading recommendation or an XAUUSD preset):
  ```dotenv
  DEWAKO_MAX_SPREAD_PCT=0.25
  DEWAKO_MAX_PRICE_DEVIATION_PCT=0.50
  DEWAKO_MAX_QUOTE_AGE_SECONDS=5
  ```
- The guard validates top-of-book only. The current adapter contract does not supply verified executable depth or market impact, so this is not depth-based slippage protection and cannot guarantee a fill at the quote. The quote is fetched once before an intent is created and fetched again immediately before the adapter submission.
- Strategy output must anchor its entry reference to the latest validated candle close. The execution engine independently applies quote checks, so strategy/plugin code cannot skip them.

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


## Phase 4.5 additions
- Dependencies for HTTP/browser tests: `pip install -r backend/requirements.txt -c backend/constraints-test.txt -r backend/requirements-test.txt`. The constraints are resolved from a Python 3.13.15 install and upgraded to versions with no findings in the pinned-environment `pip-audit` check. Python 3.14 is not part of the CI matrix; the pinned `pydantic-core`/`greenlet` wheels cannot be built on the available 3.14 interpreter.
- Real-app HTTP tests: `cd backend/tests && DEWAKO_REQUIRE_HTTP_TESTS=1 python -m pytest -q --timeout=300 test_api_testclient.py test_api_phase44.py` (needs fastapi, httpx, uvicorn; with the flag set a missing dependency FAILS).
- Dashboard e2e against the real app: `python -m playwright install chromium && DEWAKO_E2E_SERVER=real PYTHONPATH=backend python frontend/browser_tests/test_dashboard_browser.py`.
- Dashboard browser tests against the stand-in (NOT FastAPI): same command without `DEWAKO_E2E_SERVER`. Label results accordingly.
- Standalone runner (no pytest): `python3 scripts/run_tests_standalone.py [module-substring ...]`; every test has a bound (`DEWAKO_TEST_TIMEOUT`, default 180 s) and a hang is reported as `FAIL (TIMEOUT ...)` with the test name.


## Phase 5.1: running without pytest
`PYTHONPATH=scripts/pytest_shim python3 scripts/run_tests_standalone.py [module-substring ...]` supports the pytest features the suite uses (parametrize, raises(match), approx, monkeypatch) with a per-test timeout. It is a stand-in for sandboxes without package access; CI uses real pytest. Ruff: `ruff check backend/dewako backend/tests scripts --select E9,F63,F7,F82`.
