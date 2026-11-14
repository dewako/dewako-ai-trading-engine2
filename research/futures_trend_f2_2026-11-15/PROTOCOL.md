# F2: trend rule, long + short, on futures costs — test protocol (pre-registered 2026-11-15, before any run)

Owner request: "F2" (docs/FUTURES_PAPER_STAGE.md): find out whether a long + short strategy survives on futures costs at 1x, with
2x and 3x reported. This file is committed before `run.py` is executed for the first time. Nothing below the "Results" line exists yet.

## The rule (textbook values, nothing tuned, the same on every coin)
Daily candles (UTC days). On each daily close, for a coin with no open position:
* close above the highest high of the previous 55 days -> go **long** at the next day's open;
* close below the lowest low of the previous 55 days -> go **short** at the next day's open.
Exit: trailing stop only. Long stop = highest close since entry minus 3 x ATR(20, Wilder); short stop = lowest close since entry plus
3 x ATR(20). The stop is updated at each daily close and never moves against the position. A touch during the day fills at the stop;
a gap through it fills at the open. No targets, no reversal: after an exit the coin waits for a new 55-day breakout.

## Account and sizing
One shared account, 10,000 USDT, compounding, run through the paper futures core (`dewako/futures/paper.py`), ISOLATED margin.
Size per trade: quantity = equity x risk / (3 x ATR at the signal), so a stop-out costs about `risk` of equity before costs.
* **1x (the verdict): risk 1%, leverage setting 1.**
* 2x (reported): risk 2%, leverage setting 2. 3x (reported): risk 3%, leverage setting 3.
Position value is capped at leverage x equity / 3 per coin. An entry is skipped (and counted) if the paper core's `entry_guard`
refuses it (liquidation less than 2x the stop distance away).

## Data
Binance **spot** 5m candles resampled to UTC days: BTCUSDT and ETHUSDT 2020-01-01..2026-10-31, SOLUSDT from 2020-08-11. Spot stands
in for the perpetual. Only these three coins are available here; the 5 to 8 coins proposed earlier need more data files.

