# "Dewako Flow" 1.0 — design and test protocol (pre-registered 2026-11-10, before any run of it)

Owner request: the strongest spot strategy that can be built from SMA, ATR and VWAP while keeping news, market manipulation and order
flow in mind. It lives in `dewako_flow_plugin.py` (loaded with `--plugin`), is not registered in the engine, and is added only on the
owner's decision after a pass.

## What the data can and cannot see (stated before the design)
The store has price and volume candles only. There is **no news feed, no order book and no buy/sell (taker) volume**. So the three
concerns are handled by what a candle can show, not by the real thing:
| Concern | What is actually used (a proxy) |
|---|---|
| News | A shock bar: true range above 3 x ATR(14). After one, no new entry for 6 bars (24 h). News itself is never read. |
| Manipulation (stop hunts, spike-and-reverse) | Decisions only on bar closes, never on an intrabar touch; no entry on a signal bar whose upper wick is more than half its range; stop 2 x ATR away, outside ordinary noise. |
| Order flow | Price relative to the rolling VWAP with above-average volume: a close back above VWAP on higher volume is read as buyers in control. This is not order-book flow. |

## Rules (spot, long only, 4h — the timeframe where the earlier tests showed costs do not eat the edge)
* Regime (SMA): close above its 200-day simple average (1,200 closes).
* Entry (VWAP + volume): close above the 30-bar (5-day) rolling VWAP, the previous close at or below its own 30-bar VWAP, and this
  bar's volume above its 20-bar average.
* Filters (ATR): none of the last 6 bars is a shock bar (true range > 3 x the ATR(14) of the 14 bars before it); the signal bar's upper
  wick is at most half its high-low range.
* Stop (ATR): entry - 2 x ATR(14).
* Exit: close below the 30-bar VWAP by more than 1 x ATR(14), or close below the 200-day average.
* Sizing and risk limits: the engine's own (0.5% risk, 10% position cap). Every number above is fixed here; none is tuned afterwards.

## Test
Engine replay, BTCUSDT and ETHUSDT 4h (5m resampled), 2020-01-01..2026-11-01, `--lookback 1210`, fee 0.1% per side, the full promotion
gate, `--variants-tried 104` (everything tried on this data so far, counted generously). One run per coin.

## What "qualifies" means, and the honest limits
The full gate passes for that coin; a pass is examined (top-trade dependence, hold-out count) and reported before anything is added.
"Most powerful" cannot be promised: this is one design, its lessons come from this same data, and it is the ~104th variant tried on it,
so even a pass is optimistic until it holds on unseen coins or forward paper trading. A fail is reported as it is, with no 1.1 tuned here.

## Results (added after the runs; nothing above was changed). **Both coins FAILED the gate. Not added.**
| Coin | Trades | Net | 2x costs | Max DD | OOS vs IS expectancy | Periods | Gate |
|---|---|---|---|---|---|---|---|
| BTC | 109 | +704 | +395 | 5.8% | -2.51 vs 8.79 | 2 of 4 | **FAILED** (out-of-sample) |
| ETH | 109 | +2,025 | +1,671 | 4.3% | 9.25 vs 20.64 (45%; needs 50%) | 3 of 4 | **FAILED** (out-of-sample) |
Each fails exactly one check, the out-of-sample one: the most recent quarter of trades earns much less than the earlier ones (BTC: loses).
ETH misses the 50% line by a small margin; a near miss is a fail, and the thresholds are not moved. For comparison Dewako ETH 1.0:
138 trades, +1,422, 2x +509, DD 8.2%, passed. Per the protocol, no Dewako Flow 1.1 is tuned on this data.
