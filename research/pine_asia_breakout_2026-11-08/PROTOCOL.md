# Owner-supplied Pine strategy "BTC Breakout — Asia Range + ADX + Breakeven" — test protocol (pre-registered 2026-11-08)

Written before any run. The owner asked: try it, and add it to the system only if it qualifies.
It is tested in a standalone research harness (`harness.py` here). It is NOT registered in the engine.

## What is tested
The Pine script exactly as written, all inputs at their defaults (EMA 50/200, ATR 14, stop 1.5 ATR, target 2.2 ATR, buffer 0.10 ATR,
ADX 14 > 20 using the script's own SMA-based ADX, breakeven activate 1.0 ATR / buffer 0.3 ATR). No parameter is changed or tuned.

Pine behaviours reproduced on purpose, even where they look unintended:
* Asia range resets at New-York midnight (`newDay`), so the range actually used is 00:00-03:00 NY, not 20:00-03:00.
* Entry is a stop order at range +/- buffer placed when the close is already beyond it, so it normally fills at the next bar's open.
  Pending entries are cancelled when outside 03:00-16:00 NY, not flat, or ADX <= 20; the "armed" flag is not cleared by a cancel.
* At most one long and one short per NY day. Sizing 10% of equity, no leverage, no pyramiding.
* Exits (stop and target) are recomputed every bar from the current ATR and are active from the bar after the entry fill.
  The breakeven move is not latched: it applies only on bars whose close is above (below) the activation level.
* Fills: stop/limit at the order price, or at the open if the bar gaps through it. If one bar touches both stop and target, TradingView's
  bar path is used (open -> nearer extreme -> other extreme -> close).

## Data
Binance spot archive 5m, BTCUSDT and ETHUSDT, 2020-01-01 to 2026-11-01, resampled to 15m (primary) and also run on 5m.
Both timeframes are declared now: 2 variants tried per coin. Exchange-maintenance holes are left as they are.
Shorts are simulated on spot prices as a stand-in for a perpetual: no funding, no liquidation (10% of equity, 1x, cannot liquidate).

## Costs (owner rule: 0.1% fee per side always)
* Base: fee 0.10% per side + slippage 0.02% per fill against us. (The script's own setting is Bybit 0.055% + 2 ticks; reported for reference only.)
* Stress: 2x (0.20% + 0.04%).

## Gate (same criteria as the engine's promotion gate)
At least 100 closed trades; net P&L positive at 2x costs; max drawdown <= 15%; out-of-sample (last 25% of trades by time) expectancy
>= 50% of in-sample and positive; at least 50% of 4 equal time periods profitable.

## Readings and what "qualifies" means
1. Long + short, as written (needs the futures stage; cannot go into the spot system today).
2. Long side only (same script, shorts never placed): this is the only form the spot-only system could run.
The strategy qualifies for adding to the system only if reading 2 passes the gate on BTC on 15m. ETH and 5m are reported.
Results are reported whatever they are. A fail is not followed by tuning on this data.
