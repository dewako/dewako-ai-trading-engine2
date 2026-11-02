# Paper vs live

* Every order, position and trade row carries `environment` PAPER or LIVE. Adapters declare their environment; the engine refuses a mismatch. Paper cash lives in `paper_accounts`; live balances are never used for paper.
* **LIVE order gate** (`SafetyController.live_gate`), re-evaluated for every order: live switch ENABLED; trading state SYSTEM_HEALTHY (no kill switch, no reconciliation required, risk healthy); exchange row is LIVE, enabled, verified, key read+trade, no withdraw, credential without withdrawal permission; a clean reconciliation younger than 10 minutes; strategy version RELEASED and enabled (experimental strategies are stored as DRAFT and can never pass). Anything that cannot be read blocks.
* Fresh install: live DISABLED. After a restart: ENABLED becomes BLOCKED unless full startup verification passed (the API process always blocks it until a person re-enables).
* Futures/perpetuals are modelled (`market_type`) but rejected as NOT_IMPLEMENTED.

## Stop-loss and take-profit exits: paper behaviour versus real-market guarantees

What the system does today (paper) and what it does NOT promise for a real market.

**How an exit works.** The exit monitor reads the quote each cycle and evaluates the triggers on the **bid** (stop: bid <= stop; take-profit: bid >= target; optional time stop). A triggered exit is a SELL sent through the Execution Engine only after the Risk Engine approves it as an exit of an existing long position.

**Entry spread versus exit spread, and stop-loss versus take-profit.** Entries are refused when the spread exceeds the normal limit (`DEWAKO_MAX_SPREAD_PCT`). Exits are split by urgency:
- **Stop-loss: never deferred for spread.** When the bid reaches the stop, the exit is sent at market immediately through any spread, with no tolerance setting involved. A stop limits downside; waiting for a tighter spread would let the loss grow. Only a quote that is missing, stale, crossed, non-positive or non-finite stops it (protection is then reported unverified; a price is never guessed).
- **Take-profit and time stop: bounded tolerance.** They exit normally within the normal limit, may use the explicit tolerance `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT` (unset by default), and otherwise wait for a favourable spread. The tolerance never lowers the normal limit.

| Trigger and condition | Result |
|---|---|
| Any trigger, spread within the normal limit | Exit sent normally |
| **STOP, any spread** | Exit sent at once; if the spread was above the normal limit, `EMERGENCY_EXIT_WIDE_SPREAD` is audited with policy `STOP_LOSS_IMMEDIATE` |
| TAKE_PROFIT / MAX_HOLD, spread above the normal limit and within the tolerance | Exit sent; `EMERGENCY_EXIT_WIDE_SPREAD` audited |
| TAKE_PROFIT / MAX_HOLD, spread above the tolerance, or no tolerance configured | **Exit NOT sent**; position stays OPEN; CRITICAL incident `EXIT_BLOCKED_WIDE_SPREAD`; audited once per streak with the reason, bid, spread and limit; retried every cycle; new entries blocked |
| Any trigger, stale, crossed, zero, negative, non-finite or missing quote | No order, no guessed price; `EXIT_PROTECTION_UNVERIFIED` incident; new entries blocked |

An incident clears only after the position has actually closed or protection is verified again. A position is never reported closed before the exit order has filled, and the recorded fill price is the quoted price, never the stop or target price.

**What paper trading does NOT show you (real-market differences).**
- A stop is not a guaranteed price. In a gap or a thin book the real fill can be far worse than the stop; the paper fill is the quoted bid plus the configured slippage, which is a model, not a measurement. A stop-loss now sells through any spread, so in a thin or glitching market it can fill far from the stop, and a single bad bid tick can trigger it: the trigger is the bid, and the spread check no longer filters a bogus quote for stops. A deferred take-profit can wait while price keeps moving (the gain can shrink).
- Paper fills are immediate and complete. A real exchange can fill partially, reject, rate-limit or be unreachable at the moment a stop triggers. Partial-exit tracking is Stage 2 WP3 and is not yet implemented.
- Paper quotes are simulated or public-feed quotes. Latency, queue position, order-book depth and exchange outages are not modelled.
- Nothing here is evidence of live readiness. Live trading remains locked.
