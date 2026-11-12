# GitHub CI/CD final validation report

**Final status: PARTIALLY VERIFIED.** Every workflow job passes on the evaluated commit and the failure-propagation proof holds, but two
acceptance items need repository settings this session could not change (branch protection on `main`; secret-scanning settings).
Live trading remains **LOCKED**; readiness remains **NOT READY**.

Evaluated commit: `main` = `74e74640a7e0fb9ad0e693401f33b36e17a8c248` (merge of PR #7). Date: 2026-11-13.
This file itself lands in a later documentation-only commit.

## Three separate things (not to be confused)
| | Status on `74e7464` | Means |
|---|---|---|
| CI success | **PASSED**: ci run 43316036942 attempts 1, 2, 3 all green (10/10 jobs each); security run 43316036830 green (3/3) | the code builds, the suites pass under the stated policies |
| Paper-trading qualification | **PASSED (bounded)**: paper-qualification run 43316069726 green (4/4) | the paper runtime behaved correctly in the tested conditions; the multi-day soak has not been run |
| Live-trading readiness | **NOT READY, LOCKED** | nothing in this work moves toward live trading; no exchange adapter, credential or testnet path was added |

## Evidence on `74e7464`
| Workflow / job | Result |
|---|---|
| ci: static, sqlite (3.12), sqlite (3.13), postgres16, crash-repeat, windows-crash (3.13), windows-crash (3.14, informational), e2e-real-app, docker, compose-smoke | green in attempts 1, 2 and 3 of run 43316036942 |
| security: python-dependencies, secrets-history, container-image | green (run 43316036830): pip-audit "No known vulnerabilities found"; gitleaks no unreviewed findings; Grype no High/Critical with a fix |
| paper-qualification: checklist (SQLite), checklist (PostgreSQL 16) | 210 tests each, 0 skipped, 0 failed, 39/39 checklist tests passed |
| paper-qualification: soak (SQLite) | 3,000 cycles, 12 restarts, 27,000 worker iterations, 0 failures, 403 round trips, 0 incidents, memory +8.1 MB after warm-up (51.2 MB at warm-up), database 4.1 MB, 196 s |
| paper-qualification: soak (PostgreSQL 16) | 3,000 cycles, 12 restarts, 27,000 iterations, 0 failures, 403 round trips, 0 incidents, memory +8.2 MB, 203 s |

Soak latency, p95 per worker iteration (ms, SQLite / PostgreSQL; GitHub-hosted ubuntu-24.04, Python 3.13, one run each, wall clock
around each `run_once` call): bot_runner 26.2 / 23.6; exit_monitor 17.6 / 18.7; risk_equity 6.6 / 3.0; order_recovery 6.4 / 2.8;
reconciliation 3.4 / 1.5; audit_integrity 34.8 / 75.8. These are a first baseline, not a before/after comparison: no performance
improvement is claimed.

## Failure-propagation proof (branch `ci-failure-proof`, never merged)
| Run | Injected | Result |
|---|---|---|
| 43311773541 (`1c7b2f4`) | JS syntax error; a crash test fails; e2e on the stand-in server; health check on a missing route; a required test skipped | every job red: static (SyntaxError), crash-repeat and both Windows jobs (the failing crash test), compose-smoke ("backend did not become healthy"), e2e (checks fail on the stand-in), sqlite/postgres16/docker (the failing test) |
| 43314394409 (`1592c13`) | **only** a required test skipped | sqlite 3.12/3.13, postgres16 and docker red although pytest itself reported 0 failures ("REQUIRED test did not pass (['skip'])"); every other job green |
Not isolated: the e2e "real server required" rule alone (the stand-in also fails the checks, so the job was red either way).
The branch could not be deleted from this session (write access to that API path is blocked); delete it in the GitHub UI.

## Acceptance criteria
| Criterion | Status |
|---|---|
| Required GitHub Actions jobs pass | Met (above) |
| Backend and frontend validation pass | Met: both backends, 3.12 + 3.13, browser e2e on the real app, JS syntax check, Node UI tests enforced |
| SQLite and PostgreSQL testing policies satisfied | Met: SQLite only PostgreSQL-only skips; PostgreSQL zero skips; shared required lists |
| Docker validation passes | Met: test image vs PG16; production compose smoke |
| Security checks pass or documented exceptions approved | Met with documented exceptions awaiting owner approval: 3 synthetic test values in `.gitleaks.toml`; Medium Python 3.12 CVEs (below the High threshold) listed in `GITHUB_CI_SECURITY_REVIEW.md` |
| Test artifacts retained | Met: JUnit / logs uploaded by every job, missing files fail the upload |
| No important test silently skipped | Met: proven by run 43314394409 |
| Paper-trading safety tests pass | Met (bounded): run 43316069726 |
| Documentation reflects the repository | Met for the documents in `GITHUB_CI_DOCUMENTATION_REVIEW.md`; other historical audits unchanged |
| No critical/high unresolved regression | Met. One coverage reduction by decision: Windows 3.14 is informational |
| Live trading remains locked | Met |
| `main` protected by the required checks | **Not met: needs the owner** (blocked through this session) |

## Owner actions
1. Settings -> Branches -> rule for `main`: require status checks (`static`, `sqlite (3.12)`, `sqlite (3.13)`, `postgres16`,
   `crash-repeat`, `windows-crash (3.13)`, `e2e-real-app`, `docker`, `compose-smoke`, `python-dependencies`, `secrets-history`,
   `container-image`), require branches up to date, block force pushes and deletions.
2. Turn on secret scanning with push protection and Dependabot alerts.
3. Delete branch `ci-failure-proof`.
4. Review the 10 Dependabot pull requests (#8-#17). Each must pass ci + security before a merge. Major action upgrades (#9-#12) and the
   base-image jump to `3.15-slim` (#8) need care; moving the image to `3.13-slim` (the version CI tests most) is the safer change.
5. Approve or reject the documented security exceptions and the long-soak acceptance numbers.

## Known limits
* Soak memory rose 8 MB over 3,000 cycles (0.9 MB over 300 locally): within the limit, but not flat. The multi-day soak will show
  whether it levels off.
* The soak injects market data and sets wide risk limits for churn; see `GITHUB_CI_PAPER_TRADING_QUALIFICATION_PLAN.md`.
* GitHub-hosted runners only; nothing was deployed.
