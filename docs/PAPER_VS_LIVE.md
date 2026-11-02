# Paper vs live

* Every order, position and trade row carries `environment` PAPER or LIVE. Adapters declare their environment; the engine refuses a mismatch. Paper cash lives in `paper_accounts`; live balances are never used for paper.
* **LIVE order gate** (`SafetyController.live_gate`), re-evaluated for every order: live switch ENABLED; trading state SYSTEM_HEALTHY (no kill switch, no reconciliation required, risk healthy); exchange row is LIVE, enabled, verified, key read+trade, no withdraw, credential without withdrawal permission; a clean reconciliation younger than 10 minutes; strategy version RELEASED and enabled (experimental strategies are stored as DRAFT and can never pass). Anything that cannot be read blocks.
* Fresh install: live DISABLED. After a restart: ENABLED becomes BLOCKED unless full startup verification passed (the API process always blocks it until a person re-enables).
* Futures/perpetuals are modelled (`market_type`) but rejected as NOT_IMPLEMENTED.

## Stop-loss and take-profit exits: paper behaviour versus real-market guarantees

What the system does today (paper) and what it does NOT promise for a real market.

**How an exit works.** The exit monitor reads the quote each cycle and evaluates the triggers on the **bid** (stop: bid <= stop; take-profit: bid >= target; optional time stop). A triggered exit is a SELL sent through the Execution Engine only after the Risk Engine approves it as an exit of an existing long position.

**Entry spread versus exit spread.** Entries are refused when the spread exceeds the normal limit (`DEWAKO_MAX_SPREAD_PCT`). A triggered exit meets the same normal limit first, then the explicit exit-spread tolerance `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT` (unset by default). The tolerance applies to exits only, never lowers the normal limit, and applies to every exit trigger (stop, take-profit and time stop). The quote must still be fresh, positive and uncrossed whatever the spread.

| Condition at the trigger | Result |
|---|---|
| Spread within the normal limit | Exit sent normally |
| Spread above the normal limit, within the tolerance | Exit sent; `EMERGENCY_EXIT_WIDE_SPREAD` audited |
| Spread above the tolerance, or no tolerance configured | **Exit NOT sent**; position stays OPEN; CRITICAL incident `EXIT_BLOCKED_WIDE_SPREAD`; `EXIT_BLOCKED_WIDE_SPREAD` audited once per streak with the reason, bid, spread and limit; retried every cycle; new entries blocked |
| Stale, crossed, zero, negative, non-finite or missing quote | No order, no guessed price; `EXIT_PROTECTION_UNVERIFIED` incident; new entries blocked |

An incident clears only after the position has actually closed or protection is verified again. A position is never reported closed before the exit order has filled, and the recorded fill price is the quoted price, never the stop or target price.

**What paper trading does NOT show you (real-market differences).**
- A stop is not a guaranteed price. In a gap or a thin book the real fill can be far worse than the stop; the paper fill is the quoted bid plus the configured slippage, which is a model, not a measurement. The deferral policy above means a wide-spread exit can wait while price keeps moving: the loss can grow during the deferral. Choosing the tolerance trades "exit at any price" against "do not give away the spread".
- Paper fills are immediate and complete. A real exchange can fill partially, reject, rate-limit or be unreachable at the moment a stop triggers. Partial-exit tracking is Stage 2 WP3 and is not yet implemented.
- Paper quotes are simulated or public-feed quotes. Latency, queue position, order-book depth and exchange outages are not modelled.
- Nothing here is evidence of live readiness. Live trading remains locked.
