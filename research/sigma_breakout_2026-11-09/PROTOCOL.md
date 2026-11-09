# Owner-supplied Pine strategy "Sigma Breakout" — test protocol (pre-registered 2026-11-09, before any run)

Owner request: test it on ETHUSDT 15m; do not add it to the project yet. It is re-implemented bar by bar in `harness.py` here
(research only, not registered in the engine). `sigma_breakout.pine` is the owner's script, unchanged.

## What is tested
The script as written, all inputs at their defaults: N sigma days 14, band multiplier 1.0, reference = RTH open (VWAP off),
stop loss off, flatten 16:00 ET, no cap on entries per day.

Behaviours reproduced on purpose:
* "RTH" is 09:30-16:00 New York time, a US stock session. ETH trades 24/7, so the script trades only that window, on every calendar
  day including weekends (each NY date is a "day"). The day's reference is the open of the first bar at or after 09:30.
* Sigma for a slot (bar open time, HHMM NY) = mean of |close - day open| at that slot over the last 14 days that had it, excluding
  today. A slot trades only once it has exactly 14 values and sigma > 0. Today's value is stored after the bar is used.
* Long when a bar closes above open + sigma, short when it closes below open - sigma; the opposite break flips. No new entry on a bar
  ending at or after 16:00; any position is closed at the close of the bar ending at 16:00 (the 15:45 bar).
* All orders fill at the signal bar's close (process_orders_on_close = true).

## Data
Binance spot archive 5m ETHUSDT, 2020-01-01..2026-11-01, resampled to 15m, same store as the earlier tests. Maintenance holes are left as
they are. Shorts use spot prices as a stand-in for a perpetual: no funding, no liquidation (1x).

## Costs (owner rule: 0.1% fee per side always)
Base: 0.10% fee per side + 0.02% slippage per fill against us. Stress: 2x. Zero-cost run reported as a diagnostic only.
(The script itself sets no commission; that is not used.)

## Readings (4 runs, declared now)
| Reading | Sizing | Sides |
|---|---|---|
| A1 | as written: 1 ETH per trade, capital 100,000 | long + short |
| A2 | as written: 1 ETH per trade, capital 100,000 | long only (short signals only close a long) |
| B1 | engine-comparable: 10% of equity per trade, capital 10,000 | long + short |
| B2 | engine-comparable: 10% of equity per trade, capital 10,000 | long only |

## Gate (same criteria as the engine's promotion gate)
At least 100 closed trades; net P&L positive at 2x costs; max drawdown <= 15%; out-of-sample (last 25% of trades by time) expectancy
>= 50% of in-sample and positive; at least 2 of 4 equal time periods profitable.

It "qualifies" for the spot system only if B2 passes. Long + short (A1/B1) would need the separate futures stage. Results are reported
whatever they are; a fail is not followed by tuning on this data. Even a pass is not added without the owner's decision.

## Honest limits
Re-implementation checked against hand-built bars (`test_harness.py`), not against TradingView's Strategy Tester. About 84 variants have
now been tried on this ETH data, so a single pass would be optimistic.
