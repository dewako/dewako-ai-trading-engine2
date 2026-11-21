# Pre-registered test: long-only daily trend, 2017-2019 (unseen period)

Written and committed on 2026-11-21 BEFORE `run.py` was run on this data. Results are appended below the line at the end, never edited into this text.
Research only: no orders, no engine change, nothing here promotes a strategy.

## Why this test exists
F2 (long+short 55-day breakout, 3 ATR trailing stop) failed 5/6 on BTC/ETH/SOL and 4/6 on five unseen coins (drawdown 17.49%, out-of-sample edge 34% of in-sample). Shorts showed no edge. "Long-only with drawdown-aware sizing" was a hypothesis formed AFTER seeing those results, so 2020-2026 cannot confirm it. The 2017-2019 Binance data was not looked at by any earlier test and is the only unseen period available.

## Rule (one specification, no tuning)
- Signal and exit: exactly F2 (`harness.py`): close above the previous 55 daily highs, entry at the next daily open, 3 x ATR(20, Wilder) trailing stop. Daily bars are aggregated from 5m candles by UTC day.
- Long only. 1x leverage, isolated, one shared 10,000 USDT paper futures account for all coins.
- Sizing: each entry risks `1% x max(0, 1 - drawdown / 0.15)` of equity, drawdown = (peak closing equity - equity at entry) / peak. At 15% drawdown no new entry is taken. The only change to the harness is the optional `dd_limit` argument (default None = F2 unchanged; verified: F2 `results.json` is reproduced identically, and 4 new tests cover it).
- Costs: fee 0.10% per side + 0.05% slippage (verdict); 2x stress 0.20% + 0.10%. Long funding 0.03% per day, as in F2 (spot prices stand in for perpetuals).

## Data
- Verdict period: Binance spot 5m archive, 2017-08-17 to 2019-12-31, BTC, ETH, BNB, XRP, ADA, LINK. Each coin joins when its data starts (BNB 2017-11, ADA 2018-04, XRP 2018-05, LINK 2019-02) and needs 55 days of history before its first signal.
- Archive repair (`fix_rows.py`, reports in `results/dropped_*.json`): rows whose close time is not open + 5m - 1ms, or whose open time is off the 5-minute grid, are dropped and recorded (BTC 249, ETH 249, BNB 248, XRP 2, ADA 2, LINK 1). Nothing else is altered. Days with missing candles stay in (partial days); days with under 90% of 288 candles are counted and reported.
- Reference period (already seen, used only for the in-sample average R): the same six coins, 2020-01-01 to 2026-10-31, same rule.

## Verdict checks (all must pass)
1. At least 20 closed trades. (The 100-trade promotion gate cannot be met on this data; a pass here is not a promotion.)
2. Net positive at base costs and at 2x costs.
3. Max drawdown of the closing-equity curve <= 15%.
4. Average R per trade positive and >= 50% of the reference-period average R.
5. At least 2 of 4 equal date slices profitable (a flat slice is not profitable).
6. Max drawdown below equal-weight buy-and-hold over the same period.

## Stop rule
Any failure: the daily-trend family on spot crypto is dropped as a candidate and not re-tested with other parameters on this data. A pass means only: write a separate pre-registered pooled test aiming at the 100-trade gate, and forward paper testing. Not promotion.

## Reported, not part of the verdict
Each coin alone; by calendar year; result without the best 1 and 3 trades; unscaled long-only; the original long+short rule; neighbouring settings (40/55/70 days, 2.5/3/3.5 ATR).

## Known limits
Survivorship bias (the 2017-2019 universe is coins that survived and were later chosen); spot standing in for perpetuals with constant funding; only 2.4 years, with the number of coins growing over time; 2017-2018 slippage may be understated; partial days where candles are missing; a single bull-bust-recovery cycle.

---
## Results (appended 2026-11-21 after the single run; protocol text above unchanged)
Raw numbers: `results/results.json`. One bug fix between the first attempt and the run (duplicate dict key in the data report; the first attempt crashed before computing any strategy result; no rule or check changed).

**Verdict: PASS, 6/6.** 29 closed trades; net +1,637.76 (+16.4%) at base costs, +1,578.44 at 2x; max drawdown 5.92%; average R 0.635 vs 0.608 in the 2020-2026 reference (147 trades); slices +430.61 / -376.46 / +1,682.43 / -98.82 (2 of 4 profitable); buy-and-hold drawdown 77.62%.

What the pass does and does not mean (stress test of the result):
- The drawdown-aware sizing never acted: peak drawdown was 5.9%, far below 15%, so `skipped_dd` = 0 and the scale was only a small reduction (unscaled long-only: +1,943.64, drawdown 6.95%, same 29 trades). That part of the hypothesis was not tested by this data.
- Thin: 29 trades; net without the best 3 trades is +154.62 (about zero); 2019 supplies 17 of 29 trades and +1,583.61 of the net; 2018 was negative (-320.47); XRP lost on all 3 trades.
- Check 6 is weak for a strategy that is mostly in cash: it earned +16.4% in a period where equal-weight buy-and-hold made +172.8% (with a 77.6% drawdown). The rule is a low-risk, low-return filter, not a profit engine.
- Direction is consistent with the 2020-2026 reference (avg R 0.635 vs 0.608; neighbouring settings all positive, 20-46 trades), which is the useful part: the edge did not vanish on unseen data.
- The long+short rule on the same data: +3,465.73, drawdown 6.79%, avg R 0.557 (53 trades, 4 still open): shorts did not hurt here, which differs from 2020-2026 and shows how regime-dependent this is.
- Known limits stand: survivorship, spot standing in for perpetuals, a single cycle, 11 BTC days with under 90% of candles.

Per the stop rule this is not a promotion and the 100-trade gate is still unmet. Next step under the protocol: a separate pre-registered pooled test aimed at 100 trades, plus forward paper testing.
