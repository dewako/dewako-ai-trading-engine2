# Intraday futures strategies on 5m and 15m — test protocol (pre-registered 2026-11-15, before any run)

Owner request: "build different strategies for futures mostly on 5m and 15m time frame". Four standard ideas, each with its textbook
settings, long + short, tested on both timeframes: 8 cells. This file is committed before `run.py` is executed for the first time.

## The four rules (same settings on every coin and both timeframes; nothing tuned)
Signals are decided on a bar's close and filled at the next bar's open. One position per coin. ATR and RSI use Wilder's smoothing.
| Code | Idea, and why it might work | Entry | Exit |
|---|---|---|---|
| **BRK** | Day-range breakout: a move beyond the last day's range tends to continue | close above the highest high (below the lowest low) of the previous day of bars (288 on 5m, 96 on 15m) | stop 2 ATR(14) from entry, trailed on closes, never loosened |
| **MR** | Bollinger reversion: stretched prices snap back | close below the lower Bollinger band (20, 2) with RSI(14) < 30 -> long; above the upper band with RSI > 70 -> short | close back across the 20-bar average; fixed stop 2 ATR(14); time stop 48 bars |
| **PB** | Pullback in a trend: join the trend after a dip | EMA50 > EMA200 and close > EMA200; the bar's low touches EMA20 and it closes above EMA20 and above its open -> long (mirror for short) | fixed stop 1.5 ATR(14); target 2 x the stop distance |
| **SQZ** | Squeeze breakout: quiet periods end in a move | previous bar had Bollinger (20, 2) inside Keltner (20-bar average +/- 1.5 ATR(20)); this bar closes outside the Bollinger band -> trade that side | close back across the 20-bar average; fixed stop 2 ATR(14) |
Exits decided on a close fill at the next open. Stops: a touch fills at the stop, a gap fills at the open; the entry bar is checked
too. A bar that touches both stop and target counts as the stop.

## Account, sizing, fills
10,000 USDT split into three equal sub-accounts (BTC, ETH, SOL), each compounding on its own, **1x**: position value never above the
sub-account's equity. Size = 1% of the sub-account at risk for the stop distance, then capped at 1x (on these timeframes the cap
usually binds, so the real risk per trade is smaller than 1%). R = trade profit after costs / (quantity x stop distance).
Every fill (entry, stop, target, exit) is a taker fill with slippage against the trade.
Funding 0.01% per 8 hours on the position value, charged per bar held: longs pay, shorts receive nothing.

## Data
Binance spot 5m BTCUSDT, ETHUSDT 2020-01-01..2026-10-31 and SOLUSDT from 2020-08-11; 15m resampled from 5m. Spot stands in for
the perpetual.

