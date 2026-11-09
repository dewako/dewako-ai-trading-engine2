# Owner-supplied Pine strategy "Sigma Breakout" — test protocol (pre-registered 2026-11-09, before any run)

**Status: REJECTED — scrapped by the owner on 2026-11-10 after failing every test (spot and futures, 5m and 15m). Not in the engine. Kept only as a record so it is not re-tested.**

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

## Results (added after the runs; nothing above was changed)
ETHUSDT 15m, 2020-01-01..2026-11-01 (239,463 bars), 0.10% fee + 0.02% slippage per fill. **All 4 pre-declared readings FAILED. Not added.**

| Reading | Trades | Net | Net at 2x | Max DD | Win rate | OOS vs IS expectancy | Period P&L | Gate |
|---|---|---|---|---|---|---|---|---|
| A1 1 ETH, long + short | 2,580 | -6,802 | -20,214 | 8.3% of 100k | 40% | -0.63 vs -3.31 | -2,522 / -1,770 / -2,959 / +448 | FAILED |
| A2 1 ETH, long only | 1,228 | -3,607 | -9,940 | 4.3% of 100k | 42% | -1.45 vs -3.43 | -694 / -1,166 / -1,851 / +104 | FAILED |
| B1 10% equity, long + short | 2,580 | -3,462 | -6,481 | 37.7% | 40% | -0.40 vs -1.65 | -1,710 / -852 / -809 / -90 | FAILED |
| B2 10% equity, long only (the qualifying reading) | 1,228 | -1,115 | -3,385 | 15.8% | 42% | -0.54 vs -1.03 | -102 / -477 / -524 / -12 | FAILED |

B2 passes only "enough trades"; it fails 2x costs, drawdown (15.8% > 15%), out-of-sample and periods (0 of 4).
Mean hold 4.4-4.5 h; exits are 81% at the 16:00 ET flatten, 19% by flip.

Diagnostic (not a gate input): at zero cost the strategy makes money in every reading (B2 +1,933; A1 +6,609). Its edge before costs is
about 0.10-0.16% of the position per trade, against about 0.24% of real cost per round trip. This is a much larger gross edge than the
Asia-range script (0.007-0.02%), but still smaller than the fee.

Reference (not gate inputs), B1 long + short at futures fees, spot prices standing in for the perpetual, no funding:
| Cost per side | Net | Net at 2x | Max DD | Periods | Gate checks failed |
|---|---|---|---|---|---|
| 0.05% + 0.01% (futures taker) | -1,088 | -3,462 | 20.2% | -1,011 / -284 / -302 / +509 | 2x costs, drawdown, periods |
| 0.02% + 0.01% (futures maker) | +405 | -1,088 | 13.6% | -639 / +57 / +44 / +943 | 2x costs |
The maker row assumes every fill at the bar close earns the maker fee, which a breakout-at-the-close entry cannot get (it is a market
order); it is an upper bound. Funding (16:00 UTC falls inside the 09:30-16:00 ET window) is not included.

## Addendum 2026-11-09: futures long + short scalping (owner request; pre-registered before these runs)
Same script, same defaults, long + short, engine-comparable sizing (10% of equity, 1x, 10,000). Spot prices stand in for the perpetual
(the Binance futures archive is unreachable from this workspace). Timeframes: **5m** (new) and 15m (already run above as B1).
Entries and exits are market orders at the bar close, so the realistic futures cost is the **taker** fee.
| Cost case | Fee + slippage per side | Role |
|---|---|---|
| Owner rule | 0.10% + 0.02% | gate |
| Futures taker | 0.05% + 0.01% | gate (the realistic futures cost) |
| Futures maker | 0.02% + 0.01% | reference only: not reachable with market orders |
| Zero | 0 | diagnostic only |
Funding is not in the P&L (no funding data here); the number of 8-hour funding times each trade is open across is reported.
"Works for futures scalping" means the full gate passes at the futures taker cost on 5m or 15m. Even then it is not added: futures is a
separate stage the owner has not opened. No tuning after these runs.

### Addendum results (nothing above was changed). **Fails at every cost on 5m and 15m. Not added.**
| TF | Cost | Trades | Net | Net at 2x | Max DD | Period P&L | Gate checks failed |
|---|---|---|---|---|---|---|---|
| 5m | owner 0.10% | 3,187 | -4,486 | -7,435 | 47.2% | -1,787 / -1,282 / -1,032 / -385 | 2x, DD, OOS, periods |
| 5m | taker 0.05% | 3,187 | -1,916 | -4,486 | 25.7% | -936 / -652 / -513 / +184 | 2x, DD, periods |
| 5m | maker 0.02% (ref) | 3,187 | -212 | -1,916 | 15.0% | -478 / -254 / -127 / +646 | 2x, periods |
| 15m | owner 0.10% | 2,580 | -3,462 | -6,481 | 37.7% | -1,710 / -852 / -809 / -90 | 2x, DD, OOS, periods |
| 15m | taker 0.05% | 2,580 | -1,088 | -3,462 | 20.2% | -1,011 / -284 / -302 / +509 | 2x, DD, periods |
| 15m | maker 0.02% (ref) | 2,580 | +405 | -1,088 | 13.6% | -639 / +57 / +44 / +943 | 2x |
Zero cost: 5m +1,851, 15m +2,147. Mean hold about 4.2-4.5 h (it is an intraday hold, not a scalp); 0.66-0.72 funding times per trade.
5m is worse than 15m: more trades, a smaller move per trade, same cost per trade.
