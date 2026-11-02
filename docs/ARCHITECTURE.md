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
* **Workers:** `runtime/workers.py` supervises periodic jobs (recovery, reconciliation, ...) and records heartbeats. The default API process does not start any workers yet; they are building blocks to be wired when live adapters exist.
* Paper and live use separate adapters (`environment` attribute), separate rows (`environment` column) and separate accounting (`paper_accounts`, positions/orders filtered by environment). The engine refuses an adapter whose environment differs from the request.
