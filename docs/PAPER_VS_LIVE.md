# Paper vs live

* Every order, position and trade row carries `environment` PAPER or LIVE. Adapters declare their environment; the engine refuses a mismatch. Paper cash lives in `paper_accounts`; live balances are never used for paper.
* **LIVE order gate** (`SafetyController.live_gate`), re-evaluated for every order: live switch ENABLED; trading state SYSTEM_HEALTHY (no kill switch, no reconciliation required, risk healthy); exchange row is LIVE, enabled, verified, key read+trade, no withdraw, credential without withdrawal permission; a clean reconciliation younger than 10 minutes; strategy version RELEASED and enabled (experimental strategies are stored as DRAFT and can never pass). Anything that cannot be read blocks.
* Fresh install: live DISABLED. After a restart: ENABLED becomes BLOCKED unless full startup verification passed (the API process always blocks it until a person re-enables).
* Futures/perpetuals are modelled (`market_type`) but rejected as NOT_IMPLEMENTED.

## Stop-loss and take-profit exits: paper behaviour versus real-market guarantees

What the system does today (paper) and what it does NOT promise for a real market.

**How an exit works.** The exit monitor reads the quote each cycle and evaluates the triggers on the **bid** (stop: bid <= stop; take-profit: bid >= target; optional time stop). A triggered exit is a SELL sent through the Execution Engine only after the Risk Engine approves it as an exit of an existing long position.

**Entry spread versus exit spread, and stop-loss versus take-profit.** Entries are refused when the spread exceeds the normal limit (`DEWAKO_MAX_SPREAD_PCT`). Exits are split by urgency:
- **Stop-loss: exits at once.** When the bid reaches the stop the exit is sent at market through any spread a real market can show, with no tolerance setting involved. One safeguard: a spread above `DEWAKO_STOP_SUSPECT_SPREAD_PCT` (default 10 %) is far more likely a bad quote than a market, so it must persist for `DEWAKO_STOP_SUSPECT_CONFIRM_CYCLES` consecutive monitor cycles (default 3, i.e. two held cycles, about 10 s at the default 5 s interval) before the stop is sent. During the hold the system says so loudly: CRITICAL incident, new entries blocked, worker unhealthy. `off` removes the safeguard; confirm cycles of 1 removes the wait.
- **Take-profit and time stop: wait for a fair spread.** They exit normally within the normal limit, may use the explicit tolerance `DEWAKO_EMERGENCY_EXIT_MAX_SPREAD_PCT` (unset by default), and otherwise wait. Waiting is audited (`EXIT_WAITING_WIDE_SPREAD`, once per streak) and reported, but it is NOT an incident: the worker stays healthy and new entries are not blocked, because the position's stop-loss is still enforced every cycle (and still exits at once if price reverses).

| Trigger and condition | Result |
|---|---|
| Any trigger, spread within the normal limit | Exit sent normally |
| STOP, spread above the normal limit and up to the suspect limit | Exit sent at once; `EMERGENCY_EXIT_WIDE_SPREAD` audited, policy `STOP_LOSS_IMMEDIATE` |
| STOP, spread above the suspect limit, first cycles | **Held**: no order, position stays OPEN, CRITICAL incident `EXIT_BLOCKED_WIDE_SPREAD`, `EXIT_STOP_QUOTE_SUSPECT` audited once, entries blocked, retried every cycle |
| STOP, suspect spread persists for the confirmation cycles | Exit sent at the quoted bid; audited, policy `STOP_LOSS_CONFIRMED_WIDE` |
| STOP, the quote returns to normal before confirmation | Exit sent at the normal quote; the glitch price is never used |
| TAKE_PROFIT / MAX_HOLD, spread above the normal limit and within the tolerance | Exit sent; `EMERGENCY_EXIT_WIDE_SPREAD` audited |
| TAKE_PROFIT / MAX_HOLD, spread above the tolerance, or no tolerance configured | **Waits**; position stays OPEN; `EXIT_WAITING_WIDE_SPREAD` audited; no incident, entries not blocked |
| Any trigger, stale, crossed, zero, negative, non-finite or missing quote | No order, no guessed price; `EXIT_PROTECTION_UNVERIFIED` incident; new entries blocked |

An incident clears only after the position has actually closed or protection is verified again. A position is never reported closed before the exit order has filled, and the recorded fill price is the quoted price, never the stop or target price.

**What paper trading does NOT show you (real-market differences).**
- A stop is not a guaranteed price. In a gap or a thin book the real fill can be far worse than the stop; the paper fill is the quoted bid plus the configured slippage, which is a model, not a measurement. A stop-loss sells through any spread up to the suspect limit, so in a thin or glitching market it can fill far from the stop, and a single bad bid tick can trigger it: the trigger is the bid, and the spread check no longer filters a bogus quote for stops. A deferred take-profit can wait while price keeps moving (the gain can shrink).
- Paper fills are immediate and complete. A real exchange can fill partially, reject, rate-limit or be unreachable at the moment a stop triggers. Partial-exit tracking is Stage 2 WP3 and is not yet implemented.
- Paper quotes are simulated or public-feed quotes. Latency, queue position, order-book depth and exchange outages are not modelled.
- Nothing here is evidence of live readiness. Live trading remains locked.