## Costs
* **Verdict cost: fee 0.10% per side (the owner's rule) + 0.05% slippage.** Funding 0.01% per 8 hours on the position value: longs
  pay, **shorts receive nothing** (real funding varies and is sometimes negative; this is the cautious assumption).
* 2x stress: fee 0.20% + slippage 0.10%, same funding.
* Reported only: Binance taker (0.05% + 0.01%); funding paid by both sides.

## Verdict: "passes" = all six, at 1x, at the verdict cost, for the three coins together
1. at least 100 trades;
2. net profit positive at the 2x stress cost;
3. maximum drawdown of daily equity (open positions included) at most 15%;
4. out of sample: average R per trade for entries from 2024-01-01 is positive and at least 50% of the average for entries before it
   (R = trade profit after costs / the amount risked at entry);
5. at least 2 of 4 equal calendar periods have a positive net profit;
6. maximum drawdown below buy-and-hold's (a third of the account in each coin at its first day, never rebalanced). A low bar:
   return / drawdown for both is reported next to it.
No retuning after the results. If it fails, it fails.

## Also reported (not part of the verdict)
* each coin on its own; long trades and short trades separately; every calendar year;
* result without the best 1, 3 and 5 trades;
* neighbours 40 and 70 days, 2.5 and 3.5 ATR (a plateau or a peak?), at 1x and the verdict cost;
* 2x and 3x: profit, drawdown, liquidations, entries skipped by the guard.

## Limits known before the run
* Three coins that all rose enormously over the period; shorts faced a rising market. Three coins move together, so this is not three independent tests.
* Roughly 60 to 100 trades are expected, so check 1 may fail on count alone.
* Daily candles: the order of the high and low inside a day is unknown. A day that touches the stop counts as stopped.
* The 2024+ period is "out of sample" only for this rule's fixed textbook values; nothing was fitted on the earlier period either.

## Results (added after the runs; nothing above was changed)

**Verdict: FAIL (5 of 6 checks passed; check 4, out of sample, failed). Nothing retuned.**

### Two harness faults found after the first run (the rule and the protocol were not changed)
The first run printed PASS. Its numbers did not add up: the trades' profits summed to 9,505 while the account gained 5,709.
1. **Trade profit was measured from the whole account**, so a trade that was open while another coin's trade closed was credited
   with that other trade's result. Fixed: a trade's profit is its own coin's ledger entries. This changed every per-trade figure
   (R values, by side, by year, best trades) and check 4. Net profit and drawdown come from the equity curve and did not change.
2. **A stop touched on the entry day was not checked until the next day.** Fixed. It changed nothing at 55 days / 3 ATR and
   moved two of the 2.5 ATR neighbours slightly.
Each fix has a test that fails without it. The first run is kept in `results/results_first_run_with_harness_faults.json`.

### Verdict checks (1x, fee 0.10% + slippage 0.05%, BTC + ETH + SOL together, 2020-01..2026-10)
| Check | Result | |
|---|---|---|
| 1. at least 100 trades | 116 | pass |
| 2. positive at 2x costs | +5,286 | pass |
| 3. max drawdown at most 15% | 11.06% | pass |
| 4. out of sample (2024+) avg R positive and at least 50% of in-sample | 0.201 R (47 trades) vs 0.546 R (69 trades) = **37%** | **FAIL** |
| 5. at least 2 of 4 periods profitable | 4 of 4 (+3,005, +651, +1,167, +885) | pass |
| 6. max drawdown below buy-and-hold | 11.06% vs 88.88% | pass |

### 1x results
| Cost | Trades | Win | Net | Max DD | Avg R |
|---|---|---|---|---|---|
| **Verdict: 0.10% + 0.05%** | 116 | 49% | **+5,709** (10,000 -> 15,709, about 6.8% a year) | 11.06% | 0.41 |
| 2x stress: 0.20% + 0.10% | 116 | 47% | +5,286 | 11.47% | 0.39 |
| Binance taker: 0.05% + 0.01% | 116 | 49% | +5,936 | 10.86% | 0.42 |
| Verdict cost, shorts also pay funding | 116 | 47% | +5,368 | 11.40% | 0.39 |

Buy-and-hold of the same three coins: +195,898 with an 88.88% drawdown (return / drawdown 22.0, the rule's 5.2).

* **By side:** long 77 trades +4,864; short 39 trades +849. Short only: +612 with a 10.2% drawdown. Shorts added little.
* **By entry year:** 2020 +736, 2021 +2,314, 2022 +293, 2023 +1,023, 2024 +569, 2025 +224, 2026 +553. Every year positive; 2021 is 40% of the total.
* **Without the best trades:** all +5,713; without the best 1: +4,745; best 3: +2,866; best 5: +1,990. The best three are all SOL longs.
* **Each coin alone:** BTC +1,182 (41 trades), ETH +1,049 (40), SOL +2,707 (35).

### Neighbours (1x, verdict cost): net / max drawdown / trades
| | 2.5 ATR | 3.0 ATR | 3.5 ATR |
|---|---|---|---|
| 40 days | +6,212 / 13.5% / 162 | +6,482 / 14.1% / 143 | +7,840 / 11.8% / 123 |
| 55 days | +5,667 / 10.4% / 133 | **+5,709 / 11.1% / 116** | +7,053 / 8.7% / 99 |
| 70 days | +4,527 / 9.5% / 115 | +5,150 / 9.4% / 98 | +6,359 / 8.7% / 83 |
All nine are positive with drawdowns under 15%: a plateau, not a peak. The verdict stays with the pre-registered 55 / 3.0.

### Leverage (verdict cost)
| | Trades | Net | Max DD | Entries refused by the guard | Liquidations |
|---|---|---|---|---|---|
| 1x (risk 1%) | 116 | +5,709 | 11.06% | 3 | 0 |
| 2x (risk 2%) | 105 | +10,225 | 20.75% | 44 | 0 |
| 3x (risk 3%) | 74 | +6,527 | 22.43% | 168 | 0 |
2x doubles the profit and doubles the drawdown (over the 15% limit). At 3x the guard refuses most entries: a 3 ATR stop on daily
candles is usually more than half the distance to a 3x liquidation price. No position was ever liquidated in any run.

### What this does and does not show
* It is the first rule tested here that is positive in every year, at double costs, and on every neighbouring setting.
* It still fails the pre-registered test: the edge after 2023 is 37% of the edge before it. A fading edge is the usual sign of
  an effect that is weakening or was partly luck.
* Three coins that move together are closer to one test than three. The best trades are concentrated in SOL.
* Spot candles stood in for perpetuals; funding was a constant, not the real history.
* The honest next test is the same rule, unchanged, on coins never looked at (new data files needed), not a change to this one.
