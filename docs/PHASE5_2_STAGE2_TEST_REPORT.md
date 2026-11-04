# Phase 5.2 Stage 2 - Test Report

Code under test: `efa1de241d5e7d95e3957abbc317918f46e70d57` (branch `stage2-wp7-lifecycle`, all Stage 2 work). Later commits only add documents and evidence files. Paper trading only.

## Local full suites (sandbox, Python 3.13.18, pytest 9.1.1)
| Suite | Collected | Passed | Failed | Skipped | Why skipped |
|---|---|---|---|---|---|
| SQLite | 638 | 588 | 0 | 39 | 36 HTTP tests (`fastapi is not installed` in the sandbox) + 3 PostgreSQL-only |
| PostgreSQL 16.x (throwaway local cluster) | 638 | 591 | 0 | 36 | the same 36 HTTP tests |

The 36 HTTP/API tests could not run locally. They run in hosted CI (below) with `DEWAKO_REQUIRE_HTTP_TESTS=1`, where zero skips are enforced on PostgreSQL. Raw logs and JUnit XML: `docs/evidence/stage2_full_suite_sqlite.txt`, `stage2_full_suite_postgres16.txt`, `stage2_sqlite_junit.xml`, `stage2_pg16_junit.xml`.

## Hosted CI, commit `efa1de2`, run 41637014964
| Job | Result |
|---|---|
| sqlite 3.12 / 3.13 | 627 tests, 624 passed, 3 skipped (PostgreSQL-only, allowed), gate 66/66 required tests |
| postgres16 | 627 tests, 627 passed, 0 skipped, gate 78/78 |
| crash-repeat (3 fresh processes x SQLite + PG16) | 24-27 tests each, 0 skipped, 3/3 required kill tests |
| windows-crash 3.13 / 3.14 | 39 tests, 36 passed, 3 skipped (PostgreSQL-only), 3/3 required kill tests, three repetitions |
| static, e2e-real-app (real Uvicorn process), docker | green |

Attempts 1, 2 and 3 of this same run (same commit, whole workflow re-run each time): all jobs green each time.

## New tests per work package
WP5 8, WP4 8, WP1 15, WP2/2b/2c 28, WP6 17, WP3 15, WP7 7 = 98 new tests, plus edited existing ones: five Stage 1.1 spread tests and six WP2/WP2b tests moved to the new exit policy (assertions not weakened; the policy changed by owner decision), one WP3 assertion (REDUCING instead of OPEN after a partial exit). Every new test file has mutation checks recorded in the changelog; the one mutation that first survived (WP7 restart quarantine) led to an added assertion.

## Requirement-to-test map
| Brief item | Evidence |
|---|---|
| Executable-price-aware sizing | `test_stage2_wp1_risk_sizing.py` (grid > 500 cases, final gate, exchange rounding) |
| Exit handling never silently abandons a position | WP2 suite, suspect-quote hold, WP3 stall detection |
| Partial exits survive restart and reconcile | WP3 restart tests, WP7 real-kill partial-exit test |
| Readiness reflects critical worker failure | WP4 suite, WP6 and WP7 application-level tests |
| Time-stop status explicit | WP5 suite, `/risk/limits` assertion in `test_stage1_http.py` |
| Audit failures visible and safe | WP6 suite (17) |
| Real process termination | WP7 (SIGKILL / TerminateProcess, return code asserted) |
| Uvicorn lifecycle, Docker | CI `e2e-real-app`, `docker` jobs (existing, green) |
| Concurrency / idempotency | duplicate-signal, replayed-report and duplicate-exit tests (WP3), existing Stage 1.1 concurrency tests |

## Not covered (stated)
Real exchange behaviour, partial fills on a real venue, power loss or disk failure, a PostgreSQL server dying while connected (simulated only), the cause of the earlier Windows 3.14 hang (run 41311105233; not reproduced since).
