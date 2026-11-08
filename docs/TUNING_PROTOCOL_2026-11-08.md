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
