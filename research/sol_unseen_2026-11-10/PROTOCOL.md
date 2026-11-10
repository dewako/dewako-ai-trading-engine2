# SOLUSDT as an unseen coin — test protocol (pre-registered 2026-11-10, before any strategy run on SOL)

Owner supplied Binance spot SOLUSDT 5m klines (75 monthly files, 2020-08-11..2026-10-31). No strategy has ever been run on SOL here.

## What is run: 2 runs, no parameter changed, no tuning afterwards whatever the result
1. Dewako ETH 1.0 (engine library, defaults).
2. Dewako Flow 1.0.6 (the tuned variant: VWAP 30, exit 1 ATR, stop 3 ATR), via `../dewako_flow_tuning_2026-11-10/variants.py`.
SOL/USDT 4h (5m resampled), 2020-08-12..2026-11-01, `--lookback 1210`, fee 0.1% per side, cash 10,000, `--streak-reset daily`,
`--max-gap-intervals 3`, the full promotion gate, `--variants-tried 2` (two variants on this coin; neither was fitted to it).
The 200-day filter needs 1,200 candles, so trading can start only around March 2021.

## Data handling (done before this protocol, without looking at any strategy result)
* 6 rows with a malformed close time were dropped, all at the same exchange-outage moments as the BTC/ETH rows dropped earlier
  (`results/dropped_rows.json`). Remaining gaps are the same maintenance windows already listed for BTC/ETH.
* 475 four-hour candles trip the engine's 10% spike check (227 of them in 2021). They are accepted in bulk as real SOL moves
  (`results/spikes_accepted.json`); I checked them against the 5m candles but no person reviewed them one by one. In 58 of them
  one extreme appears in a single 5m candle (a wick). File integrity is unverified (no checksums), as for BTC/ETH.

## What a result means
* Pass on SOL = the rules work on a coin they were not built on. It is still not independent proof: SOL moves with ETH and BTC, and SOL
  is a coin picked today knowing it survived and rose strongly (survivorship). A pass is examined (top trades, by year) before any claim.
* Fail = reported as it is. Neither strategy is retuned on SOL.

## Results (added after the runs; nothing above was changed). **Both FAILED the gate on SOL. Nothing retuned.**
| Strategy | Trades | Net | 2x costs | Max DD | OOS vs IS expectancy | Periods | Gate |
|---|---|---|---|---|---|---|---|
| Dewako ETH 1.0 | 109 | +2,348 | +1,899 | 7.7% | 10.05 vs 23.65 (42%; needs 50%) | 3 of 4 | **FAILED** (out-of-sample) |
| Dewako Flow 1.0.6 | 83 | +723 | +526 | 4.6% | -14.26 vs 14.53 | 2 of 4 | **FAILED** (too few trades; out-of-sample) |
Both are profitable overall and at 2x costs, and both earn much less (Flow: loses) in the most recent quarter of their trades.
Dewako ETH 1.0 fails one check, by 8 points. Dewako Flow 1.0.6 fails two; its ETH pass after tuning did not carry over to SOL.
