# Dewako Flow tuning — protocol (pre-registered 2026-11-10, before any tuning run)

Owner request after Dewako Flow 1.0 failed the gate on the out-of-sample check: "fine tune it if there is room for improvement".
The 1.0 protocol said no 1.1 would be tuned on this data; the owner has overridden that, so the tuning is done the only way that keeps
the answer believable: the same split and selection rules as docs/TUNING_PROTOCOL_2026-11-08.md.

## Split
* Training: 2020-01-01 to 2024-01-01. All selection happens here.
* Test: 2024-01-01 to 2026-11-01. Not used to choose anything.
Caveat: the 1.0 full-period results (including its weak hold-out) were seen before this grid was written. No trade of 1.0 was
inspected; the grid below is the plain neighbourhood of the three numeric choices that set how long a trade is held.

## Grid (fixed, 12 variants, versions 1.0.1 - 1.0.12; 1.0.x with the 1.0 settings is the baseline)
vwap_bars 18 / 30 / 60 (3 / 5 / 10 days) x exit_atr 1.0 / 2.0 x atr_stop 2.0 / 3.0.
Regime (1,200 bars), volume window (20), shock rule (3 x ATR, 6 bars) and wick limit (0.5) are not touched. No other variant is tried.

## Selection (training only, mechanical: `pipeline.py`)
One screening replay per variant and coin, fee 0.2% per side (twice the real fee). The same settings must serve both coins.
Score = the lower of the BTC and ETH net P&L; a variant needs at least 40 closed trades on each coin.
The best score wins. **If the winner is the baseline, or does not beat the baseline's score, the answer is "no room in this grid"** and
nothing further is run.

## Test (only for a winner that beats the baseline)
One screening run per coin on 2024-01-01..2026-11-01 at 0.1%, then the full promotion gate over 2020-2026 at 0.1% with
`--variants-tried 116` (104 + these 12). Pass = the gate passes. Reported whatever it is; no second grid on this data after it.
A pass is examined and reported before anything is added; adding stays the owner's decision.

## Results (added after the run; nothing above was changed)
**Selection (training 2020-2023, fee 0.2%).** Winner 1.0.6 = VWAP 30, exit 1.0 ATR, **stop 3.0 ATR** (the only change from 1.0: stop 2 -> 3 ATR).
Score 627 vs baseline 1.0.5's 450. All four VWAP-60 variants scored higher on raw net but had under 40 BTC trades and were ineligible, as fixed above.
Full table: `results/selection.json`.

**Test screens 2024-01-01..2026-11-01, fee 0.1% (period never used for selection; screens are not evidence):**
BTC 33 trades, +62 (one trade is over half the gross profit). ETH 28 trades, +326 (under 30 trades: mostly noise).

**Full gate 2020-01-01..2026-11-01, fee 0.1%, `--variants-tried 116`:**
| Coin | Trades | Net | 2x costs | Max DD | OOS vs IS expectancy | Periods | Gate |
|---|---|---|---|---|---|---|---|
| BTC | 105 | +945 | +660 | 5.1% | -3.73 vs 12.42 | 2 of 4 | **FAILED** (out-of-sample) |
| ETH | 102 | +1,896 | +1,611 | 3.8% | 12.08 vs 19.86 (61%) | 4 of 4 | **PASSED** |

**Examination of the ETH pass (`examine.py`, one extra replay per coin to get the trade list; no rule changed):**
* Largest trades: ETH net is +1,392 without its best trade, +559 without the best 3, +38 without the best 5. BTC: +383, -316, -752.
* By exit year, ETH: 2020 +243, 2021 +721, 2022 no trades, 2023 -5, 2024 +640, 2025 +476, 2026 -179.
* Thin margins: 102 trades against a minimum of 100; ETH net is lower than 1.0's (+2,025); the pass comes from the last quarter of trades
  holding up better (61% vs 45%), not from more profit. 38 of 102 trades win.
* 116 variants have been tried on this data and the baseline missed by 5 points, so a flip to "pass" from one stop change is weak evidence.
* BTC still fails the same check, so this is an ETH-only result, like Dewako ETH 1.0. The two are both long-only ETH trend strategies under
  the same 200-day filter; their overlap has not been measured.

Per this protocol there is no second tuning round. 1.0.6 is not added to the engine; that is the owner's decision.
