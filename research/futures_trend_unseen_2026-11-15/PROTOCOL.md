# F2 confirmation: the daily trend rule on five coins it has never seen (pre-registered 2026-11-15, before any run)

Owner decision 2026-11-15: "leave short time frames". The only candidate left is the daily trend rule from
`../futures_trend_f2_2026-11-15/` (55-day breakout, 3 ATR(20) trailing stop, long + short, 1x, risk 1%), which failed its own test on
check 4 (out-of-sample edge 37% of in-sample) while passing the other five. This run asks one question: does the same rule, unchanged,
work on coins that were never looked at while it was being chosen? Committed before `run.py` is executed for the first time.

## What is run
`harness.run` from the F2 folder, imported unchanged (same code, same settings, same account model). Nothing is tuned.
* **Coins:** BNB, XRP, ADA, LINK (2020-01-01..2026-10-31) and AVAX (from 2020-09-22, its Binance listing). Five coins, one shared
  10,000 USDT account, ISOLATED, 1x, risk 1% per trade.
* **Data:** Binance spot 5m monthly archives supplied by the owner, resampled to UTC days. Rows whose close time is not open + 5m - 1
  (exchange-outage artefacts, 6 to 8 per coin) were dropped before import, as for BTC/ETH/SOL; `results/dropped_*.json` lists them.
* **Costs:** the F2 verdict cost (fee 0.10% + slippage 0.05%; longs pay 0.01% funding per 8h, shorts receive nothing) and the 2x stress.

## Verdict: the same six checks as F2, on the five new coins together at 1x
1. at least 100 trades; 2. positive at 2x costs; 3. max drawdown at most 15%; 4. out of sample (entries from 2024-01-01) average R
positive and at least 50% of the earlier average; 5. at least 2 of 4 equal periods profitable; 6. drawdown below buy-and-hold.
"Confirmed" = all six pass here. Because these coins were never used to pick the rule, a pass here means more than the BTC/ETH/SOL
result; a fail means the F2 result does not generalise and the rule is dropped.

## Also reported (not part of the verdict)
Each coin alone; long/short; each year; without the best 1/3/5 trades; the eight coins together (BTC, ETH, SOL + the five) as one
account, which is what a real portfolio would be; the F2 neighbours (40/55/70 days x 2.5/3/3.5 ATR) on the five new coins.

## Limits known before the run
Spot candles stand in for perpetuals; funding is a constant. The five coins and the earlier three all rose enormously over 2020-2021
and move together, so eight coins are still far from eight independent tests. Shorts face a rising market overall.

## Results (added after the run; nothing above was changed)
