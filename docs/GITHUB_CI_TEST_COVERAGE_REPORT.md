# CI test coverage: before and after

"Coverage" here means which tests CI runs, which it requires, and what it refuses to accept; not line coverage (no line-coverage tool is
configured, and none was added). Before = run 43299863383 attempt 2 on `da950cb` (the last commit before this work). After = the runs
listed in `GITHUB_CI_FINAL_VALIDATION_REPORT.md`.

## Per job
| Job | Before | After | Change |
|---|---|---|---|
| static | compileall (backend), ruff errors, secret scan, pip-audit | compileall (backend + scripts), ruff errors, secret scan, skip-policy self-test (8 cases), `node --check` of 5 dashboard JS files | pip-audit moved to `security.yml` (broader there) |
| sqlite 3.12 / 3.13 | 874 tests, 3 skipped (PostgreSQL-only), floor 450, 260 required | same suite + 1 new test (Node required); floor 870; 271 required; Node UI tests can no longer skip; JUnit kept | stricter |
| postgres16 | 874, 0 skipped, floor 450, 272 required | floor 870; 274 required (adds `test_hung_worker_is_killed_by_the_controller_timeout` and the PostgreSQL race test); Node required | stricter |
| crash-repeat | 24 (SQLite) / 27 (PG) x3, floor 22, 3 required | floors 24 / 27, same 3 required | stricter |
| windows-crash 3.13 | 39, 3 skipped x3, floor 25 | floor 39; artifact per Python version | stricter |
| windows-crash 3.14 | blocking | informational (`continue-on-error`), still runs and reports | **weaker by decision**: intermittent hang (run 42839151033) not root-caused |
| e2e-real-app | 16 checks, exit code only, log kept | JUnit; fails unless all 16 recorded and the real FastAPI server was used; floor 16; log + JUnit + screenshots kept; secret scan of uploads | stricter |
| docker | full suite in the image vs PG16, 869 passed + 5 skipped, skips unchecked, JUnit lost | JUnit kept; floor 870; skips only "backend-only image" / "node is not installed"; PostgreSQL-only tests required | stricter |
| compose-smoke | did not exist | production image and compose: no .env refused, no tests in image, uid 10001, healthy, /health, migrations, restart keeps the database, graceful stop, missing / invalid setting refuses to start | new |
| security | did not exist | pip-audit x3, gitleaks over history, Grype on the image; weekly too | new |
| paper-qualification | did not exist | 39-test checklist on SQLite + PG16; bounded soak on SQLite + PG16 | new (runs from `main` only: scheduled / manual) |

## Required-test lists
The inline lists in the old `ci.yml` had drifted: `test_hung_worker_is_killed_by_the_controller_timeout` was required only on SQLite, and
the nine crash-boundary tests plus the HTTP backend-identity test only on PostgreSQL, although all of them run on both. They are now one
file, `.github/required-tests/common.txt` (271), plus `postgres-only.txt` (3). Every test required before is still required (checked by
script when the files were built).

## Skip policy
* SQLite: only skips whose message contains "PostgreSQL-only" (3 tests).
* PostgreSQL 16: none.
* Docker image: "backend-only image" (2 tests that need the full repository layout) and "node is not installed" (the Node UI tests,
  which run in the SQLite and PostgreSQL jobs).
* Windows: "PostgreSQL-only".
* `DEWAKO_REQUIRE_HTTP_TESTS=1` (existing) and `DEWAKO_REQUIRE_NODE=1` (new) turn a missing prerequisite into a failure.

## PostgreSQL-specific behaviour
The PostgreSQL job runs the whole suite with zero skips, so nothing is tested only against SQLite by accident, with one deliberate
exception: the replay engine always runs in a throw-away SQLite database (`backtesting/replay.py`), so replay tests exercise SQLite even
in the PostgreSQL job. Run records, the promotion gate and everything outside the replay run on both. Now documented in `docs/TESTING.md`.

## Not covered
No line coverage measurement; no mutation testing in CI (mutation checks were done by hand for the new tests); Windows has no PostgreSQL;
the soak does not exercise the real public market-data path.
