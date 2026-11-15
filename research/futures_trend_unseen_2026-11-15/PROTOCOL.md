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

**Verdict: FAIL — 4 of 6 checks passed; checks 3 (drawdown 17.5% > 15%) and 4 (out-of-sample edge 34% of in-sample) failed. Nothing retuned.**

### The five unseen coins together (1x, fee 0.10% + slippage 0.05%)
| Check | Result | |
|---|---|---|
| 1. at least 100 trades | 181 | pass |
| 2. positive at 2x costs | +15,355 | pass |
| 3. max drawdown at most 15% | **17.49%** | **FAIL** |
| 4. out of sample (2024+) avg R at least 50% of in-sample | 0.295 R (77 trades) vs 0.878 R (104) = **34%** | **FAIL** |
| 5. at least 2 of 4 periods profitable | 3 of 4 (+10,183, -2,517, +6,762, +1,796) | pass |
| 6. drawdown below buy-and-hold | 17.49% vs 85.76% | pass |
Net **+16,224** on 10,000 (about 15% a year), win rate 43%, 0 liquidations, 10 entries refused by the guard.

* **Each coin alone:** BNB +2,583, XRP +1,973, ADA +2,233, AVAX +5,690, **LINK -227** (44 trades, 32% wins). Four of five positive.
* **By side:** long 106 trades +16,708; **short 75 trades -437**. The shorts lose on these coins too (F2: +849 on BTC/ETH/SOL).
* **By year:** 2020 +1,809; 2021 +7,945; 2022 -337; 2023 +2,349; 2024 +2,839; 2025 +71; 2026 +1,595. One losing year (2022).
* **Without the best trades:** all +16,271; without the best 1: +12,593; best 3: +6,460; best 5: +3,148. The best five are all longs,
  three of them from the 2021 bull run.
* **Neighbours:** all nine settings positive (+8,428 to +16,224); drawdowns 15.0% to 24.6%, only 70 days / 3.5 ATR under 15%.

### All eight coins as one account (reported, not a verdict)
297 trades, **+28,732**, max drawdown 23.5%, positive at 2x costs (+26,550); 0.746 R before 2024 vs 0.259 R after (35%).
Longs +27,352 on 183 trades; shorts +1,459 on 114. Every coin positive except LINK.

### What this shows
* **The profit generalised; the test still fails.** On coins never looked at, the rule made more per trade (0.63 R vs 0.41 R) and
  the same picture held: positive at double costs, far below buy-and-hold's drawdown, most years positive, no setting-sensitivity.
  This is the first idea tested here whose result carried over to unseen data.
* **Two things did not carry over:** the drawdown limit (17.5% and 23.5% across eight coins, against the 15% rule), and the
  out-of-sample ratio, which came out at 34-37% in every group. An edge that is one third of its 2020-2023 size after 2024 is the
  consistent finding, not noise in one group.
* **Shorts have no edge in this data** (-437 here, +849 in F2, 189 short trades in all, about zero R). Long-only would have scored
  better on every check, but that is a change decided after seeing results, so it is not applied here; it is a hypothesis for a new
  pre-registered test.
* Eight correlated coins over one crypto cycle are still one history, and 2021 supplies most of the profit.

### Harness note
`harness.run` from the F2 folder was used as is. The books were checked: trade P&L sums to the account change in every run.
