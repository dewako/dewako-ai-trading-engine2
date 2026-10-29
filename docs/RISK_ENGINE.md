# Risk engine

Limits are immutable objects; only a human can replace them. `RiskEngine(kill_switch_active=...)` treats the durable kill switch as halted, and treats a failure to read it as halted.

Persisted in `risk_state` (`risk/state.py`): day-start/peak/current equity, daily realised P&L, consecutive losses, last loss time. `RiskStateStore.context()` builds the `RiskContext` from these rows plus open positions, so a restart or a careless caller cannot reset daily loss. Counters roll over at 00:00 UTC; drawdown and loss streak persist across days. `init_scope` never overwrites existing counters.

If a closing fill cannot be recorded in risk state, `risk_healthy` is set to 0 and trading is blocked until a person looks.

Exits go through `approve_exit` (must be opposite side of a real position, size = position, blocked when halted).
**Not persisted/enforced yet:** exposure caps use position notional at average price (no mark-to-market), correlated-risk and per-asset cooldown inputs are not fed from the database.


## Executable-price basis for pre-trade accounting (Phase 5.1)
The engine's backstop checks (exchange minimum notional, available balance including fee, position %, total exposure %, balance after reservations) and the reservation use `conservative_risk_price`: BUY = max(approved reference, executable ask), SELL = min(approved reference, executable bid). The order keeps the approved reference as its price. The quote is re-read just before submission; a BUY whose ask has risen above the reserved unit price is not sent and its reservation is released. Invalid or missing prices block the order before any intent exists.
Because the RiskEngine sizes at the reference price, give it headroom below the backstop cap (e.g. 9.5% vs 10%) or cap-sized entries will be refused after slippage.


### Phase 5.2: send-time SELL rules and quantity validation

Immediately before submission a SELL is re-validated against the current bid: exchange minimum notional, lot size and the quote guard. A failure marks the order FAILED (`RULES_REVALIDATION_FAILED`), audits `ORDER_BLOCKED` (stage pre-send), and sends nothing; replay returns DUPLICATE, so there is no retry. Exchange filters and balances must be finite and valid; quantity precision is rounded down using decimal arithmetic, never increased to reach a lot boundary. Malformed exchange fill responses remain unresolved and are not applied to position/trade accounting.

**Open limitation:** sizing still uses the reference price. An entry sized exactly at `max_position_pct` is blocked if the executable ask is higher. Quote-based sizing is NOT IMPLEMENTED; operators should keep `max_position_pct` for the strategy below the engine cap (the fixtures use 9.5 against 10.0). Verification evidence and remaining execution risks are in `docs/PHASE5_2_FINAL_VERIFICATION_REPORT.md`.
