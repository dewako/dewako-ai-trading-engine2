# Real-data strategy review: weaknesses, fixes, final test (2026-11-07)

Protocol and variant count: `docs/FINAL_TEST_PROTOCOL_2026-11-07.md` (written before the final test). Data: Binance spot BTCUSDT / ETHUSDT 5m archives
(owner-supplied, checksums NOT available), resampled to 1h/4h. Costs: fee 0.1% per side, spread/slippage/impact model (range-scaled to a 5m reference
after the fix below). Long side only (spot). Results are simulations, not evidence of live profit.

## Weaknesses found and what was done
| Weakness | Evidence | Action |
|---|---|---|
| Replay fed strategies a bare market state (volume ratio always 1.0) | 4 of 6 strategies made 0 trades in a year | Replay uses the live state builder; gate refuses runs on the simplified state |
| Risk engine's 4-consecutive-loss latch ended runs after weeks | six runs, 4-5 trades each | Replay policy `streak_reset` (never / daily) recorded in the run; warning when the latch rejects >10% of ticks |
| Trend AI 2.1/2.2, Momentum AI 1.4, AI Strategy 0.3 have only an ATR stop (no exit) | every trade ended at its stop; 1 closed trade in 12 months on the 2024-25 bull run | New versions Trend AI 2.3, Momentum AI 1.5, AI Strategy 0.4: one opposite-of-entry exit each, entries unchanged |
| Round-trip cost larger than the per-trade edge at 1h | Momentum AI 1.5: +140 at zero costs, -283 at modelled costs over 6 months | Slower timeframe (4h) tested; fee not lowered |
| Simulated spread/slippage grew with bar length | ~0.45% round trip at 1h vs ~0.22% (fee + realistic spread) | Range-based costs scaled to a 5-minute reference (5m results unchanged) |
| A version built on another strategy kept its fingerprint if the parent changed | found while writing the exits | Fingerprint now includes parent strategy sources (existing fingerprints unchanged) |

## Results (net P&L on 10,000 start, trades in brackets; "2x" = still positive at doubled costs)
1h, dev 2024-03..2025-03 / validation 2025-04..2025-10: every version loses in both windows except Breakout AI 1.8 (dev +95, 58 trades, fails 2x; validation -95).
4h: Trend AI 2.3 dev +96 (15, 2x ok), validation +174 (7, 2x ok) = the only finalist. AI Strategy 0.4 dev +42 (fails 2x), Mean Reversion 1.2 +5 / -59, Trend AI 2.2 -219.

## Final test (opened once, Trend AI 2.3 at 4h)
| Window | Net | 2x costs | Trades | Verdict |
|---|---|---|---|---|
| ETHUSDT 2024-03..2026-10 | +318 | +212 | 37 | FAILED the gate: trades < 100, out-of-sample expectancy -13.5 vs in-sample +17.0 |
| BTCUSDT 2025-11..2026-10 (bear) | +29 | -7.9 | 14 | FAILED: 2x costs negative; one trade is over half of the gross profit |

Pre-registered pass rule (positive at 2x costs on BOTH): NOT met. No strategy version is profitable robustly after fees, spread and slippage on this data.
Trade counts (14-37) are too small for the numbers to be more than suggestive; the ETH result is encouraging but its out-of-sample part is negative.

## Not done / limits
Both final-test windows are now used: further tuning on them would be curve fitting. No parameter search was run. Spot long-only; futures not implemented.
Fee tier assumed 0.1% (lower tiers would help; not assumed). Archive checksums unavailable. 100-trade gate threshold unchanged.
