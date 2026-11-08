# "Dewako" 1.0 — design and test protocol (pre-registered 2026-11-08, before any run of it)

Owner request: build a new strategy from what the last three days of testing showed, name it Dewako, and add it to the project only if it
qualifies. It lives in `dewako_plugin.py` here and is loaded with `--plugin`; it is not in `dewako/strategies/` and not registered at startup.

## What the testing showed, and the design rule each lesson gives
| Finding (source) | Design rule |
|---|---|
| Strategies without a real exit ended every trade at the stop (REAL_DATA_FIRST_RUN_2026-11-07) | An explicit exit from the first version. |
| Fast intraday trading has an edge before costs of 0.007-0.02% per trade vs ~0.24% cost (Pine breakout, 5m/15m) | 4h bars, few trades, let winners run: no fixed target. |
| 4h trend rules made money before 2025 and decayed after (tuning 2026-11-08) | Trade only in a long-term uptrend; step aside entirely otherwise. |
| Tuned settings did not hold out of sample (tuning 2026-11-08) | No tuning: every number is a textbook default fixed here. |
| Trend AI 2.3.11 had too few trades for the gate (75-77 in 6.8 years) | A more frequent entry trigger than an EMA cross with ADX + structure filters. |

## The rules (spot, long only, 4h; no parameter will be changed after this file is committed)
* Regime: close above its 200-day simple average (1,200 4h closes). No regime, no entry; regime lost, exit.
* Entry: close above the highest high of the previous 20 bars (Donchian 20, the Turtle System 1 entry).
* Initial stop: entry - 2 x ATR(14) (the Turtle 2N stop). Held by the engine's exit monitor.
* Exit: close below the lowest low of the previous 10 bars (Turtle System 1 exit), or close below the 200-day average.
* Sizing and every risk limit: the engine's own, unchanged (0.5% of equity at risk per trade, position capped at 10% of equity).

## Test
* Data as in TUNING_PROTOCOL_2026-11-08: Binance spot BTCUSDT and ETHUSDT, 5m resampled to 4h, 2020-01-01..2026-11-01,
  maintenance holes up to 3 candles tolerated, flagged crash candles accepted by the owner.
* Engine replay with `--lookback 1210` (the 200-day average needs 1,200 closes, so the first ~200 days of 2020 are warm-up with no entries).
* Real fee 0.1% per side, `--streak-reset daily`. One full promotion-gate run per coin, `--variants-tried 76`
  (15 untuned screens + 52 tuning variants + 8 Pine runs on this same data + this one).
* The gate's hold-out (last 25%) is roughly 2025-02 onward, the period where every earlier strategy lost its edge.
* Buy-and-hold over the same window is reported next to it (information, not a gate check), both for a full position and for a 10% position
  (the most the engine lets any one trade hold).

## What "qualifies" means
The full gate passes for that coin. Dewako is added to the project only for a coin where it passes, as a candidate version (never a default
until the owner promotes it). A fail is reported as it is, and no Dewako 1.1 is tuned on this data.

## Honest limit
The design used lessons from this same 2020-2026 data, so this is not a clean out-of-sample test. A pass would be a reason to paper-trade it
forward on new prices, not proof.

## Results (added after the runs; nothing above was changed)
| Coin | Trades | Net | 2x costs | 3x costs | Max DD | OOS vs IS expectancy | Periods | Gate |
|---|---|---|---|---|---|---|---|---|
| BTC | 132 | +2,247 | +1,804 | - | 3.6% | -6.54 vs 26.49 | 3 of 4 | **FAILED** (out-of-sample) |
| ETH | 138 | +1,422 | +509 | +444 | 8.2% | 20.71 vs 8.09 | 3 of 4 | **PASSED** |

Buy and hold, same window: BTC +978%, ETH +1,646%. 10% of the account in ETH, never rebalanced: 10,000 -> 26,325 (account max DD 65%).
Dewako ETH: 10,000 -> 11,422 (max DD 8.2%).

### ETH pass, examined (results/inspect_ETHUSDT_4h.json)
* Without its 3 best trades (+730 2020-12, +587 2020-07, +446 2024-02) the 6.8-year result is **-341**; without the best 5, -1,108.
* Hold-out (from 2025-05-23): 21 trades, +435. Without its 3 best, -290. One trade (2025-07, +436) carries it.
* Period P&L: +1,476 / -464 / **+3** / +435. The "3 of 4 profitable" check passes on a +3 USDT period.
* 39 winners avg +121.5, 99 losers avg -33.5: the usual trend-following shape (few large winners), so concentration alone is not proof of
  luck, but with 21 hold-out trades the out-of-sample check is decided by one trade.
* Monte Carlo drawdown p95 10.4%, worst 15.1%.

### Decision
The pre-registered rule (gate passes) is met for ETH. It is **not added to the project**: the pass rests on 2 checks each decided by one
trade or by +3 USDT, it is the 1 pass among ~76 variants tried on this data, its design used lessons from this data, and it earned 11x
less than holding 10% of the account in ETH. This extra condition was stated to the owner after the gate result and before this
examination; it is stricter than the protocol, never looser. BTC: failed. Per the protocol, no Dewako 1.1 is tuned on this data.
Next honest test: forward paper trading on prices after 2026-11-01.
