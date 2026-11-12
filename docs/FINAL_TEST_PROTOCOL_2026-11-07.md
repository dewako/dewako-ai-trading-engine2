# Final-test protocol, written BEFORE the final test was run (2026-11-07)

Goal asked by the owner: retest all strategies, find the weaknesses, fix them, do ETHUSDT too. To keep "fix until profitable" from becoming curve fitting:

- Dev window: BTCUSDT 2024-03-01..2025-04-01. Validation window: BTCUSDT 2025-04-01..2025-11-01. (No strategy had been run on these before this session.)
- BTCUSDT 2025-11-01..2026-10-31 had already been seen for the six original versions; it is used only for versions that never ran on it (Trend AI 2.3).
- ETHUSDT 2024-03..2026-10 (never run before) is the final test, opened once, for finalists only.
- Finalist rule (fixed before opening the final test): net P&L > 0 AND still > 0 at 2x costs in BOTH the dev and the validation window.
- Final pass rule: net > 0 and > 0 at 2x costs on ETHUSDT 4h (32 months) AND on BTCUSDT 4h 2025-11..2026-10.
- Variants tried before the final test (the multiple-testing count): 9 strategy versions x 1h (dev, validation) + 4 versions x 4h (dev, validation) = 13 strategy/timeframe combinations, plus one cost-model change.
- Changes made were structural, not parameter searches: (1) three new versions that add the opposite-of-entry exit the originals lacked; (2) a slower timeframe (4h) because the per-trade gross edge at 1h was smaller than the round-trip cost; (3) the simulator's range-based spread/slippage scaled to a 5-minute reference (was growing with bar length; it charged about 0.45% round trip at 1h versus about 0.22% for fee + a realistic spread). The fee (0.1% per side) was not lowered. The 100-trade threshold was not lowered.
- Spike candles: owner said "do ETHUSDT also"; the 4h candles flagged by the validator are exact aggregates of 5m candles that passed the 5m validator (ETH: 4 reviewed spikes, all real events with continuous prices and 10-27x volume), so they are acknowledged and listed in the run commands.
