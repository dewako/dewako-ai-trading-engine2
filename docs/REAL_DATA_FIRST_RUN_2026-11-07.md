# First real-data run: BTCUSDT 1h, 2025-11-01 .. 2026-11-01 (owner-supplied Binance spot archive)

Data: BTCUSDT 5m archive resampled to 1h (`binance_archive:resampled:5m`, complete 12-candle buckets only), 8,760 bars, passes the dataset
validator. Archive had no checksum files: integrity UNVERIFIED. 1h is the only timeframe all six strategy versions support (5 of 6 do not
support 5m). Last 25% held out; 4 equal periods; costs: fee 0.1%, spread/slippage/impact as SimConfig defaults (assumed, not calibrated).
Scratch databases only; nothing was saved to the application database. `streak_reset=daily` (see defect 2).

## Result: all six FAIL the promotion gate. No strategy shows an edge here.
| Strategy | Trades | Net P&L (10k) | Max DD | Net at 2x / 3x costs | In-sample / out-of-sample net | Win rate |
|---|---|---|---|---|---|---|
| AI Strategy 0.3 | 34 | -537 | 5.8% | -631 / -742 | -537 / -30 (3 trades) | 0% |
| Breakout AI 1.8 | 53 | -194 | 2.8% | -390 / -584 | -273 / +81 (17 trades) | 32% |
| Mean Reversion 1.2 | 92 | -544 | 5.8% | -882 / -990 | -461 / -105 | 40% |
| Momentum AI 1.4 | 37 | -621 | 6.7% | -794 / -948 | -621 / -39 (5 trades) | 0% |
| Trend AI 2.1 | 28 | -512 | 5.5% | -678 / -794 | -512 / -22 (2 trades) | 0% |
| Trend AI 2.2 | 22 | -409 | 4.7% | -528 / -646 | -409 / -11 (1 trade) | 0% |
Every strategy also fails "enough trades" (need 100), "positive at 2x costs", "out-of-sample holds up" and "consistent across periods".
Long side only: spot refuses the short entries all of these strategies emit.

## Findings about the strategies (not the replay)
- Trend AI 2.1/2.2, Momentum AI 1.4 and AI Strategy 0.3 set an ATR stop and NO take-profit, no trailing stop and no exit signal
  (`manage_position` returns None in the base class and they do not override it). Every trade therefore ends at its initial stop: they can
  never book a profit by design. Example: Trend AI 2.2 entered 2025-12-19, rose 11.4%, and was stopped out at -2% 37 days later.
  Their 0% win rate is structural, not a data result. Breakout AI and Mean Reversion set take-profits and do win trades.

## Defects found in this project's own tooling while doing this run (all fixed, tested, mutation-checked)
1. Stage 3A replay passed bare candles to strategies (volume_ratio always 1.0, no trend/momentum/regime). Volume-gated strategies could never fire:
   4 of 6 made zero trades. Now the replay uses the live state builder (`state_source=market_state`, 59 warm-up bars counted). The old mode is kept as
   `candles_only` for plumbing tests; the gate refuses runs made with it and refuses records that do not state the source.
2. The risk engine's max-consecutive-losses limit latches (only a win resets it, and no trade can happen once it trips), so a year-long run measured only
   the first 1-2 months. `streak_reset` is now an explicit, recorded replay policy ("never" = platform as is; "daily" = a person resumes at UTC midnight).
   A latched halt above 10% of ticks produces a visible warning. Live limits are unchanged. NOTE: live, 4 consecutive losses halt the bot with no
   automatic reset - the owner should decide whether that is intended.
3. Cost stresses (2x, 3x) pushed the simulated spread past the 0.5% quote guard, an exit-protection incident latched, and trading stopped
   (2 trades instead of 53), so "positive at 2x costs" compared different things. Stress runs now raise the guard ceiling with the costs.
4. Known, not fixed: the simulated spread is a fraction of the candle's range, so on 1h bars in volatile hours it can exceed 0.5% (real BTCUSDT
   spread is far smaller). This penalises every strategy equally in volatile hours; the cost model remains assumed, not calibrated.

## What this does and does not show
It shows the pipeline works end to end on 12 months of real data and that these particular strategies, long-only on 1h BTC in this period with
these assumed costs, do not pass. It does not show that no profitable strategy exists, and one year of one asset
is a single regime: BTC closed at about 109,700 on 2025-11-01 and about 77,600 on 2026-10-31 (low 57,800 in between), a falling year, which is the hardest case for a long-only strategy. ETHUSDT was not run (4 spike candles await a human decision).
