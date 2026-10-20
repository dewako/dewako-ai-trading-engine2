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
