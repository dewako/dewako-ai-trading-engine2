# Phase 4 repository assessment (before any Phase 4 code)

Inspected 2026-10-21 on the Phase 3.4 final-hardening tree (Linux, Python 3.13). Status of Phase 3: INCOMPLETE. Live trading: LOCKED.

## 1. Current architecture and order flow
`market_data/*` (public providers, hub, store, state_builder) -> `MarketState` -> `strategies/*` (class-based `Strategy`, registry keyed by (name, version), `SignalEngine.validate_signal`) -> `risk/engine.py` (`RiskDecision`) -> `execution/engine.py` (`ExecutionEngine.execute`, the only caller of adapter `create_order`) -> `exchanges/*` adapter -> `order_store` / `state_machine` / `positions` / `reservations` -> `reconciliation/service.py` -> `persistence/audit.py` (hash chain) + `incidents`.
Engine sequence: `_refusals` (approved decision, signal_id, environment in PAPER|LIVE, market_type SPOT, adapter present, adapter.environment == requested, LIVE lock = adapter must be `live_execution_verified` or `test_double`) -> live gate (LIVE only) -> exchange-rule validation (fail closed) -> ONE transaction: signal + intent + order row + durable risk reservation -> SUBMITTING persisted before the call -> adapter call -> outcome; timeouts become UNKNOWN/RECONCILING, never resent; `recover()` and `sync_active_orders` at startup; `submit_authorized` (worker lease fencing) checked before every LIVE send.
Safety: `safety/controller.py` (kill switch, live switch, `live_gate`, `activation_readiness`, `readiness_state`, evidence attestations), auth/RBAC in `auth/service.py` + `api/security.py`.

## 2. Reusable interfaces
- `ExchangeAdapter` ABC + `AdapterCapabilities` (all flags default False) + `classify_error` + `Fill`/`AccountBalance` (`exchanges/base.py`); `ExchangeManager`/`PersistentExchangeManager` (vault, permission check, redaction); `REGISTRY` dict; `core/symbols.py` (BASE/QUOTE normalisation, XAU/USD works via the USD quote).
- `Strategy` ABC, `register`, `StrategyVersionLock` fingerprints, `StrategySwitcher` (explicit KEEP/CLOSE/PREVIOUS_MANAGES policy, human-only for live), `rule_builder.py` (declarative, pydantic-validated, "no code is run": the natural safe runtime for imported packages), `StrategySelector`.
- `AuditLog` (hash-chained, scrubbing), `incidents`, `order_events`, `worker_health`, `safety_state`, `credentials` vault (Fernet, rotation).
- Test fixtures: `FakeLive`, `FileLive`, `SimExchange`, `PaperExchange`, `engine_for`, `make_live_ready`.

## 3. Modules that need extension
`exchanges/base.py` (explicit operation->capability map, error normalisation with retry class), `exchanges/registry.py` (guarded registration), `execution/engine._refusals` (capability enforcement, TESTNET handling), `docs/EXCHANGE_ADAPTERS.md`. Later: `strategies/loader.py` (currently executes arbitrary Python in-process when `DEWAKO_ALLOW_CODE_PLUGINS=1`; must NOT be reused for ZIP packages), `api/main.py` (new guarded routes), `runtime/application.py` (activity/AI wiring), `frontend/prototype/index.html`.

## 4. Missing capabilities per Phase 4 feature
| Feature | Missing |
|---|---|
| 4.1 adapters | no capability->operation contract enforced by the engine; no normalised error/retry object; registry accepts anything; **no TESTNET environment**: DB CHECK constraints allow only PAPER/LIVE in 8 tables, engine accepts only PAPER/LIVE; no testnet adapter exists; no XAUUSD venue adapter (Binance/Bybit/OKX stubs are crypto spot; the spot-only restriction would reject CFD/margin gold) |
| 4.2 ZIP packages | no package schema, importer, hash/manifest model, lifecycle states, quarantine; no isolated Python runtime; strong sandboxing is NOT available on this OS (a subprocess is not a sandbox) so only the declarative runtime (rule_builder spec) can safely execute imported packages; current strategy `timeframes` support 1m/5m in `timeframes.py` but built-in strategies declare 15m-4h; no XAUUSD strategy |
| 4.3 AI manager | `ai/` and `news/` packages are empty; no provider interface, modes, redaction policy, schemas, circuit breaker, or config persistence; no network access here, so no real local or cloud provider can be exercised (mock providers only) |
| 4.4 Live Activity | no unified event schema/stream: data is spread over `audit_log`, `order_events`, `incidents`, `worker_health`, `reconciliation_*`; no `/activity` API; **frontend is one static 88 KB demo page (`frontend/prototype/index.html`), vanilla JS, no build, no backend client, no JS test runner**, all data fake ("DEMO"/"SIMULATED" labels); `fastapi` unavailable so HTTP tests cannot run here |
| 4.5 integration | depends on the above |

## 5. Safety-critical code that must not be weakened
Engine `_refusals` live lock, `live_gate`, `activation_readiness`/`enable_live`, SUBMITTING-before-send, UNKNOWN handling and `retry` rules, durable reservations, `submit_authorized` fencing, kill-switch recheck before send, audit hash chain + append-only triggers, strategy fingerprints and `StrategySwitcher` policy, RBAC guards, PG bounded waits, `bounded.py` crash-test harness.

## 6. Dependencies and runtime constraints
Python 3.13 here (user runs Windows 3.11/3.14). pydantic v2, cryptography, stdlib PG client; FastAPI/httpx/pytest not installable in this container (custom runner + stand-in only), Node 22 available (no frontend deps, no network for npm), no Docker daemon, no outbound exchange/AI access. Docker/CI never executed.

## 7. File-level plan (small, backward compatible)
- 4.1: `exchanges/contract.py` (new), `exchanges/registry.py` (guarded `register_adapter`), `execution/engine.py` (`_refusals`: capability + TESTNET), tests `test_adapter_contract.py`, docs. TESTNET order environment: NOT enabled (needs a schema migration + a real testnet adapter + credentials); the engine refuses it explicitly.
- 4.2: `strategy_packages/` (schema, safe unzip, validator, declarative runtime bridge to `rule_builder`, lifecycle store + migration 6, guarded routes). Python-runtime packages: refused ("no isolation available").
- 4.3: `ai/` (provider interface, mock/Ollama-LM-Studio-compatible HTTP clients, cloud-neutral client, privacy/redaction, schemas, circuit breaker, config in `safety_state`-style table, no direct execution path).
- 4.4: `activity/` (event schema + read-model over existing tables + new small `activity_events` table only if needed), `GET /activity`, frontend page section in the prototype, Node-based tests of pure rendering/filter functions.
- 4.5: wiring + docs + readiness update.

## 8. Acceptance tests per stage
4.1: conformance suite over every shipped adapter; capability enforcement; env mismatch; TESTNET refusal; registration cannot claim live/testnet verification; no adapter call when authorization fails; error normalisation. 4.2: valid/invalid/malicious ZIP matrix, hash tamper, bombs, symlinks, version collision, unsupported runtime, signals through the normal pipeline. 4.3: zero cloud calls in local mode, fallback policy, redaction, schema rejection, injection, breaker, AI-off. 4.4: schema, ordering, dedupe, replay, redaction, filters, pagination, unknown-state rendering, pause-UI-does-not-stop-backend. Each stage: full SQLite + PG16 suites green before the next stage.
