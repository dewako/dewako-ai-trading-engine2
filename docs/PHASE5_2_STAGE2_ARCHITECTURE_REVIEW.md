# Phase 5.2 Stage 2 - Architecture Review

Flow (unchanged shape): Strategy -> Signal -> Risk Engine -> Execution Engine -> Adapter. Risk Engine is the only authority on size and approval; Execution Engine is the only sender; ExitMonitor is the only automatic seller of open positions.

## What changed and why

| Area | Before | After | Residual risk |
|---|---|---|---|
| Sizing (WP1) | quantity fixed from the reference price | `RiskEngine.size_for_executable_entry` rescales (never up) from the executable ask; `final_entry_problem` re-checks the rounded quantity | a gap between ask check and fill on a real exchange |
| Exit policy (WP2/2b/2c) | a wide spread could defer a stop | stop sent at once; suspect spread (>10 %) must persist 3 cycles; take-profit / time stop wait for a fair spread, audited, no incident | a real flash crash also looks like a wide spread: stop may be delayed up to ~2 cycles |
| Readiness (WP4) | worker looked healthy at iteration start | STARTING until an iteration completes; stale heartbeat (3x interval + 5 s) = FAILED; critical failure = not ready; recovery needs a real success | depends on worker intervals being right |
| Time stop (WP5) | env-only | stored in the database; DISABLED by default; changes refused while positions are open; reported NOT_ENFORCED / ENFORCED | change needs a restart with nothing open |
| Audit (WP6) | bare `except: pass` in several paths | `record_safely` holds, logs CRITICAL, reports DEGRADED, flushes in order exactly once, blocks entries (never exits) and readiness while degraded | held events are memory-only; a crash while degraded loses them |
| Partial exits (WP3) | not recorded durably; stuck partial silent | `position_exits` table, REDUCING state, restart keeps known partial exits, stall detection (120 s) | stalled exit needs a human / recovery worker; no automatic resend by design |
| Recovery tests (WP7) | one SIGKILL test | real-kill matrix incl. partial exit, repeated cycles, feed / DB / worker failure | simulated DB failure, not a server stop |

## Design principles applied
1. Fail closed for risk-increasing actions (entries), never for risk-reducing ones (exits).
2. A failure of the evidence system must neither change a trade nor be silent.
3. Derived state is written in the same transaction as its source.
4. Nothing is ever resent after an unknown outcome: recovery reads the exchange.
5. Stated limits instead of claims.

## Authority and isolation checks
- Live stays LOCKED: no code path added that enables it; no live or testnet adapter added; paper and live environments remain separate columns and adapters.
- No new permissions, endpoints with side effects, or secrets. One new read-only field in `/health/detailed` (`audit_persistence`).
- Risk limits are not modified by any automatic path.

## Known architectural weak points (not fixed in Stage 2)
- Paper adapter fills instantly; partial fills, delays and cancels were exercised only through test doubles.
- Single database is a single point of failure: a DB-wide outage blocks order creation (intended) and also the audit store.
- Stop trigger evaluated on the bid of a polled quote, not on an exchange-side stop order (paper only).
