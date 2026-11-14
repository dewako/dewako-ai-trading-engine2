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
