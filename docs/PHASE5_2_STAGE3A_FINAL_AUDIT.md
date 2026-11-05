# Phase 5.2 Stage 3A: Final Audit (backtest foundation)

Scope: paper / research only. Nothing in 3A can reach an exchange, hold credentials or enable live trading (still LOCKED).
Verdict: **STAGE 3A PASSED (research foundation only).** It is not evidence that any strategy is profitable; 3B has not started.

## What was built
| Step | Component | Tests |
|---|---|---|
| 3A-1 | Dataset layer: validation, point-in-time build, SHA-256 content hash, refusal of bad data | 22 |
| 3A-2 | Simulated exchange + virtual clock: spread, slippage, impact, fees, partial fills, latency, rules, faults | 29 |
| 3A-3 | Replay through the real Strategy → Risk → Execution → ExitMonitor, injectable time, pessimistic intrabar path | 21 (+23 subtests) |

## Evidence
- SQLite 660 passed / 39 skipped; PostgreSQL 16 663 passed / 36 skipped (every skip is a documented PostgreSQL-only or HTTP-in-hosted-CI case; `assert_no_skips.py` enforces it).
- Hosted CI run 42010671559 on `baa120f`: attempts 1, 2 and 3 all green on all 9 jobs (static, sqlite 3.12/3.13, postgres16, crash-repeat, windows-crash 3.13/3.14, e2e-real-app, docker).
- Mutation checks: 22 (dataset hash/validation, earlier), 22 (simulator), 24 (replay/time source). Survivors were fixed by adding tests, or shown to be equivalent mutants (fee term in the digest; a redundant `max()` on the clock, since removed).
- Determinism: the same dataset and config gave the identical digest in three separate processes with different `PYTHONHASHSEED` (0, 1, 12345), using the full cost model.

## Independent findings along the way (all fixed or documented)
1. Persistence timestamps ignored the injected clock → `core/timesource.py`; every stored timestamp is simulated in a replay (test scans all tables).
2. The runtime refuses to trade without quote-guard limits; the replay now states its own and records them.
3. An unknown strategy produced a silent all-ERROR run that looked like "no trades" → now raises.
4. Unknown exit reason was mislabelled STRATEGY → now UNKNOWN.
5. A 10.5 % in-bar range is refused by the dataset validator (spike rule), which correctly protected a test from bad data.

## Known limits (carried to 3B, not hidden)
- Paper evidence only; the cost model is a conservative assumption, not calibrated to a real exchange.
- Costs use the whole bar's range and volume (known only after the bar closes): an approximation.
- Spot, long-only, market orders, one position per bot; no funding, borrow, order-book depth.
- Throughput ≈ 55 ms per strategy tick: a year of 5m bars is about 1.5 h per strategy. 3B must measure and speed this up.
- Strategy parameters cannot be overridden yet (needed for walk-forward/optimisation).
- Real exchange download still untested (no network here); dataset gap explanations and spike acknowledgements are human-supplied.
- Windows 3.14 hang (run 41311105233) remains unexplained; it has not recurred.
- Production code was touched in five files for the time source. The change is behaviour-neutral by default and covered by the full existing suite plus mutation checks, but a reviewer should read that diff.

## Gate to 3B
Open owner decisions: promotion thresholds, whether the gate also applies to strategies already PAPER_ELIGIBLE, history period (suggested ≥12 months, last 3 months held out), XAUUSD data source. 3B must not enable live trading or add testnet/live adapters.
