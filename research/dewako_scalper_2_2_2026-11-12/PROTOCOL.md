# Dewako Scalper 2.2 (owner's Pine strategy) — test protocol (pre-registered 2026-11-12, before any run)

Owner request: "check this strategy if it passes". Source: `Dewako_Scalper_2.2_STRATEGY.pine` (copied unchanged). Futures, long + short,
so it cannot run in the engine (no shorts); `harness.py` re-implements the script's own trade tracker bar by bar. Not added to the engine.

## What is tested: the script exactly as shipped, default inputs, no tuning
| Chart | Preset | Main settings (from the script) |
|---|---|---|
| BTC, ETH 15m | "15m / 30m" (default) | EMA 9/21, VWAP (UTC day), SuperTrend 10 x 4, adaptive RSI 8-28, signals PB/RS/ST/AL, 12-21 UTC, stop swing(5) clamped to 1-4 ATR, targets 2/3/4/6 R, break-even at TP1, SuperTrend trail after TP1, skip if fees > 0.25 R |
| BTC, ETH 30m | "15m / 30m" | same |
| BTC, ETH 5m | "3m / 5m" | EMA 34/89, SuperTrend 20 x 5, 13-17 UTC, previous closed 4h SuperTrend(10, 3) must agree, stop 2.5-4 ATR, targets 2/4/6/10 R, no break-even, skip if fees > 0.10 R |
Strict mode, partials, time stop, flip exit, risk limits and optional filters stay off (script defaults). The fee filter keeps the
script's own 0.08% input in every cost scenario (it is a rule of the strategy, not the cost). 3m is not run (no 1m data here).

Sizing as in the script: 1% of equity at risk per trade, position capped at 10x equity; compounding; 10,000 USDT start.
Funding: 0.01% per 8h on the position value at 00/08/16 UTC, longs pay, shorts receive (the script's assumption).

## Data
Binance spot 5m BTCUSDT / ETHUSDT 2020-01-01..2026-11-01 (the store used for every test), resampled to 15m / 30m / 4h buckets from the
5m bars present. Spot stands in for the perpetual the script was written for; spot and perpetual prices track closely but are not equal.

## Fills (two modes)
* **Gate mode: next-bar open.** A signal on a bar close is filled at the next bar's open, stop and targets then set from that fill with the
  script's stop distance. This is what an alert-driven bot can actually get.
* **Reference mode: signal-bar close** (`process_orders_on_close=true`, as the script's Strategy Tester does). Reported only to compare with
  the script's own claims, never used for the verdict.
Stops: an intrabar touch fills at the stop, a gap through it at the open; a bar touching stop and TP4 counts the stop (as the script).

## Costs per side (fee + slippage)
0.04% + 0 (the script's own setting); **0.05% + 0.01% (Binance futures taker, the gate cost)**; 0.10% + 0.02% (the owner's 0.1% rule).
2x stress doubles fee and slippage.

## Gate and what "passes" means
The engine's five checks as in the earlier futures tests: >= 100 trades; positive at 2x costs; max drawdown <= 15%; the last quarter of
trades' average P&L >= 50% of the earlier trades' and positive; >= 2 of 4 equal periods profitable.
**"Passes" = the full gate passes in gate mode at the taker cost**, judged per chart (6 charts). No retuning after the results.

Also reported (not part of the verdict):
* Split by date: before 2023-11-01 (data the script's author says was never used: the 12-month tuning and the 3-year test are both later)
  vs 2023-11-01..2026-11-01. This is the most useful number: real out-of-sample for this script.
* Sum of R in reference mode at 0.04% for 2025-11-01..2026-11-01 on the four 15m/30m charts, to compare with the script's "+197R" claim
  (a sanity check of the port; data differences spot vs perpetual are expected).

## Known issues in the script found while reading it (before any run)
* Every default was chosen on the same 12 months it reports (session hours, SuperTrend 4.0, break-even, the 3m/5m preset), and the 3m/5m
  preset was fitted after the defaults lost -127 R there. Its own notes say SOL and BNB lost and BTC 15m lost in 2025.
* The Strategy Tester fills entries at the signal bar's close and charges 0.04% per side, both kinder than a bot paying taker fees.
* At 1% risk with up to 10x position size, a normal losing streak (its win rate is 30-40%) is a double-digit drawdown.

## Results (added after the runs; nothing above was changed). **All 6 charts FAILED at every cost. Nothing retuned.**
Gate mode (next-bar open), 2020-01..2026-11, 10,000 USDT start, 1% risk compounding:
| Chart | Trades | Win | Net, script 0.04% | **Net, taker (gate)** | Owner 0.10% | Max DD (taker) | Sum R (taker) | Before 2023-11 R | From 2023-11 R |
|---|---|---|---|---|---|---|---|---|---|
| BTC 15m | 2,711 | 26% | -8,143 | **-9,595** | -9,995 | 97.3% | -272 | -174 | -98 |
| ETH 15m | 2,911 | 26% | -1,420 | **-7,764** | -9,963 | 85.0% | -94 | -46 | -48 |
| BTC 30m | 1,502 | 27% | +742 | **-3,403** | -8,932 | 71.7% | -12 | -56 | +44 |
| ETH 30m | 1,708 | 24% | -5,498 | **-7,509** | -9,524 | 80.7% | -107 | -42 | -65 |
| BTC 5m (3m/5m preset) | 801 | 32% | -2,691 | **-4,481** | -7,944 | 59.7% | -46 | -58 | +13 |
| ETH 5m (3m/5m preset) | 1,148 | 31% | -1,302 | **-3,964** | -8,208 | 66.6% | -27 | -49 | +22 |
Every chart fails at least "positive at 2x costs" and "drawdown within limit"; the 15m charts and ETH 30m fail 4 of 5.
On the data the author never used (before 2023-11) all six charts lose. Even at the script's own cost and fill (reference mode) the
full-period result is negative on BTC 15m, ETH 15m, ETH 30m, BTC 5m, ETH 5m, and BTC 30m makes +688 with a 62% drawdown.
Most of the BTC 15m loss is on shorts (-9,387 of -9,595) in the 2020-2021 bull run.

**Port check.** Reference mode, 0.04%, last 12 months, four 15m/30m charts: 1,167 trades and +66.6 R, vs the script's claim of 1,095
trades and +197 R. Trade counts agree within 7%; the R does not. Not resolved here (no TradingView): spot vs perpetual candles and any
small port difference are the candidates. Comparing the Strategy Tester's trade list for one chart would settle it. Even at the claimed
figure, the last 12 months are the period the defaults were picked on, and the losses above come mainly from earlier years.
