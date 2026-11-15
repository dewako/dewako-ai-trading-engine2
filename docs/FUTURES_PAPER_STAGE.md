# Futures (paper) — stage plan and status

Owner request, 2026-11-15: "I need futures trading too. Build a parallel system with leverage selection and cross and isolated mode."

**Status: F1 built (accounting core, paper only). Not connected to the engine, the API, the dashboard or any exchange.**

## Rules that still apply
* Paper only. Never submit real orders; live trading stays LOCKED.
* No testnet or live adapter. The owner's rule "no testnet/live/broker adapters" is still in force; F4 below cannot start until
  the owner lifts it in writing.
* The Risk Engine stays the only authority for risk and sizing, the Execution Engine the only authority for execution.
* The spot system is not changed. `dewako/futures/` is a separate package; a test fails if any spot module imports it or if it
  gains network code. The running spot paper test (commit e249b5a on the owner's laptop) is unaffected.
* The 0.1% fee per side is charged by default (Binance futures taker is 0.05%; 0.1% is the owner's rule and is conservative).

## What F1 is: `backend/dewako/futures/paper.py`
| Feature | Behaviour |
|---|---|
| Direction | Long and short, one net position per symbol (one-way mode) |
| Leverage | Selectable per symbol, a whole number from 1 to the cap. Cap: `DEWAKO_FUTURES_MAX_LEVERAGE`, default **3**; no setting can exceed **20**. An invalid value refuses to start |
| Margin mode | **ISOLATED** (default) or **CROSS**, per symbol |
| Fills | Market, at mark ± 0.05% slippage, fee 0.1% of the order value |
| Funding | `apply_funding(symbol, rate)`: positive rate, longs pay and shorts receive |
| Liquidation | Checked at every price change, funding payment and entry |
| Reduce-only | A reduce-only order can only shrink a position; it is capped at the position size |
| Entry rules | `size_for_risk` (size from the stop distance, never from leverage) and `entry_guard` (liquidation must be at least 2x further away than the stop) |
| Audit | Every change to the account's money is a ledger entry; `check()` proves the money adds up |

### ISOLATED and CROSS in this model
* **ISOLATED:** each position holds its own margin. The most it can lose is that margin, even if the price gaps through the
  liquidation price. The rest of the account is untouched.
* **CROSS:** positions share the whole wallet. A profit on one symbol holds up a loss on another, and liquidation is further
  away. When it does come, **every cross position is closed and the whole cross wallet is lost.** Isolated positions and their
  margin survive.

### Simplifications (all on the conservative side, all to be revisited before testnet)
1. A liquidated isolated position loses its whole margin; a cross liquidation takes the whole wallet. A real exchange liquidates
   in steps and may return a remainder.
2. Maintenance margin: one rate for the whole position, by position value (0.5% to 50k, 1% to 250k, 2.5% to 1M, 5% above). Real
   exchanges use a different table per symbol, and also lower the maximum leverage as the position grows.
3. Positions never flip: an order larger than the position is refused unless it is reduce-only.
4. Leverage and margin mode can be changed only while the symbol has no open position.
5. Unrealized profit cannot be used as margin for new positions.
6. Fills are instant and complete at the mark price ± slippage: no order book, no partial fills, no latency. Liquidation uses the
   same mark price as fills (no separate last price).
7. Hedge mode (long and short on one symbol at once), limit orders, and stop orders resting on the exchange are not modelled.

## Tests
`backend/tests/test_futures_paper.py`: 28 tests, each expected number worked out by hand in the test. Includes 40 random
sessions of 300 actions that check after every action that money is conserved, the wallet is never negative, and no isolated
position stays open below maintenance margin. Mutation check: 22 deliberate faults in `paper.py`, all 22 caught.

## What is NOT built yet
| Stage | Content | Blocked by |
|---|---|---|
| F2 | Research: test long + short strategies on futures costs at 1x, with 2x and 3x reported. Pre-registered protocol, same gate | nothing |
| F3 | Engine wiring, paper only: futures positions and orders in the database, Risk Engine rules (leverage cap, liquidation-to-stop distance, isolated by default), Execution Engine path, funding and mark-price feed, API and dashboard controls for leverage and margin mode, crash and restart tests | owner approval of the F3 brief (it changes approved Stage 3 requirements) |
| F4 | Binance futures testnet adapter | the owner lifting the adapter rule; F3 passing its paper qualification |
| F5 | Live | everything above, plus a strategy that passes the gate; LOCKED |

## Owner decisions recorded here
* 2026-11-15, owner: "Yes 3x and isolate are default configurations." Leverage cap default **3** (the hard ceiling of 20 stays),
  margin mode default **ISOLATED**.
* 2026-11-15, owner: F2 next.

## F2 result (2026-11-15)
`research/futures_trend_f2_2026-11-15/PROTOCOL.md`: 55-day breakout with a 3 ATR trailing stop, long + short, daily, BTC + ETH + SOL,
through this paper core. **FAIL**: 5 of 6 checks passed (+5,709 on 10,000 at 1x, 11.1% drawdown, positive at double costs and in
every year), but the out-of-sample edge is 37% of the in-sample edge (the limit is 50%). Not retuned.

## Intraday and unseen-coin results (2026-11-15)
* `research/futures_intraday_2026-11-15/`: four standard rules on 5m and 15m, long + short: **all 8 cells FAIL** at every cost. The
  rules earn -0.03% to +0.02% of the position per trade before costs against a 0.30% round trip. **Owner decision: short timeframes
  are closed** ("leave short time frames").
* `research/futures_trend_unseen_2026-11-15/`: the daily trend rule, unchanged, on BNB, XRP, ADA, AVAX, LINK: **FAIL** on drawdown
  (17.5%) and out-of-sample ratio (34%), while +16,224 on 10,000, positive at double costs and on 4 of 5 coins. The profit generalised;
  the fading edge and the drawdown did not pass. Shorts contribute nothing in either group.
* F3 (engine wiring) stays blocked: no strategy has passed its pre-registered test. The next candidate is a long-only version of the
  trend rule with a drawdown-aware size, as a new pre-registered test.
