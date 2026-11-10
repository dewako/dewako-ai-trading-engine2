# ATR + VWAP futures scalper, long + short, 5m — design and test protocol (pre-registered 2026-11-10, before any run)

Owner request: build a scalping strategy for futures, long and short, on 5m and 3m, using two indicators: ATR and VWAP. Research only;
not added to the engine (futures is a separate stage; the engine cannot replay shorts). `harness.py` runs it bar by bar.

**3m is not tested:** 3m candles need 1m data; the store here has 5m only (a 5m bar cannot be split). Reported as not run.

## Indicators (fixed, textbook defaults; no tuning)
* VWAP: session VWAP of typical price (h+l+c)/3 x volume, reset at 00:00 UTC each day (TradingView's crypto default session).
* ATR: 14-bar Wilder ATR (TradingView `ta.atr(14)`).

## Two variants, both declared now (the two standard ways to trade VWAP with an ATR band)
| | V1 — reversion to VWAP | V2 — breakout from VWAP |
|---|---|---|
| Long signal (on a bar close, flat) | close < VWAP - 2 x ATR | close > VWAP + 1 x ATR, and the previous close was not |
| Short signal | close > VWAP + 2 x ATR | close < VWAP - 1 x ATR, and the previous close was not |
| Entry | market at the next bar's open | market at the next bar's open |
| Stop | 1.5 x ATR from entry | 1 x ATR from entry |
| Target | close back at or beyond VWAP -> exit at the next open | 2 x ATR from entry (resting order) |
| Time stop | 24 bars (2 h): exit at the next open | 24 bars (2 h) |
One position at a time. A stop the bar opens through fills at the open; a bar touching stop and target counts the stop (worst case).
Sizing: 10% of equity per trade, 1x, 10,000 USDT (leverage scales P&L and drawdown alike; per-trade edge in % is reported).

## Data, costs, gate
Binance spot 5m BTCUSDT and ETHUSDT, 2020-01-01..2026-11-01, standing in for the perpetual (futures archive unreachable). No funding in
P&L; funding times crossed are counted. Cost per side = fee + slippage: 0.10% + 0.02% (owner rule), **0.05% + 0.01% (futures taker,
the gate cost: entries are market orders)**, 0.02% + 0.01% (maker, reference only), zero (diagnostic). 2x stress doubles both.
Gate: the engine's five checks. "Works" = the full gate passes at the taker cost. 4 runs (2 variants x 2 coins); no tuning afterwards.
