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
