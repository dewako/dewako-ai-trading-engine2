# Parameter tuning protocol — BTC/ETH 4h, 2020-2026 (pre-registered 2026-11-08)

Written before any tuning run. The owner asked for the strategies to be improved by tuning. This file fixes how, so that the result can still
be believed.

## Data
Binance spot archive, BTCUSDT and ETHUSDT 5m, 2020-01 to 2026-10, resampled to 4h. 16 exchange-maintenance holes (1-3 candles on 4h) are
explained gaps and tolerated in the decision window (`--max-gap-intervals 3`). 16 partial candles at the start of a halt were dropped.
Crash candles the data check flags (56 BTC, 133 ETH on 4h) were accepted by the owner.

## Split
* Training: 2020-01-01 to 2024-01-01. All tuning happens here.
* Test: 2024-01-01 to 2026-11-01. Not used to choose anything. The chosen setting is run on it once.

Caveat: the 2024-03..2026-10 data was already used on 2026-11-07, when the missing exits of Trend AI / AI Strategy were found and the
2.3 / 0.4 exit versions were written. The exits were derived from the entry rules, not tuned, but the test window is not virgin.

## Grid (fixed, 52 variants)
* Trend AI 2.3: (ema_fast, ema_slow) in (10,50) (20,50) (20,100) (50,200); adx_min 15 / 25; atr_stop 2 / 3. 16 variants.
* AI Strategy 0.4: min_volume_ratio 0.8 / 1.0 / 1.2 / 1.5; atr_stop 1.5 / 2 / 3. 12 variants.
* Mean Reversion 1.2: bb_dev 2 / 2.5; rsi_low 25 / 30 / 35; atr_stop 2.5 / 3.5; adx_max 25 / 30. 24 variants.
Every other parameter stays at its default. No other variants will be tried in this round.

## Selection (training only)
One screening replay per variant and symbol, fee 0.2% per side (twice the real fee). Same parameters must serve both coins.
Score = the lower of the BTC and ETH net P&L. A variant needs at least 40 closed trades on each coin in training.
The best-scoring variant per strategy is chosen. If none scores above 0, that strategy is reported as not improvable by this grid.

## Test
Each chosen variant: one screening run on 2024-01-01..2026-11-01 at the real fee (0.1%), and the full promotion gate over 2020-2026 with
`--variants-tried` = 52 + the 15 untuned screens = 67. The gate's hold-out (last 25%) lies inside the test window.
Pass = the gate passes. The result is reported whatever it is; a fail is not followed by another grid on the same data.

## Results (added 2026-11-08, after all runs; nothing above was changed)

### Selection on training (2020-01-01..2024-01-01, fee 0.2%)
* Trend AI: **2.3.11** (ema 20/100, adx_min 25, atr_stop 2). BTC 45 trades +2,424; ETH 43 trades +3,697. Score +2,424.
* AI Strategy: **0.4.8** (min_volume_ratio 1.2, atr_stop 2). BTC 141 trades +977; ETH 153 trades +1,891. Score +977.
* Mean Reversion: best eligible score -159 (1.2.12). **Not improvable by this grid.** No variant made money on both coins.
* Raw rows: research/tuning_2026-11-08/results_train.jsonl.

### Test screen (2024-01-01..2026-11-01, fee 0.1%, screening only)
| Variant | Coin | Trades | Net P&L | Max DD |
|---|---|---|---|---|
| Trend AI 2.3.11 | BTC | 31 | +914 | 4.1% |
| Trend AI 2.3.11 | ETH | 31 | +888 | 5.9% |
| AI Strategy 0.4.8 | BTC | 112 | +298 | 3.0% |
| AI Strategy 0.4.8 | ETH | 112 | +127 | 5.4% |

### Full promotion gate (2020-01-01..2026-11-01, fee 0.1%, variants-tried 67, max-gap 3): **all four FAILED**
| Variant | Coin | Trades | Net | 2x costs | DD | Out-of-sample vs in-sample expectancy | Periods | Fails on |
|---|---|---|---|---|---|---|---|---|
| Trend AI 2.3.11 | BTC | 77 | +3,644 | +3,335 | 7.1% | 10.24 vs 58.34 (18%) | 3/4 | trades, OOS |
| Trend AI 2.3.11 | ETH | 75 | +4,961 | +4,562 | 5.9% | 18.53 vs 80.48 (23%) | 4/4 | trades, OOS |
| AI Strategy 0.4.8 | BTC | 255 | +1,607 | +814 | 3.4% | -2.18 vs 9.80 | 3/4 | OOS |
| AI Strategy 0.4.8 | ETH | 266 | +2,487 | +1,586 | 5.7% | 0.35 vs 12.91 (3%) | 4/4 | OOS |

### Reading
* Tuning did not produce a strategy that passes. Per this protocol, no second grid is run on this data.
* The common failure is the hold-out (last 25% of 2020-2026, roughly 2025-02 onward): edge per trade drops to 3-23% of its earlier level,
  or below zero. For AI Strategy the positive test-window screens must therefore come mostly from 2024, before the hold-out (inferred from the two numbers, not measured month by month).
* Trend AI also trades too rarely on 4h (about 11 trades per coin per year) for the 100-trade minimum.
* Next honest options are new hypotheses, not more knobs: a 1h timeframe (more trades), a regime filter judged on fresh data
  (2026-11 onward, paper only), or the futures/short stage once approved.
