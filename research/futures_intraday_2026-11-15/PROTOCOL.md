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
