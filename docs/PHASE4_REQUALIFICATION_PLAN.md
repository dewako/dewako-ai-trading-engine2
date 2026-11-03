# Phase 4 Re-qualification - Plan (DRAFT v2, awaiting approval; no code changed)

Paper research only. Live trading stays LOCKED. This plan reopens Phase 4 (Backtesting) of the 10-phase roadmap as its own milestone with its own acceptance criteria. It does not change Phase 5 scope. Proposed position: after Phase 5.2 Stage 3, before Phase 6.

## 1. Why reopen Phase 4 (findings on the current code, `dewako/backtesting/engine.py`, 137 lines)
| # | Finding | Effect |
|---|---|---|
| F1 | A stop fills at the stop price even when the bar opens below it | losses on gaps understated; contradicts the live stop policy (fill at the quoted bid) |
| F2 | Equity changes only when a trade closes | drawdown and Sharpe understated while a position is open |
| F3 | Own sizing, no Risk Engine, no take-profit, no time stop, no spread, no partial fills | tests different logic from the system that trades; Stage 2 rules absent |
| F4 | Fixed slippage percentage | no spread, no volatility or size effect |
| F5 | Overfitting check is four rules of thumb; no walk-forward, no out-of-sample, no variant count | good-looking results cannot be trusted |
| F6 | Not wired to `market_data/store.py` (which already keeps point-in-time `known_ms` and gap detection) | no dataset identity, no reproducible data |
| F7 | Strategy-package ladder BACKTEST_ELIGIBLE -> PAPER_ELIGIBLE checks only a permission, not evidence | nothing forces a backtest before promotion |
| F8 | 3 tests (reproducible, costs reduce profit, no look-ahead); no CI evidence of fill realism | weak proof |
Not yet checked: whether Phase 4 was formally accepted earlier; whether Phase 5.2 Stage 3 already covers part of this (brief not seen).

## 1b. Mapping to the owner's Phase 3 & 4 brief (received 2026-11-04)
Source: "Phase 3 & Phase 4 - Development Brief". Its own principles: strategies must not bypass the Risk Engine; backtesting uses deterministic, reproducible execution assumptions; AI must not modify parameters or promote strategies without an approved validation process; results are not proof of future profitability. Phase 4 is complete only with "realistic execution assumptions and reliable performance reports".

