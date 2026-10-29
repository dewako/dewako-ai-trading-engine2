# Exchange adapters

Contract: `exchanges/base.py` (async). Used by the engine: `create_order`, `get_order(client_order_id=)`, `cancel_order`, `get_open_orders`, `get_positions`, `get_balance`, `get_markets`, `get_api_permissions`. `OrderNotFound` means the exchange positively has no such order; `OrderOutcomeUnknown` means the result is unknown; `NotImplementedForExchange` means not built.

## Capability matrix (truthful; "verified" means executed against the real service, which has not happened for any row)

| Adapter | Market data | Fake / test double | Testnet | Verified order lookup + reconciliation | Production submission |
|---|---|---|---|---|---|
| Paper | n/a | n/a (it is the simulator) | n/a | n/a | MARKET orders only, simulated money |
| Fake / FileLive (tests) | n/a | yes: lookup, fills, balances, timeouts, crash recovery | n/a | tests only; says nothing about a real exchange | **not applicable** |
| Binance | public REST provider (`market_data/binance_public.py`): written, unexecuted against the live service (network blocked here) | no | no | **no** | **NOT_IMPLEMENTED** (adapter: signed permission check only; unrun against the live exchange) |
| Bybit | public provider written, unexecuted | no | no | no | NOT_IMPLEMENTED (stub) |
| OKX | public provider written, unexecuted | no | no | no | NOT_IMPLEMENTED (stub) |

No live path exists that is verified. `live_execution_verified` is False for every real adapter, so the engine live lock refuses LIVE orders to them regardless of switches, flags, confirmation phrases or environment variables, and `activation_readiness()` refuses to turn live ON while no enabled LIVE exchange can pass (`test_live_activation.py`). Enabling any of these requires: testnet runs, verified `get_order(client_order_id=)`, fills and balances, and a reviewed change to the capability flag. None of that has been done.

Pre-trade validation (`execution/validation.py`): symbol listed/enabled, order type allowed, lot step (rounded down), min/max quantity, tick size (limit), min notional, available quote balance. Missing information blocks the order. Not covered: leverage/margin/liquidation/reduce-only/funding (futures are rejected outright).

## Phase 4.1: capability contract, registry, error normalization

`exchanges/contract.py` is the single place that says what an adapter must declare to be used.

- **Operation → capability.** `OPERATION_CAPABILITY` maps each operation to one `AdapterCapabilities` flag (`create_order`→`order_submission`, `get_order_by_client_order_id`→`lookup_by_client_id`, `get_markets`→`instrument_metadata`, …). `supports()` / `require()` answer from the declaration; an undeclared operation raises `NotImplementedForExchange`. There is deliberately no withdraw or transfer operation.
- **Engine enforcement** (`ExecutionEngine._refusals`, before any order row and before any adapter call): the adapter must declare `order_submission`, `instrument_metadata` and `account_snapshot`; a non-PAPER adapter must also declare `lookup_by_client_id` (so an ambiguous submission can always be resolved). Unknown environments are refused.
- **TESTNET is not an order environment in this build.** `ADAPTER_ENVIRONMENTS = (PAPER, TESTNET, LIVE)` may be *declared*, but `ORDER_ENVIRONMENTS_ENABLED = (PAPER, LIVE)`: the schema has no TESTNET order rows and no verified testnet adapter exists. TESTNET execution is refused with zero adapter calls. Status: BLOCKED, by design, pending a migration and a verified adapter.
- **Pre-send rule.** `NotImplementedForExchange` from `create_order` is the only definitive "not sent". Any other exception, including a generic `NotConnected` (a connection dropped mid-request cannot be told apart from never-sent), puts the order in UNKNOWN and reconciles by client order id. It is never resent; trading stays RECONCILIATION_REQUIRED until cleared. Adapters raise `NotConnected` from `create_order` only if they can prove nothing was transmitted, and `OrderOutcomeUnknown` otherwise.
- **Registry** (`register_adapter`): refuses bad names, non-adapters, duplicates, replacing built-ins, invalid environments, un-instantiable classes, `order_submission` without client-id lookup / metadata / account snapshot, `test_double` unless explicitly allowed, and any `live_execution_verified` / `testnet_verified` claim not on the reviewed allowlists (`LIVE_VERIFIED_ADAPTERS`, `TESTNET_VERIFIED_ADAPTERS`, both empty). Registering an adapter never changes live activation.
- **Errors** (`normalize_error`): `REJECTED`/`UNSUPPORTED`/`NOT_FOUND` are definitive, no retry. Writes with any other failure are `RECONCILE_FIRST` and outcome-unknown; only reads may `BACKOFF_RETRY`. Messages are redacted and bounded.
- **Testing a new adapter**: subclass `ExchangeAdapter`, declare only what is implemented, register it, and run `tests/test_adapter_contract.py` patterns against it with a fake transport. Mock responses are never evidence about a real exchange; nothing is marked verified without operator evidence and a reviewed allowlist change.
