# Stage 1.1 Remediation Report

Base: `649700099d802eb544a07b99d821dea87dff9cdb` (branch `ci/stage1-g1-fixes`). Scope: the four P1 defects from `STAGE1_G1_REMAINING_DEFECTS.md`. Live trading stays locked; no testnet or live adapter was added; no credentials were used. Paper trading only.

## Status

**Implemented and locally tested; NOT verified on hosted CI.** The HTTP tests could not run in the build sandbox (FastAPI cannot be installed there), so the one new HTTP test and the two edited HTTP tests are unexecuted until the hosted run. Everything else ran on SQLite and PostgreSQL 16.

| Run (sandbox) | Result |
|---|---|
| Full suite, SQLite | 491 passed, 0 failed, 39 skipped (36 HTTP not runnable here + 3 PostgreSQL-only) |
| Full suite, PostgreSQL 16.15 | 494 passed, 0 failed, 36 skipped (all HTTP) |
| New file `test_stage1_1_p1.py` | 29 passed |
| ruff (E9,F63,F7,F82) | clean |

## The four fixes

**P1-1 Time stop.** `Config.max_hold_seconds` (env `DEWAKO_MAX_HOLD_SECONDS`) is passed to the `ExitMonitor`. Unset means no time stop; no default duration exists. Zero, negative, non-finite, boolean or non-numeric values raise at start-up (`Config.__post_init__`, `from_env`) and in the monitor constructor. Tests: default has none; exits at or after the threshold and not before; through the running Application; validation.

**P1-2 Executable-price sizing.** `executable_risk_quantity()` scales an approved ENTRY quantity by `(reference - stop) / (executable_price - stop)` whenever the executable basis (`max(reference, ask)`) is above the reference, so the loss at the stop cannot exceed what the Risk Engine approved. It never increases a quantity; a stop at or above the executable price, or any missing or non-finite figure, blocks the order. The reduction is audited (`reduced_for_executable_entry_risk_from`). Scaling happens before the exchange-rule, balance and limit checks, so those checks and the reservation use the reduced quantity.

**P1-3 Wide-spread exits.** `WideSpreadError` (a `QuoteGuardError`) carries the quote's own figures. The monitor evaluates the stop/target/time triggers on the bid of an otherwise valid quote (fresh, positive, uncrossed). A triggered exit then follows an explicit policy:
- `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT` unset (default), or the spread is wider than it: **no order is sent**, the position stays OPEN, a CRITICAL `EXIT_BLOCKED_WIDE_SPREAD` incident blocks new entries, the attempt count is kept and the exit is retried every cycle.
- Set and the spread is within it: the exit is sent with that bound only (the engine raises that one order's spread limit, never lowers it, for a SELL EXIT only) and audited as `EMERGENCY_EXIT_WIDE_SPREAD`.
- Staleness, crossed or invalid quotes are never rescued by the override. The position is never reported closed unless a fill is applied. Incidents clear after the normal clean cycles.

**P1-4 Readiness.** `Application.readiness()` is computed from current state: the startup verdict plus, when workers run, every critical worker HEALTHY and `_protection_ok()`. Anything unknown is not ready. `/health` returns 200 `{"status":"ok"}` when ready and **503** `{"status":"degraded","state":"SYSTEM_DEGRADED"}` when a running worker is unhealthy; other not-ready states keep their previous 200 `not_ready` shape. New `/health/live` is liveness only. With workers disabled, the startup status is reported as before (entries are refused anyway). No container health check in the repo used `/health`.

## Behaviour changes to be aware of

1. **Entry quantities are now slightly smaller whenever the ask is above the reference.** Example: reference 100, stop 95, ask 100.05 gives about 0.99 of the approved quantity. This is the fix, not a side effect.
2. **Executable-ask limit blocks became redundant for sized entries.** After scaling, notional at the ask is never above notional at the reference, so the two older tests that asserted a block purely from the ask basis were rewritten (limit and balance placed between the reduced notionals at the reference and at the ask) to still prove the basis is executable.
3. **`/health` can now return 503 during start-up** until each critical worker has completed one cycle.
4. **Fixtures:** `hardening_helpers.decision()` gained `entry_price`; tests not about the premium pass the adapter's ask (`FAKE_LIVE_ASK`, `PAPER_ASK`) so their literal quantities remain exact. Crash-matrix quantities are unchanged for the same reason.

## Not done / risks

- HTTP tests (including `test_readiness_follows_the_current_worker_state_over_http_and_liveness_does_not`) have not executed anywhere yet. The same scenario passes at the Application level.
- The emergency limit has no recommended value. A market-style exit in a wide spread can fill badly; the operator must choose the number or leave it unset.
- No soak or real-exchange evidence. Paper only. Profitability is not claimed.
- The Windows crash-job hang seen once on an earlier SHA is unexplained and unrelated to these changes.

## CI

Seven new `--require` names were added to the SQLite and PostgreSQL gates (see `.github/workflows/ci.yml`). Stage 1.1 should be treated as unverified until a hosted run on its commit is fully green, then re-run three times.
