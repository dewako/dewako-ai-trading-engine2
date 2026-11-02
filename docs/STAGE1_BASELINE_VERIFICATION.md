# Stage 1 / Stage 1.1 Verification Record

Scope: paper trading only. Live trading stays locked; no testnet or live adapter exists; no real credentials were used in any run listed here. This file records what was actually executed, with run URLs and SHAs, and what is still NOT verified.

## 1. Verdict

| Gate | Status | Evidence |
|---|---|---|
| **G1 (Stage 1 baseline)** | **PASSED** (with the caveat in section 4) | hosted CI, all 9 jobs green, SHA `f476998723915f1df7c570e9110c10fd783dc9fa` (`main`), run 41372146491, attempts 1, 2, 3 |
| **Stage 1.1 (four P1 fixes)** | **PASSED** on the same commit | branch run 41370530401 at `608cc8d91f5c`, attempts 1 and 2 green; merged by PR #3 |
| Stage 2 (instrument selection, domain model, testnet adapter) | **NOT STARTED, BLOCKED** | needs separate explicit authorisation |
| Testnet / live trading | **NOT IMPLEMENTED, LOCKED** | |

## 2. Hosted CI evidence

Repository: https://github.com/dewako/dewako-ai-trading-engine2

| Run | SHA | Attempts | Result |
|---|---|---|---|
| https://github.com/dewako/dewako-ai-trading-engine2/actions/runs/41372146491 | `f4769987` (main, after PR #3) | 3 (original + 2 manual re-runs) | 9/9 jobs green in each attempt |
| https://github.com/dewako/dewako-ai-trading-engine2/actions/runs/41370530401 | `608cc8d9` (stage1-1-p1-fixes) | 2 (original + 1 re-run) | 9/9 jobs green in each attempt |
| https://github.com/dewako/dewako-ai-trading-engine2/actions/runs/41369763076 | `986c6232` | 1 | FAILED: 4 jobs, one test (see section 5) |

Jobs: static, sqlite (3.12, 3.13), postgres16, crash-repeat, windows-crash (3.13, 3.14), e2e-real-app, docker.

Counts on run 41370530401 (attempt 1): PostgreSQL 16: 530 tests, 0 skipped, 0 failed, skip gate `required_ok=44/44`. SQLite 3.12 and 3.13: 530 tests, 3 skipped (all "PostgreSQL-only", allowed), 0 failed, gate `required_ok=32/32`. e2e-real-app: 0 failed against the real FastAPI app under Uvicorn. The HTTP tests executed in these jobs. They first ran on hosted CI on the Stage 1 branch (run on `649700099d80`: PostgreSQL 500 passed / 0 skipped, SQLite 497 passed / 3 allowed skips, e2e 15/15); the Stage 1.1 HTTP tests first ran on 2026-11-02 (section 5).

## 3. What Stage 1.1 changed (behaviour)

Details in `STAGE1_1_REMEDIATION_REPORT.md`. Summary: optional, disabled-by-default time stop; entries sized from the executable ask (never scaled up); bounded policy for stop/target exits through wide spreads, raising a CRITICAL `EXIT_BLOCKED_WIDE_SPREAD` incident that blocks new entries; `/health` readiness from current worker/protection state (503 `SYSTEM_DEGRADED`) plus `/health/live`.

## 4. Caveats (not resolved)

1. **Windows 3.14 hang unexplained.** `windows-crash (3.14)` hung on attempt 3 of run 41311105233 and was cancelled with no log. It has not recurred in the later runs listed above, but its cause is unknown. CI now has pytest `--timeout` and `faulthandler` flags that will name the test if it recurs. "Passed" here means no recurrence so far, not that the cause is fixed.
2. **Paper-only evidence.** Tests show the logic is internally consistent on simulated quotes. They do not show profitability, exchange behaviour (fills, rejects, rate limits, outages), or readiness for testnet or live.
3. **Controls reported NOT_ENFORCED by design:** high-impact news blackout and live realised-PnL limits.
4. Test evidence relies on injected deterministic data and manually driven worker ticks in several tests; there is no soak run of the real loops.
5. The local PostgreSQL runs used a throwaway `fsync=off` cluster; the hosted PG16 job is the stronger evidence.

## 5. Failure recorded for honesty

Run 41369763076 failed on `test_stage1_http.py::HttpLifecycle::test_create_bot_over_http_then_see_an_order_and_a_protected_position` (postgres16, sqlite 3.12/3.13, docker). Cause: the test asserted `/health` was 503 before any worker had reported, but the supervisor ticks its workers at start-up, so the app was already READY. A test premise error, not a readiness defect; the racy assertion was removed (`608cc8d`). The deterministic 503 case is covered by `test_readiness_follows_the_current_worker_state_over_http_and_liveness_does_not`.

## 6. Before anything beyond Stage 1.1

Stage 2 and any testnet adapter need explicit authorisation and a stated definition of "good enough" for paper results (for example a minimum paper-run period and what to inspect in it). Live trading remains locked regardless.
