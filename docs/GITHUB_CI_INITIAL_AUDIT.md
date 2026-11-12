# GitHub CI/CD initial audit (read-only)

Evaluated: `main` = `d650bb87435ffbacd4371316ae427ae1f1d4510d` and branch `stage3a-dataset-layer` = `da950cb10664eb1a3a2c4306dd0e70cadec51a4d`
(66 commits ahead of main, merged afterwards as PR #6 -> `9172eb2af4127baaa599af6edf1808b24f4f8ca5`). Date: 2026-11-13.
Method: reading the repository and the GitHub REST API (runs, jobs, logs, branch protection). Nothing was changed during the audit.
Not readable through this session: repository Actions settings and secret-scanning settings (the API path is blocked).

## Workflow inventory (before)
One workflow, `.github/workflows/ci.yml` (685 lines), `on: [push, pull_request]`, `permissions: contents: read`. No Dependabot, no CodeQL,
no other workflow. `main` had no branch protection (`GET branches/main/protection` -> 404).

| Job | Runner / Python | What it ran | Timeout |
|---|---|---|---|
| static | ubuntu 3.13 | compileall, ruff E9/F63/F7/F82, scan_secrets.py, pip-audit of requirements.txt | 15 |
| sqlite x2 | 3.12, 3.13 | full suite on SQLite, only "PostgreSQL-only" skips allowed, >= 450 tests, 260 required tests inline | 30 |
| postgres16 | 3.13 + postgres:16 | full suite, zero skips, >= 450, 272 required tests inline, JUnit kept | 45 |
| crash-repeat | 3.13, SQLite + PG | crash suites x3 in fresh processes, >= 22, 3 required | 40 |
| windows-crash x2 | 3.13, 3.14 | crash suites x3 on SQLite, >= 25 | 40 |
| e2e-real-app | 3.13 + Playwright | 16 browser checks against the real FastAPI app | 30 |
| docker | ubuntu | docker-compose.test.yml (PG16 + full suite in the image), docker build | 30 |

Baseline evidence (run 43299863383, attempt 2, `da950cb`): sqlite 874 tests / 3 skipped (PostgreSQL-only) / 260 of 260 required;
postgres16 874 / 0 / 272 of 272; docker 869 passed + 5 skipped, skips not checked; crash-repeat 24 (SQLite) and 27 (PG) x3; windows 39 / 3
skipped x3; e2e 16 passed. Three green attempts of that run exist (attempts 1-3, all jobs).

## Verified findings
| # | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | High | `main` unprotected: a merge with red or no CI is possible | branch protection API 404 |
| F2 | High | `--min-tests 450` while ~874 run: losing ~400 tests would still pass | ci.yml, job logs |
| F3 | Medium | push + pull_request both run, no concurrency control: superseded pushes keep running | two runs on `da950cb` |
| F4 | Medium | no `workflow_dispatch` | ci.yml |
| F5 | Medium | no JUnit kept for the SQLite jobs; e2e has no JUnit and does not assert real mode or the number of checks | ci.yml |
| F6 | Medium | Windows 3.14 hang recurred (job cancelled at the 40-min limit); the Stage 2 audit says it had not | run 42839151033 |
| F7 | Low | a replay test hit the 300 s per-test limit once (flake risk) | run 42520384580 |
| F8 | Low | the required-test list was copied in two jobs and had drifted (one test only in the SQLite copy, 11 only in the PG copy although they run on SQLite too) | ci.yml |
| F9 | Low | header comments said the workflow had never run on a hosted runner | ci.yml lines 1-6 |
| F10 | Info | PostgreSQL job runs the full suite with zero skips (good) | ci.yml |
| F11 | Low | replay always uses a throwaway SQLite database by design, so replay tests never touch PostgreSQL (undocumented) | `backtesting/replay.py` |
| F12 | Info | production image is Python 3.12; only the docker job covers 3.12 + PostgreSQL | Dockerfile, ci.yml |
| F13-F16 | Medium | frontend is plain HTML/JS (no package.json/build/lockfile); Node-based UI tests SKIP when Node is missing; no JS syntax check; in the Docker image the UI tests skip silently | `frontend/prototype`, `test_stage3b_ui.py` |
| F17 | Medium | pip-audit audits whatever `requirements.txt` resolves to today, not the pinned set CI installs, and not test dependencies | ci.yml |
| F18 | Medium | secret scan is a custom regex over the working tree only; no history scan | `scripts/scan_secrets.py` |
| F19 | Medium | actions pinned by mutable tags | ci.yml |
| F20 | Low | images pinned by tag, not digest | Dockerfile, compose files |
| F21 | Medium | no container image scan | - |
| F22 | Info | least-privilege `contents: read` (good) | ci.yml |
| F23-F25 | Medium | no .dockerignore (tests and caches in the production image); unpinned dependencies in the image; no HEALTHCHECK | Dockerfile |
| F26 | Medium | production compose never started in CI: readiness, migrations, restart, shutdown, missing-secret behaviour unchecked | ci.yml |
| F27 | Medium | docker job: no skip policy, JUnit written to tmpfs and lost | docker-compose.test.yml |
| F28 | Low | `redis` service in docker-compose.yml although the code has no Redis dependency | docker-compose.yml, `.env.example` |
| F29-F31 | Medium | many paper-trading tests exist inside the suite, but no separate qualification workflow, no soak test, no soak environment; the paper runtime has never run continuously | tests, ci.yml |
| F32 | Medium | no latency / CPU / memory measurement in CI (only the replay resource test) | `test_stage3a_replay.py` |
| F33-F37 | Medium | stale documentation: README "Phase 3.4 ... CI and Docker have not been executed"; LIVE_TRADING_READINESS.md "real-HTTP tests NOT EXECUTED" and 249/252 counts; TESTING.md and compose header "not executed"; Stage 2 audit Windows note; no CI documentation | docs |

## Initial audit statement
No file, branch or GitHub setting was changed during the audit. Before the brief arrived: commit `da950cb` had been pushed and PR #6 opened.
