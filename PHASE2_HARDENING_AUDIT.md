# Phase 2 Hardening Audit

Written before any hardening change. Audited at the end of Phase 3 (strategy engine).

## What exists

| Area | State today |
|---|---|
| Database layer | Only `market_data/store.py` (SQLite candle store). No DB for orders, positions, signals, risk, safety, users, audit |
| PostgreSQL | `docker-compose.yml` starts Postgres with a dev password. **No schema, no migrations, nothing connects to it** |
| Orders | `execution/order_engine.py`: in-memory `_seen` dict for idempotency. UNKNOWN is returned but not persisted or tracked |
| Positions | None persisted. Paper adapter keeps holdings in a dict |
| Execution engine | `OrderEngine` wraps one adapter. No separation of paper and live, no state machine, no event log |
| Exchange adapters | Binance (read-only permission check; order calls are not implemented against the real API), Bybit/OKX stubs, Paper |
| Risk engine | In-memory limits and a `_halted` flag. Daily loss, drawdown, consecutive losses and cooldown arrive via a caller-supplied `RiskContext`; nothing persists them |
| Kill switch | `RiskEngine._halted`, in memory. **A restart clears it** |
| Authentication | One shared bearer token (`DEWAKO_API_TOKEN`), compared in constant time. No users, no roles, no sessions. Every authenticated caller is "admin" |
| Credential vault | Fernet, single key, no version, no rotation. `ExchangeManager` keeps records in memory only |
| Strategy registry / signals | Good. Version lock is a JSON file; signal ids deterministic; dedupe set is in memory only |
| Background workers | None. Nothing runs without an HTTP call |
| Paper/live separation | `PaperAdapter` is a separate class, but no environment concept; `OrderEngine` accepts any adapter |
| Logging / audit | Python lists on objects (`audit`). Lost on restart, no correlation IDs |

## Dangerous existing behaviour

1. Kill switch resets on restart (fails open).
2. Duplicate-order protection is in process memory: a crash between send and record, or two workers, can double-submit.
3. After an unresolved timeout the order is returned as UNKNOWN and then forgotten.
4. Any token holder can change anything; there is no separation of the live-trading, risk-settings and kill-switch-release powers.
5. Exchange records live in memory, so credentials vanish on restart and nothing links them to an environment (paper/live).
6. `OrderEngine` will submit through whatever adapter it is handed; nothing checks that live is enabled.
7. `RiskContext` is trusted from the caller, so daily loss can be reset by simply passing zero.
8. Signal-dedup state is lost on restart, so a restarted worker can re-emit and re-execute a signal.

## What will change

New: SQL schema with migrations; transactional repositories; order state machine with event log and unique intent constraint; durable safety state (kill switch, trading state, live flag); persisted risk counters; users, roles, hashed passwords and session tokens; audit log with correlation ids; credential key versioning and rotation; reconciliation service; execution layer that is the only place that talks to adapters; live gate with all conditions.

Kept unchanged: strategies, indicators, version lock logic, signal engine, switching rules, market-data providers, selector.

## Constraints in this environment (will be restated in the final report)

- **Superseded in Phase 3.2:** PostgreSQL 16 does run in this environment (server binaries, no Docker) and the suite is verified against it through a stdlib wire-protocol client. See `PHASE3_2_HARDENING_AUDIT.md`. (This audit was written when only the client was checked, which was an incorrect conclusion.)
- **FastAPI cannot be installed** (package index unreachable). Authorization is implemented as framework-independent services and tested directly. The FastAPI wiring is not executed here.
- Exchange APIs are unreachable, so Binance order endpoints and every adapter are tested against fakes only.
- Binance, Bybit and OKX live order execution adapters are **not implemented** and will return NOT_IMPLEMENTED.

## Architecture conflicts

- The spec's TypeScript-style adapter interface is adapted to the existing async Python `ExchangeAdapter`, extended with `get_order_by_client_id`-style lookups already present as `get_order(..., client_order_id=)`.
- The API currently has one `require_admin`; it will be replaced by role dependencies.

## Migration plan

1. Persistence layer + `schema_version` table and ordered migrations.
2. Order state machine and idempotent intents on top of it.
3. Safety controller and live gate; risk persistence.
4. Auth/RBAC, audit, vault versioning.
5. Reconciliation, startup sequence, API wiring.
6. Tests per stage; docs; readiness checklist.
