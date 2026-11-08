# Stage 3B changelog (metrics, validation, regimes, run records, promotion gate, read-only report, resources)

Research tooling only. Nothing here submits an order, enables live trading, or adds an exchange/broker adapter.

| Step | Change | Evidence |
|---|---|---|
| 3B-1 | `backtesting/metrics.py`: computed from trades + marked equity (profit factor, expectancy, drawdown, longest under water, exposure, warnings). | `test_stage3b_metrics.py` |
| 3B-2 | `backtesting/validation.py`: cost stress (scale_costs), hold-out window, equal-period consistency, Monte-Carlo drawdown, variant ledger, `compare`, `weaknesses`; `thresholds.py` (owner-approved placeholders, 2026-11-05). | `test_stage3b_validation.py` |
| 3B-3 | `backtesting/regimes.py`: regime labelling and per-regime results. | `test_stage3b_regimes.py` |
| 3B-4 | `records.py` + migration 11 `backtest_runs` (append-only by DB trigger, SQLite and PostgreSQL); `gate.py`: promotion gate re-evaluates stored metrics against `OFFICIAL_THRESHOLDS`, newest run only, fingerprint must match; `manager.transition` enforces it; `review_existing` (human-triggered) applies it to strategies already PAPER_ELIGIBLE. | `test_stage3b_gate.py`, `test_strategy_packages.py` |
| 3B-5 | `report.py`, four read-only API routes, `cli.py` (run records are created only from the CLI, never from the API). | `test_stage3b_report.py`, `_cli.py` |
| 3B-6 | Backtesting page now shows real run records (demo generator removed); `backtests.js`, `index.standalone.html` rebuilt by `scripts/build_standalone.py`; CSP unchanged (`UI_ASSETS` whitelist). | `test_stage3b_ui.py`, browser test (stand-in harness locally) |
| 3B-7 | Replay speed-up: when flat with nothing resting, the order sync and exit monitor are skipped (`_Run._idle`). Equivalence tests replay with the shortcut forced off and require identical digests (stops, targets, gaps, both-hit, strategy exit, split entry, unfilled resting order, every built-in strategy). Resource test bounds ms/bar and peak memory. | `IdleShortcutIsOnlyASpeedUp`, `Resources` in `test_stage3a_replay.py` |

Local results at this commit: SQLite 743 passed / 39 skipped; PostgreSQL 16 full run found one test bug (`created_at='t'` is not a valid timestamptz) in `test_an_unreadable_record_is_not_evidence`, fixed; that file then passes on both backends. Hosted CI: run 42165684897 on 6705b4d, 3 of 3 attempts green (first run ffda500 failed only in docker: missing frontend mount, fixed).

Mutation checks done for the shortcut: forcing the shortcut on/off and ignoring open positions are killed. **Ignoring resting orders is NOT killed** (see audit, limits).

## Screening mode (`--screen`, 2026-11-07)
`python -m dewako.backtesting.cli ... --screen` runs ONE replay at the stated costs and prints trades, net P&L and max drawdown labelled `SCREENING_ONLY`.
It skips the cost stress, hold-out, periods and Monte Carlo, saves nothing (with or without `--dry-run`), and prints no verdict or checks, so the promotion gate can never use it.
Measured: 3 months of BTCUSDT 5m took 4m45s (24 months about 40 minutes) against about 4 hours for the full validation of the same data. Its numbers equal the full run's 1x numbers (tested). A strategy that survives screening still needs the full run. The 10% drawdown halt of the risk engine applies in screening too: a result ending at about -10% is a halt, not a full-period result.

## Replay fix: an exit-protection incident no longer latches forever (2026-11-08)

Found while backtesting BTC/ETH 2020-2026 on 1h: one stop on a crash bar (2020-03-12) hit a simulated spread above the 0.5% guard, the exit
monitor opened an EXIT_PROTECTION_UNVERIFIED incident, and the replay made no further trade for six years (1 trade in total).

Cause: the replay's idle shortcut skipped the exit monitor whenever the account was flat. Live, the monitor keeps running while flat and
resolves the incident after two clean cycles; the replay never ran those cycles, so new entries stayed blocked. The shortcut was documented as
a pure speed-up, and it was not one in this case.

Fix: an open EXIT_PROTECTION_UNVERIFIED / EXIT_BLOCKED_WIDE_SPREAD incident makes the replay not idle, so the monitor runs until it resolves.
Test: `test_an_exit_protection_incident_still_clears_after_the_position_closes_while_the_account_is_flat` (shortcut on and off must give the
same digest; it failed before the fix: 2 fills with the shortcut vs 3 without). Required in CI.

Not changed: the live candle-freshness rule. A maintenance gap inside the lookback window refuses decisions until the gap leaves the window
(300 bars by default), in live as in the replay. On the 2020-2026 data that blocks trading for a measurable share of the time; it is
reported with each run, not worked around.
