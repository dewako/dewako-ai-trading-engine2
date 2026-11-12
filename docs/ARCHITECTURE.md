# Architecture (after hardening)

```
Market data -> MarketState -> Strategy -> SignalEngine -> Risk Engine -> ExecutionEngine -> ExchangeAdapter
                                              |               |               |
                                         signals table   risk_state     orders / order_events / trades / positions
                       Safety controller (kill switch, trading state, live switch)  gates every LIVE order
                       Reconciliation service (read-only)  ->  RECONCILIATION_REQUIRED blocks live
                       Auth (users, roles, sessions) + audit_log wrap every API route
```

* **Only `execution/engine.py` talks to an exchange adapter for orders.** Strategies, risk and the API never do. There is no API route that places an order; orders come from server-side bots (`runtime/bot_runner.py`).
* **Persistence:** `persistence/` (schema migrations, transactions). **Backends: SQLite and PostgreSQL 16.** Both run the full suite in hosted CI (see STAGE1_BASELINE_VERIFICATION.md); PostgreSQL has not been run against managed or production infrastructure.
* **Workers:** `runtime/workers.py` supervises periodic jobs (order recovery, reconciliation, risk, exit monitor, bot runner, market data, lease, audit integrity) and records heartbeats. The API process starts them at startup (`runtime/application.py`; `DEWAKO_WORKERS=0` turns them off). Updated 2026-11-13 on `9172eb2`.
* Paper and live use separate adapters (`environment` attribute), separate rows (`environment` column) and separate accounting (`paper_accounts`, positions/orders filtered by environment). The engine refuses an adapter whose environment differs from the request.