## Costs
* **Verdict: fee 0.10% per side (the owner's rule) + 0.05% slippage.**
* 2x stress: 0.20% + 0.10%.
* Reported: Binance taker 0.05% + 0.01%; maker-like 0.02% + 0; **no costs at all** (is there any edge before costs?).

## Verdict per cell (rule x timeframe), three coins together, at the verdict cost
1. at least 100 trades; 2. net profit positive at the 2x stress cost; 3. max drawdown (daily, realized equity) at most 15%;
4. average R for entries from 2024-01-01 positive and at least 50% of the average before; 5. at least 2 of 4 equal calendar periods profitable.
"Passes" = all five. Eight cells are tested, so a single pass could be luck: a passing cell is a candidate to be confirmed on
coins it has not seen, not a result. No retuning after the results.

## Also reported
Per cell and cost: trades, win rate, net, drawdown, average R; long and short separately; each coin; each year (verdict cost).

## Limits known before the run
* A round trip at the verdict cost is about 0.30% of the position. A typical 5m bar range on BTC is 0.1% to 0.3%.
  The earlier short-timeframe test (Scalper 2.2) lost on all six charts for this reason.
* Inside a bar the order of high and low is unknown; touching both counts as the stop (pessimistic).
* Three coins that move together. Spot candles, constant funding.
* Drawdown is measured on realized equity sampled daily, so losses inside open trades and inside a day are not seen.

## Results (added after the runs; nothing above was changed)

**Verdict: all 8 cells FAIL. Each passes only check 1 (trade count). At the verdict cost every cell loses the whole account. Nothing retuned.**

### Net result on 10,000 USDT, 2020-01..2026-10, BTC + ETH + SOL (drawdown in brackets)
| Cell | Trades | **Verdict 0.10% + 0.05%** | Taker 0.05% + 0.01% | Maker-like 0.02% | No costs |
|---|---|---|---|---|---|
| BRK 5m | 15,648 | **-10,000** (100%) | -9,947 | -7,519 | +7,020 (35%) |
| BRK 15m | 10,006 | **-9,994** (100%) | -9,241 | -2,837 | +11,647 (29%) |
| MR 5m | 48,095 | **-10,000** (100%) | -10,000 | -9,928 | +9,317 (57%) |
| MR 15m | 16,247 | **-10,000** (100%) | -9,968 | -9,428 | -7,341 (74%) |
| PB 5m | 84,921 | **-10,000** (100%) | -10,000 | -9,989 | +124,549 (52%) |
| PB 15m | 25,937 | **-10,000** (100%) | -9,984 | -6,429 | +43,724 (32%) |
| SQZ 5m | 49,343 | **-10,000** (100%) | -10,000 | -9,963 | +2,477 (91%) |
| SQZ 15m | 19,775 | **-10,000** (100%) | -9,986 | -9,268 | -3,632 (64%) |
Most of each loss falls in 2020 and 2021 because the account is nearly empty after that; the average R per trade is as bad or
worse from 2024 on (for example BRK 15m: -0.27 R before 2024, -0.39 R after). Long and short both lose in every cell.

### Why: the move per trade is about one tenth of the cost, or less
Average result per trade as a percentage of the position value:
| Cell | Before costs | At the verdict cost | Average hold |
|---|---|---|---|
| BRK 5m | -0.025% | -0.326% | 9 bars |
| BRK 15m | -0.031% | -0.329% | 11 bars |
| MR 5m | +0.018% | -0.282% | 7 bars |
| MR 15m | +0.007% | -0.295% | 8 bars |
| PB 5m | +0.008% | -0.291% | 11 bars |
| PB 15m | +0.017% | -0.282% | 12 bars |
| SQZ 5m | -0.010% | -0.309% | 12 bars |
| SQZ 15m | -0.023% | -0.324% | 12 bars |
Before costs the four ideas earn between -0.03% and +0.02% of the position per trade: no measurable edge. A round trip at the
verdict cost is 0.30%. Even at a maker-like 0.02% per side (0.04% a round trip) all eight lose. The "no costs" column is positive
for six cells only through position sizing and compounding on a near-zero average; it is not evidence of an edge.

### What this shows
* The failure is not in the settings. To survive a 0.30% round trip, a rule holding about an hour would need to earn roughly
  twenty times what these standard ideas earn before costs. Tuning does not close a gap of that size; it only fits noise.
* Leverage multiplies the position, so it multiplies the cost in the same proportion. It cannot help.
* Together with Scalper 2.2 (6 charts) this makes 14 short-timeframe cells tested, 14 failed.
* What the numbers leave open on these timeframes: (a) the move per trade must be many times larger, which means holding for
  hours to days and using 5m/15m only to time entries of a slower signal; (b) costs far lower than a taker's, which means resting
  limit orders, and that cannot be tested honestly on candles (no queue position, no knowledge of whether a limit order filled).

### Harness notes
The first run crashed while saving (a numpy value in the JSON output); the printed numbers were identical on the rerun. A
position-value field was added to each trade after the first results, for the "per trade" table only; it changed no result (checked:
every cell's figures are identical before and after). In every run the trades' profits add up to the account's change (asserted).
