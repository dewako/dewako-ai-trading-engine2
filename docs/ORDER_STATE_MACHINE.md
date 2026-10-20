# Order state machine

States: CREATED, SUBMITTING, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, CANCELLED, REJECTED, EXPIRED, UNKNOWN, RECONCILING, FAILED.
Allowed transitions are the table in `execution/state_machine.py`; anything else raises `IllegalTransition`.

* Every transition is one DB transaction: compare-and-set on the previous state (`StaleState` if someone else moved it), update, append to `order_events` (previous state, new state, exchange response with secrets scrubbed, error, actor, correlation id).
* **SUBMITTING is saved before the exchange is called.** A crash afterwards leaves SUBMITTING, which recovery treats as "may exist on the exchange".
* Definite refusals (`InvalidOrder`, `InsufficientBalance`) -> REJECTED. "Nothing was sent" (`NotConnected`, NOT_IMPLEMENTED) -> FAILED.
* Timeout, HTTP 5xx, reset, malformed/mismatched response -> UNKNOWN -> RECONCILING -> ask the exchange by client order id.
* FAILED can only be left through a retry, and only when the latest event is `CONFIRMED_ABSENT` (the exchange answered "no such order"). A timeout while checking is never proof of absence.
* Idempotency: `execution_intents UNIQUE(bot_id, signal_id, intent)` plus `orders.client_order_id UNIQUE`. The client id is deterministic (`DW` + 30 hex of SHA-256 over bot|signal|intent), 32 chars, and is reused by a safe retry.
* Positions only change in the same transaction as the fill that caused them. LIVE positions are closed only from an exchange-reported fill.
