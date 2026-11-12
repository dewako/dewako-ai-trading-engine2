# Documentation consistency review

Reviewed against `main` = `9172eb2` and the `ci-hardening` branch, 2026-11-13. A claim was changed only after its replacement was checked
(CI run, test name, or code path). Status words: Implemented / Automated-test verified / Manually verified / Not tested / Not applicable.

| Document | Stale claim found | Replacement | Checked against |
|---|---|---|---|
| `README.md` | "Status (Phase 3.4) ... HTTP tests, CI and Docker have not been executed"; "real FastAPI HTTP tests have not been run"; `ENCRYPTION_KEY` (the variable is `ENCRYPTION_KEYS`); a duplicated table row | dated status on `9172eb2`, HTTP tests run in CI, correct variable name, duplicate removed | CI runs 43299863383 (x3), `.env.example` |
| `docs/LIVE_TRADING_READINESS.md` | real-HTTP tests for `/safety/live/enable` (409) and `/auth/logout` (401) "NOT EXECUTED"; CI "NOT TESTED, never ran on a runner"; Docker "NOT TESTED, image never built"; 249/252 test counts | those rows updated and marked *updated 2026-11-13* with the evidence; header says only those rows were re-verified, all others are unchanged since Phase 3.4; counts marked superseded | required tests `test_enable_live_returns_409_with_reasons_and_never_claims_live_is_active`, `test_logout_requires_authentication_and_is_not_a_public_route` in green CI; `docker` and `compose-smoke` jobs |
| `docs/TESTING.md` | Docker "not executed by the author"; CI policy without `--require-file` or the job table | current job table, skip policy, required-test files, Docker and soak commands, the replay-uses-SQLite note | `ci.yml`, `security.yml`, `paper-qualification.yml` |
| `docs/ARCHITECTURE.md` | "The default API process does not start any workers yet" | workers start at application startup (`DEWAKO_WORKERS=0` turns them off) | `runtime/application.py` (`Config.from_env`, `startup`) |
| `docs/PHASE5_2_STAGE2_FINAL_AUDIT.md` | Windows 3.14 hang "has not recurred" | dated addendum: recurred in run 42839151033; 3.14 informational in CI | GitHub run 42839151033 jobs |
| `.github/workflows/ci.yml` header | "never been executed on a hosted runner" | header describes the current policy | 100+ hosted runs |
| `docker-compose.test.yml` header | "not executed by the author" | run by the `docker` job | `ci.yml` |

Not changed (outside this task or not verifiable here): the Phase 2 / 3.x hardening audits (historical records of their own commits);
`LIVE_TRADING_READINESS.md` rows other than the four above; `docs/EXCHANGE_ADAPTERS.md`. Live trading remains LOCKED and readiness
NOT READY in every document.

Every new report names the commit it evaluates: `GITHUB_CI_INITIAL_AUDIT.md` (`d650bb8`, `da950cb`), `GITHUB_CI_IMPROVEMENT_PLAN.md`
(`9172eb2`), `GITHUB_CI_FINAL_VALIDATION_REPORT.md` (the final commit and its runs).
