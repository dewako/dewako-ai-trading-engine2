# Risk engine

Limits are immutable objects; only a human can replace them. `RiskEngine(kill_switch_active=...)` treats the durable kill switch as halted, and treats a failure to read it as halted.

Persisted in `risk_state` (`risk/state.py`): day-start/peak/current equity, daily realised P&L, consecutive losses, last loss time. `RiskStateStore.context()` builds the `RiskContext` from these rows plus open positions, so a restart or a careless caller cannot reset daily loss. Counters roll over at 00:00 UTC; drawdown and loss streak persist across days. `init_scope` never overwrites existing counters.

If a closing fill cannot be recorded in risk state, `risk_healthy` is set to 0 and trading is blocked until a person looks.

Exits go through `approve_exit` (must be opposite side of a real position, size = position, blocked when halted).
**Not persisted/enforced yet:** exposure caps use position notional at average price (no mark-to-market), correlated-risk and per-asset cooldown inputs are not fed from the database.
