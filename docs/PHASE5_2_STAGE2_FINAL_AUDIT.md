# Phase 5.2 Stage 2 - Final Audit

## Final status: STAGE 2 PASSED (paper trading scope only)

Live trading remains LOCKED. This status does not say the system is ready for live trading, testnet trading or real money. It says the Stage 2 paper-runtime hardening work packages are implemented and verified as far as the evidence below reaches.

Commit audited: `efa1de241d5e7d95e3957abbc317918f46e70d57` (branch `stage2-wp7-lifecycle`; later commits are documents and evidence only).
Hosted CI: run 41637014964, attempts 1, 2 and 3, all jobs green each time (sqlite 3.12 and 3.13, postgres16 with zero skips and 78/78 required tests, crash-repeat, windows-crash 3.13 and 3.14, e2e-real-app, docker, static). Details: PHASE5_2_STAGE2_TEST_REPORT.md.

## Acceptance criteria
| Criterion | Verdict | Evidence |
|---|---|---|
| All mandatory work packages implemented or adjudicated | Met | WP1-WP7, changelog |
| Risk sizing uses approved executable-price-aware calculations | Met | WP1: grid of >500 cases; loss at stop and notional never above approved |
| Exit handling does not silently abandon protected positions | Met, with a stated trade-off | WP2-WP2c, WP3 stall detection (incident + entry block) |
| Partial exits survive restarts and reconcile | Met | WP3 durable table + REDUCING; WP7 real-kill partial exit |
| Public readiness reflects critical worker failures | Met | WP4, WP6, WP7 |
| Time-stop status explicit | Met | WP5: DISABLED default, NOT_ENFORCED reported, persisted, restart-safe |
| Audit failures visible and handled safely | Met, with a stated limit | WP6 |
| Full suites pass in required environments | Met | SQLite and PG16 locally; HTTP tests in hosted CI |
| Hosted CI green on a recorded SHA | Met | above, three runs |
| No unresolved critical safety defect | Met on the evidence; see open items | below |

## Defects found and fixed during Stage 2 (own work included)
- NaN / infinity passed the first version of the final sizing gate (comparisons with NaN are false): fixed, tested.
- My Stage 2 plan wrongly said heartbeat freshness was missing from readiness; the real defect was premature HEALTHY at iteration start. Corrected and fixed.
- Take-profit and time-stop waits were reported as unverified protection (false incidents blocking entries): fixed in WP2c.
- Audit writes swallowed in several lifecycle paths: fixed in WP6.
- An exit stuck in PARTIALLY_FILLED was silent forever: fixed in WP3.
- CI skip gate failed once because renamed tests were not updated in the required list; fixed and checked with a script.
- Mutation checks found tests that did not bite (WP7 restart assertion, a vacuous WP1 assertion): strengthened.

## Open items and honest limits (not hidden, not fixed)
1. **Paper only.** The paper adapter fills instantly. Partial fills, cancels, delays, rate limits and real spreads were exercised through test doubles. Nothing was observed on a real exchange. A pass here is not evidence for live behaviour.
2. **Stop-loss policy trade-off (owner decision).** A stop is sent at once, except when the spread exceeds the suspect limit (default 10 %); then it must persist 3 cycles (about 10 s at default intervals) before it is sent. A genuine flash crash can look the same, so a real stop can be delayed. The alternative (always sell at once) can dump the position on a bad quote. Both settings are configurable; `off` removes the safeguard.
3. **Audit buffer is memory-only.** While audit storage is down, held events live in memory; a crash then loses them. The CRITICAL log line is the only trace. Entries are blocked while degraded, exits are not.
4. **Single database.** A database-wide outage blocks order creation (intended) and the audit store together.
5. **Stalled exits are not auto-resolved.** They raise an incident and block entries; a human or the order-recovery worker decides. This is deliberate (no second sale on an unknown first one).
6. **Windows 3.14 hang (run 41311105233) is unexplained.** It has not recurred in any run since, including three runs of the audited commit, but it was not root-caused. Treat as a monitored risk.
7. **Local HTTP tests skipped.** 36 HTTP tests do not run in the sandbox (no fastapi). They were run and passed in hosted CI only.
8. **Time-stop changes need a restart with no open positions** (deliberate; no runtime API).
9. **Stage 2 branches are stacked and not merged to main.** Merging is the owner's decision; the audited head contains all of them.
10. **Not verified:** power loss, disk failure, PostgreSQL server death while connected, long-duration soak, performance under load.

## Conditions that keep live locked
No live or testnet adapter was added, no credentials were handled, no path enables live trading, no risk limit changes automatically. Before any live step: real-exchange behaviour for partial fills and stop handling must be tested on a sandbox venue under separate authorization; the items above (1, 2, 3, 6) must be re-assessed.

## Deferred, separate milestones (need explicit approval)
Risk-profile GUI (Conservative / Balanced / Custom, human-set only, audited); maximum-trades-per-day limit; Phase 9 self-improving agent (must never alter risk limits or live gates by itself).
