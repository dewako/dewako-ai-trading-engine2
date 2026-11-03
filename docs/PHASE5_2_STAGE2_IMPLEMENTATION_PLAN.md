# Phase 5.2 Stage 2 - Implementation Plan (as approved and as executed)

Paper trading only. Live trading stays LOCKED. No live or testnet adapter, no real credential, no real order was added or sent.

## Scope
Paper Trading Runtime Hardening and Reliability, work packages WP1-WP7 of the Stage 2 brief. Excluded by the brief and not touched: live execution, live adapters, autonomous or self-improving AI, LangGraph, automatic risk-limit changes, unrelated frontend work.

## Starting point
Stage 1 and Stage 1.1 recorded PASSED and merged (main at `e9f196b`). Stage 2 branches are stacked and NOT merged to main; the owner decides on pull requests.

## Sequence executed (stacked branches, each verified before the next)
| Order | WP | Branch | Head |
|---|---|---|---|
| 1 | WP5 time-stop | `stage2-wp5-time-stop` | 2f8250a |
| 2 | WP4 readiness | `stage2-wp4-heartbeat-readiness` | 05d4d9a |
| 3 | WP1 sizing | `stage2-wp1-risk-sizing` | d139513 |
| 4 | WP2 exit spread | `stage2-wp2-exit-spread` | 0dde553 |
| 5 | WP2b stop policy (owner decision) | `stage2-wp2b-stop-immediate` | b7e8829 |
| 6 | WP2c suspect-quote hold, TP waits | `stage2-wp2c-exit-policy` | 7a6f771 |
| 7 | WP6 audit reliability | `stage2-wp6-audit-reliability` | c0d20d1 |
| 8 | WP3 partial exits | `stage2-wp3-partial-exits` | 970a41e |
| 9 | WP7 lifecycle / kills | `stage2-wp7-lifecycle` | efa1de2 |

Order was chosen by risk: configuration safety first (WP5, WP4), then money-affecting logic (WP1, WP2), then evidence and recovery (WP6, WP3, WP7). WP1 was done before WP2 at the owner's instruction.

## Owner decisions that changed the plan
- Position sizing moves into the Risk Engine (WP1), computed from the executable ask.
- Stop-loss exits immediately at market; take-profit and time stop keep a spread tolerance (WP2b/WP2c). A stop that sees a spread above a suspect limit (default 10 %) must persist for 3 cycles first; this is a trade-off, see FINAL_AUDIT.
- Instrument for now: BTC/USDT; no BTC-specific assumption was added.

## Proof per work package
Each WP has its own test file (`test_stage2_wp*.py`), mutation checks recorded in the changelog, and a hosted CI run on a recorded SHA. Skip gate: `scripts/assert_no_skips.py` with `--require <test name>` for each new test.

## Migration
One additive migration (10, `position_exits`). No existing table or column was altered. See `docs/MIGRATIONS_STAGE2.md`.

## Deferred (not Stage 2)
Risk-profile GUI (Conservative / Balanced / Custom, human-set limits only), maximum-trades-per-day limit (needs separate approval), Phase 9 self-improving agent (must never change risk limits by itself).
