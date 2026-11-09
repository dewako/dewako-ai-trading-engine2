# Dewako ETH rules as a futures long + short strategy on 15m — test protocol (pre-registered 2026-11-10, before any 15m run)

Owner request: test the Dewako ETH strategy on futures, long and short, 15m. Research only: the engine's Dewako ETH (4h, spot, long only)
is not changed, and nothing here is added to the engine. The engine cannot replay shorts (spot refuses them), so `harness.py` re-implements
the rules bar by bar with the engine's fill timing (signal on a close, market order at the next open, stop watched along a worst-case
open -> low -> high -> close path for a long and open -> high -> low -> close for a short, a gap through the stop fills at the open) and the
engine's own spread + slippage model (SimConfig defaults). The engine's other risk limits (loss streaks, daily loss) are not modelled.

## Rules
Long: as Dewako ETH (close above the regime average and above the previous entry-bar high; stop entry - k x ATR; exit on a close below the
previous exit-bar low or below the regime average). Short: the exact mirror (close below the average and below the previous entry-bar low;
stop + k x ATR; exit on a close above the previous exit-bar high or above the average). Sizing: 0.5% of equity at risk, capped at 10% of
equity, 1x. 10,000 USDT.

## Readings (declared now)
| Reading | TF | regime / entry / exit bars | ATR length / stop | Note |
|---|---|---|---|---|
| R0 | 4h | 1200 / 20 / 10 | 14 / 2x | long only, fee 0.1%: harness check against the engine's own result (138 trades, +1,422) |
| R1 | 15m | 1200 / 20 / 10 | 14 / 2x | same bar counts (a different strategy: 12.5-day average, 5-hour breakout) |
| R2 | 15m | 19200 / 320 / 160 | 224 / 8x | same time spans as 4h (x16 bars; ATR stop x sqrt(16) so the stop width is about the same) |

R1 and R2 are each run long + short at three fee levels per side, with the engine's spread + slippage on top:
0.10% (owner rule, gate), 0.05% (futures taker, gate: entries are market orders), 0.02% (futures maker, reference only). 2x stress doubles
the fee, spread and slippage. Zero cost is a diagnostic. Spot ETHUSDT 5m (resampled) stands in for the perpetual: the futures archive is
unreachable here. Funding is not in the P&L; the 8-hour funding times crossed are counted and a 0.01% per crossing stress cost reported.

## Gate
Same five checks as the engine (100+ trades, positive at 2x costs, max DD <= 15%, out-of-sample >= 50% of in-sample and positive,
2 of 4 periods profitable). "Works" = R1 or R2 passes at the 0.05% taker fee. Even then it is not added: futures is a separate stage.
No tuning after these runs. About 100 variants have now been tried on this ETH data, so any single pass is optimistic.

## Results (added after the runs; nothing above was changed). **Every 15m reading FAILED. Not added.**
Harness check (R0, 4h long only, fee 0.1%): 139 trades, +1,477, DD 7.7%, periods +1,509 / -509 / +1 / +478, PASSED. The engine's own
run: 138 trades, +1,422, DD 8.2%, periods +1,476 / -464 / +3 / +435. Close enough to trust the harness's direction and size; its 2x-cost
figure (+1,021 vs the engine's +509) and the OOS split differ, so the harness's 2x result is the more optimistic of the two.

| Reading | Fee | Trades (L / S) | Long P&L | Short P&L | Net | 2x | Max DD | OOS vs IS | Periods | Failed checks |
|---|---|---|---|---|---|---|---|---|---|---|
| R1 bar counts | 0.10% | 4,689 (2,487 / 2,202) | -3,817 | -3,650 | -7,467 | -9,398 | 75.4% | -0.73 vs -1.88 | -3,991 / -1,439 / -1,149 / -887 | 4 of 5 |
| R1 | 0.05% taker | 4,689 | -2,910 | -3,042 | -5,951 | -8,461 | 61.2% | -0.65 vs -1.47 | -3,230 / -1,017 / -899 / -806 | 4 of 5 |
| R1 | 0.02% maker (ref) | 4,689 | -2,157 | -2,479 | -4,636 | -7,299 | 48.9% | -0.49 vs -1.15 | -2,728 / -666 / -617 / -625 | 4 of 5 |
| R2 time spans | 0.10% | 318 (180 / 138) | +1,646 | -346 | +1,300 | +163 | 8.9% | -3.35 vs 6.55 | +1,676 / -144 / -97 / -135 | OOS, periods |
| R2 | 0.05% taker | 318 | +1,853 | -198 | +1,655 | +811 | 6.9% | -2.27 vs 7.67 | +1,734 / -42 / +3 / -40 | OOS |
| R2 | 0.02% maker (ref) | 318 | +1,980 | -107 | +1,873 | +1,219 | 6.3% | -1.59 vs 8.36 | +1,769 / +20 / +64 / +19 | OOS |

R1 has no edge even before costs (+649 at zero cost over 4,689 trades). R2: the shorts lose at every fee; the profit is the long side, and
about 95% of it was made in the first period (2020-01..2021-09). The last 25% of trades lose money (out-of-sample expectancy negative),
and without its 3 best trades the taker result is +218. Mean hold 65 h, about 8 funding times per trade: a 0.01% per crossing stress
would cost about 290 more. Funding is not included above.
