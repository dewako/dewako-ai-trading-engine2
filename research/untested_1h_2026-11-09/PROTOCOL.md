# Momentum AI and Breakout AI on 1h — first test (pre-registered 2026-11-09, before any run)

Owner asked to keep testing until strategies pass. Re-tuning the already-failed strategies on the same 2020-2026 data is refused (see
TUNING_PROTOCOL_2026-11-08: no second grid on this data). These two have never been tested: their timeframes (5m/15m/1h, 15m/30m/1h) exclude
the 4h used so far.

* Strategies: Momentum AI 1.4, Momentum AI 1.5 (candidate), Breakout AI 1.8. Default parameters only. No tuning.
* Data: Binance spot BTCUSDT, ETHUSDT, 5m resampled to 1h, 2020-01-01..2026-11-01. Maintenance holes up to 3 candles tolerated
  (`--max-gap-intervals 3`, all 47 known 1h holes allowed); flagged 1h crash candles (BTC 32, ETH 72) accepted, as for 4h.
* Engine replay, `--lookback 300`, fee 0.1%, `--streak-reset daily`, full promotion gate, `--variants-tried 82` (76 before + these 6).
* Pass = the gate passes for that version and coin. A pass is then examined as Dewako ETH's was (dependence on the best trades,
  hold-out trade count) and reported to the owner; nothing is added to the engine without that report and the owner's decision.
* A fail is reported as it is and not followed by tuning on this data.

## Results so far (added after the runs; nothing above was changed)
| Strategy | Coin | Trades | Net | Max DD | Gate |
|---|---|---|---|---|---|
| Momentum AI 1.4 | BTC | 4 (0 winners, 1 still open at end) | -180 | 44.2% | **FAILED** 5 of 5 checks |
| Momentum AI 1.4 | ETH | 1 (0 winners) | -22 | 10.2% | **FAILED** 4 of 5 checks |
| Momentum AI 1.5 | BTC / ETH | - | - | - | not finished |
| Breakout AI 1.8 | BTC / ETH | - | - | - | not started |

The runner hit the 2-hour background limit on 2026-11-09 (each 1h gate takes 1-2 hours here). The 4 unfinished runs are not reported as
anything. `results/run.py` resumes them (finished runs are skipped).
