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

Local results at this commit: SQLite 743 passed / 39 skipped; PostgreSQL 16 full run found one test bug (`created_at='t'` is not a valid timestamptz) in `test_an_unreadable_record_is_not_evidence`, fixed; that file then passes on both backends. A full PG re-run and hosted CI are still to be recorded.

Mutation checks done for the shortcut: forcing the shortcut on/off and ignoring open positions are killed. **Ignoring resting orders is NOT killed** (see audit, limits).
