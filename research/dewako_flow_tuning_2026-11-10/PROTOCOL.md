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
