# Asia-Range Breakout (owner-supplied Pine script): protocol written BEFORE any run (2026-11-07)

Owner request: backtest this 5-minute BTC strategy on 2 years of data; add it to the project only if it is good.

## What will be tested
The owner's parameters exactly as supplied (EMA 50/200, ATR 14, stop 1.6 ATR, target 1.2 ATR, buffer 0.10 ATR, ADX 14 > 25, breakeven trigger 1.0 ATR to +0.3 ATR). **No parameter is changed or searched.** Variants tried: 1.

## Known differences from the Pine script (the platform is spot, long-only, risk-engine sized)
1. LONG side only. Spot cannot short; the script's short half is not run. Results are therefore NOT the script's results on TradingView.
2. Position size comes from the platform risk engine (2% risk, caps), not 100% of equity.
3. The stop and target are fixed at entry from the entry-bar ATR; the script recomputes them from the current ATR every bar.
4. The breakeven trail is checked on closes and exits at the next bar open; the script moves a real stop intrabar.
5. "One long per day" is derived from the day's candles (first qualifying bar of the New York day), not from a stored flag.
6. Fills: next bar open plus the simulator's spread, slippage and impact; fee 0.1% per side (the owner's figure; the gate refuses less).

## Data and windows
BTCUSDT 5m (owner-supplied Binance spot archive), 2024-11-01 to 2026-11-01 (24 months). The validator's last-25% hold-out (about 6 months) and the 2x / 3x cost stress are part of every run. Cross-check, never tuned: ETHUSDT 5m, same dates.

## Pass criteria (fixed now)
Add to the project only if ALL hold: (a) the official promotion gate returns PASSED on BTCUSDT (>=100 trades, positive at 2x costs, drawdown <= 15%, out-of-sample holds up, consistent across periods); (b) ETHUSDT net P&L > 0 and > 0 at 2x costs.
If the criteria are not met the strategy is NOT added; the result and the reasons are reported.

## Result (run after the protocol was committed; no parameter changed; variants tried: 1)
Strategy: Asia Range Breakout 1.0 (long side only), 5m, 2024-11-01 to 2026-11-01, fee 0.1% per side, daily streak reset. Research module (not in the project).

| Symbol | Verdict | Closed trades | Net P&L (10k start) | Max drawdown | Failed checks |
|---|---|---|---|---|---|
| BTCUSDT | FAILED | 373 | -1000.5 | 10.01% | positive at 2x costs (-1001.1), out-of-sample, periods (0 of 4 profitable) |
| ETHUSDT | FAILED | 347 | -1003.5 | 10.03% | same three |

Criteria (a) and (b) both unmet: **NOT added to the project.**

Caveat that matters: both runs end at almost exactly -10.0%. The risk engine's max_drawdown_pct limit is 10 and it halts permanently once reached, so the figures are the loss up to the halt, not a full 24-month result. The true full-period loss is unknown (it could be larger). The run's halt warning did not fire because that path is not counted by it. That is a reporting gap in the harness, not fixed here.

Likely cause (an estimate, not measured here): the 1.2 ATR target on 5m BTC/ETH is on the order of the 0.2% round-trip fee plus spread, so a winning trade barely covers costs while a loser pays the full 1.6 ATR plus costs. Not verified by decomposing trades.
Not tested: the short side, the script's TradingView sizing, or a futures fee schedule.
