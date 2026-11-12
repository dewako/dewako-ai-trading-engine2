# Paper-trading qualification plan

Simulated money only. Live trading stays LOCKED. A green paper qualification is evidence that the paper runtime behaves correctly under
the conditions tested; it is not evidence that any strategy makes money and it is not live-trading readiness.

## What exists (verified 2026-11-13)
| Area | Evidence | Where |
|---|---|---|
| Checklist of 39 existing tests | order and position lifecycle, entries/exits, stop-loss / take-profit / time stop, partial exits, spread and slippage, execution delays, rejected orders, duplicate prevention, risk-limit enforcement, persistence, crash recovery, reconciliation | `.github/required-tests/paper-qualification.txt` |
| Checklist job | runs every test file that holds a checklist test, on SQLite and PostgreSQL 16; each name must run and pass | `paper-qualification.yml` `checklist` |
| Bounded soak | the real Application (supervised workers, PAPER adapter, real database) for N cycles with restarts | `backend/tests/paper_soak.py`, `paper-qualification.yml` `soak` |
| Schedule | nightly 02:43 UTC and manual (cycles and restart interval as inputs) | `paper-qualification.yml` |

The checklist adds no new assertions: it names which existing, CI-required tests make up the qualification, so a renamed or deleted test
is noticed. The same tests also run in `ci.yml` on every push.

## The soak
Each cycle is one newly closed 15m bar for the strategy (Trend AI 2.2 on BTC/USDT, PAPER). The script sets a paper price (normal noise,
through the stop, or through the target), runs every supervised worker once (except `market_data` and `price_refresh`, which need the real
network), and checks invariants. Every `--restart-every` cycles the Application is shut down and a new one starts on the same database.

Fails on: a failed worker iteration; a CRITICAL incident; two OPEN positions for one bot; an OPEN position without a stop-loss; a duplicate
client order id; open positions changed by a restart; peak memory growth after warm-up above the limit (100 MB default); fewer than
5 round trips (a soak that does not trade proves nothing). Reports per-worker latency (p50 / p95 / max), memory, database size, orders.

Two findings while building it, both resolved without changing production code:
* With unchanging candles the strategy produced the same signal every cycle and duplicate protection correctly refused it after the
  first trade, so the soak now moves the market one bar per cycle.
* The Risk Engine's loss cooldown and the worker supervisor use the wall clock. Virtual time made the exit-protection check see the
  workers as stale (correctly blocking entries). The soak therefore runs on the wall clock and sets the paper account's limits it needs
  through the operator limits store (cooldown 0, wide loss limits), recorded in its report. The risk rules themselves are tested by the
  suite with their defaults. Consequence: the soak measures stability under churn, not the risk policy.

Local evidence (SQLite, Python 3.13, this workspace, `ci-hardening` before push): 300 cycles, 3 restarts, 2,700 worker iterations,
0 failures, 42 round trips, 0 incidents, peak memory +0.9 MB after warm-up, p95 bot_runner 38.6 ms, exit_monitor 26.6 ms.
Hosted results are in `GITHUB_CI_FINAL_VALIDATION_REPORT.md`.

## Long-running soak (dedicated machine, not GitHub-hosted runners)
GitHub-hosted runners are short-lived (90-minute job limit here). A multi-day soak belongs on a machine that stays up (the owner's PC or a
small VPS), using the same script:
```bash
cd backend/tests
DEWAKO_TEST_DB=postgres DEWAKO_TEST_PG_URL=postgresql://... PYTHONPATH=.. \
  python paper_soak.py --cycles 500000 --restart-every 1000 --warmup 500 --report soak-$(date +%F).json --junit soak.xml
```
Proposed acceptance for the long soak (needs the owner's approval of the numbers): 7 days; zero invariant failures; memory growth after
warm-up under 100 MB; p95 worker latency not more than 2x the bounded-soak value; database growth recorded per day.

## Not covered (stated limits)
* Market data in the soak is injected; the real public-feed path (`market_data`, `price_refresh`) is not exercised by it.
* No real exchange, no network faults, no PostgreSQL restart mid-run (a manual script exists: `scripts/pg_restart_check.py`).
* A forward paper run of a strategy on live public data (for example Dewako ETH 1.0) is a separate, owner-run activity; it judges the
  strategy, while this plan judges the runtime.
