# Stage 3B audit: status PENDING hosted CI (not yet "passed")

Scope: research foundation only. Passing this stage does not make any strategy profitable or live-ready.

## What is claimed
- Metrics, validation, regimes, run records, the promotion gate and the read-only report behave as the tests state, on SQLite (and PostgreSQL 16 for the gate/records).
- The gate cannot be satisfied by API calls or by a stored verdict; it re-evaluates stored metrics against fixed thresholds.

## Known limits (do not read these as passed)
1. Evidence is paper-only simulation. The cost model (fees, spread, slippage, impact) is assumed, not calibrated.
2. Thresholds are owner-approved placeholders (>=100 trades, positive at 2x costs, max drawdown 15%, OOS expectancy >=50% of in-sample and positive, >=50% of equal periods profitable, >=365 days history, >=25% hold-out).
3. "In-sample vs out-of-sample" is a consistency check; strategy parameters cannot be overridden, so there is no walk-forward or optimisation, and no real over-fitting test. The variant ledger only counts what was declared.
4. The CLI creates evidence as `cli:<user>` with ANALYST role: anyone with shell or DB access can create evidence. The gate checks evidence, not who produced it.
5. The TESTNET gate checks backtest evidence only; there is no forward paper-period gate.
6. XAUUSD data (Exness x MetaTrader) is not implemented: it needs a Phase 6 adapter or an offline MT5-CSV import tagged as its own source. Broker CFD data differs from exchange data.
7. Real exchange download is untested; the real FastAPI routes and e2e run for the new routes are verified only by hosted CI (FastAPI is not installed locally; browser tests ran on a stand-in harness).
8. Replay speed: about 12-14 ms per bar (a year of 5m bars is about 105k bars, so tens of minutes). The idle shortcut is covered by digest-equivalence tests except its resting-orders clause: a mutation that ignores resting orders survives all tests because a market order that cannot fill is still applied by the next tick. That clause is defensive and unproven. The resource test only catches a step change (>100 ms/bar or >150 MB), not removal of the shortcut.
9. `core/timesource` touches production paths (db `now_iso`, `utcnow`, safety controller, execution recovery). Reviewing that diff is recommended; behaviour is unchanged unless `virtual_time` is entered, which only the replay does.
10. The Windows 3.14 hang remains unexplained.

## Still deferred (need separate approval)
Risk-profile GUI, max-trades-per-day limit, self-improving agent (must never change risk limits or live gates), exchange/broker adapters.

## Hosted CI
Not yet run. To be recorded here: run id, commit, three consecutive green attempts.