| Brief item | Current state (checked in code) | Gap |
|---|---|---|
| Import historical data, multiple instruments and timeframes | `CandleStore` exists (point-in-time, gap finder); backtester takes a raw candle list for one symbol | not wired; no dataset identity |
| Validate missing AND incorrect candles | gaps only | no check for bad OHLC (high < low, zero/negative, spikes), duplicates, non-monotonic time |
| Stop-loss and take-profit processing | stop only, filled at the stop price | no take-profit, no time stop, wrong gap fills (F1) |
| Historical position and order records | trades list only | no order/position records |
| Spread and slippage, fees, sizing, realistic execution | fixed slippage and fee, own sizing | no spread; sizing bypasses the Risk Engine (violates the brief's own principle) |
| Metrics: P&L, win rate, profit factor, expectancy, max drawdown, risk/reward, consecutive wins/losses | P&L, win rate, profit factor, avg trade, drawdown (understated), Sharpe per bar | risk/reward and consecutive streaks missing; drawdown wrong (F2) |
| Strategy comparison (strategies, parameter sets, timeframes, weaknesses) | none in the engine | missing |
| Visual analytics | GUI page exists but is a DEMO: it does not run any strategy; it draws random trades from a seeded generator with hard-coded win rates per strategy, on synthetic candles. The page is labelled demo | no real results reach the GUI |
| No look-ahead, no leakage, reproducible, out-of-sample | next-bar-open entry, reproducible record; no out-of-sample or walk-forward in the engine | leakage and out-of-sample controls missing |

Consequence: the GUI backtest page is a third, unrelated implementation. It stays labelled demo until B6 replaces it with real run results; no number on it may be used as evidence.

## 2. Principles
1. What is backtested is the code that trades: the real Strategy, Risk Engine, ExecutionEngine and ExitMonitor run on replayed data; only the adapter and clock are simulated.
2. A backtest result is evidence only together with its costs, its data identity and the number of variants tried.
3. Pessimistic where unsure: costs and fills err against the strategy.
4. Reproducible: same dataset hash, code commit and parameters give the same result byte for byte.
5. No path from a backtest to live. A backtest can only unlock the next paper step.
6. No automatic change of risk limits by any research tool.
Not adopted: NautilusTrader or VectorBT as the core (would test a different system). VectorBT may be used offline for idea screening; its output is never acceptance evidence.

## 3. Work packages
| WP | Title | Deliverable | Main files (new unless noted) |
|---|---|---|---|
| B1 | Simulated exchange adapter and virtual clock | adapter with fee, spread, volatility-scaled slippage, latency, partial fills, gap fills at the quoted price; deterministic | `backtesting/sim_adapter.py`, `backtesting/clock.py` |
| B2 | Replay harness | feeds candles through real Strategy -> Risk Engine -> ExecutionEngine -> ExitMonitor in an isolated database; mark-to-market equity every bar; replaces `engine.run` (old API kept as a thin wrapper, deprecated) | `backtesting/replay.py`, `backtesting/engine.py` (edit) |
| B3 | Dataset layer | versioned dataset from `CandleStore` for any instrument and timeframe (point-in-time; gap, duplicate, ordering and bad-OHLC/spike report; content hash; source recorded); refuses a dataset with unexplained gaps or invalid candles | `backtesting/datasets.py` |
| B4 | Validation and comparison | metrics completed (expectancy, risk/reward, consecutive wins/losses, correct drawdown); strategy, parameter-set and timeframe comparison with a weakness report; walk-forward and out-of-sample splits; cost stress at 1x/2x/3x; parameter-stability map; variant counter; trade-order Monte Carlo for the drawdown range; plain-language warnings | `backtesting/validation.py` |
| B5 | Run records and promotion gate | additive migration `backtest_runs` (dataset hash, commit, parameters, assumptions, metrics, status); `PAPER_ELIGIBLE` transition requires a PASSED run for that exact strategy version, with thresholds set by a human; every decision audited | migration 11, `strategy_packages/manager.py` (edit), `backtesting/gate.py` |
| B6 | Report and GUI | per-run report (equity, drawdown, entries/exits on the chart, trades, costs paid, warnings); API route (read-only); the demo backtest page is replaced by real run results (or stays labelled demo) | API route + prototype screen |
| B7 | Evidence | tests per WP, mutation checks, SQLite and PG16 suites, hosted CI x3 on a recorded SHA, JUnit and raw logs, final audit | `docs/PHASE4_REQUALIFICATION_*` |

Order: B3 -> B1 -> B2 -> B4 -> B5 -> B6 -> B7 (data and fill realism before anything is judged on results).

## 4. Tests that must prove it (examples)
- Gap through a stop fills at the gapped price, not the stop (F1); a stop never fills better than the bar allows.
- Equity curve equals cash plus open-position value on every bar; max drawdown matches an independently computed figure (F2).
- Replay produces the same orders as live code for the same inputs (same Risk Engine sizing, same exit decisions, including suspect-quote hold and take-profit wait) (F3).
- Same dataset hash + commit + parameters => identical result; a changed bar changes the hash.
- No look-ahead: a strategy given a bar's future close gains nothing (shuffled-future test).
- Walk-forward refuses to reuse test data for tuning; variant count increases with each parameter set tried.
- Promotion gate: no evidence, failed run, run for another version, or stale dataset => transition refused and audited.
- Replay never touches a live adapter or network; paper environment only.

## 5. Acceptance criteria and final status
Final status must be one of PHASE 4 RE-QUALIFIED / PARTIALLY VERIFIED / NOT RE-QUALIFIED.
Required: findings F1-F7 closed or explicitly adjudicated; replay uses the real pipeline; results reproducible; promotion gate enforced in code; SQLite and PG16 suites green; hosted CI green three times on one SHA; no live capability added.
Honest limits to state in the final audit: candles cannot show real spreads or order-book depth, so results overstate; BTC/USDT only gives a narrow range of market conditions; backtest success is not evidence for live.

## 6. Risks and compatibility
- Behaviour change: old `run()` results will differ (costs and gaps now realistic). Old runs stay readable; none exists in the database today (not verified for the prototype data).
- Replay speed: real pipeline is slower than a vectorised loop; mitigated by an in-memory SQLite per run and running validation in parallel processes.
- Migration 11 is additive; rollback = drop `backtest_runs` and revert the gate (gate off = promotion as today, documented).
- Scope creep: no optimizer, no AI evaluation, no new strategies in this milestone.

## 7. Decisions needed from the owner
1. Approve the milestone and its position (after Stage 3, before Phase 6).
2. Promotion thresholds: who sets them and the starting values (suggested: minimum trade count, maximum drawdown, positive result at 2x costs, out-of-sample result not below a fraction of in-sample). Values are human-set only.
3. Data source for history (existing public providers via `CandleStore`) and the period to use for BTC/USDT.
4. Whether the promotion gate should also apply to strategies already in `PAPER_ELIGIBLE`.
