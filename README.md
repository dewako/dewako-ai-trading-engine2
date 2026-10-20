# DEWAKO AI Trading Engine

AI-Powered Multi-Exchange Algorithmic Trading Platform. Phase 1: UI prototype, architecture and tested core logic. No live trading, no real exchange connectivity.

## Status

| Part | State |
|---|---|
| `frontend/prototype/index.html` | Working interactive demo of all 15 pages on simulated data. Not the React app. |
| `backend/dewako` core | Working and tested: normalized types, symbol normalization, strategy plugin base + Trend AI demo, Risk Engine, order engine, paper exchange, deterministic backtester |
| Exchange adapters | Interfaces plus stubs that refuse to trade. Binance can read an API key's permissions (not yet run against the live exchange) |
| Add exchange / add strategy | Backend services and token-protected API routes, tested with fakes. Demo UI has working forms (browser memory only). Data is in memory until the DB layer exists |
| React + TypeScript + Tailwind frontend | Not started. The prototype is the design reference. |
| Market data (Phase 2) | Provider interface, Binance/Bybit/OKX public providers, point-in-time SQLite store, backfill, failover hub, MarketState builder, `/markets` API. Tested with fake responses only: not yet run against the live exchanges |
| Strategy engine (Phase 3) | Momentum AI 1.4, Mean Reversion 1.2, Breakout AI 1.8, Trend AI 2.1/2.2, AI Strategy 0.3 (experimental, never live); validated deterministic signals; version fingerprint lock; explicit position policy on switching; rule-based selector (Manual / AI-Assisted / Fully-Automated with guards). Rule-based, not trained. Tested on synthetic markets only, which shows the logic works, not that it makes money |
| Strategy engine (Phase 3) | Momentum AI 1.4, Mean Reversion 1.2, Breakout AI 1.8, Trend AI 2.1/2.2, AI Strategy 0.3 (experimental, never live); validated deterministic signals; version fingerprint lock; explicit position policy on switching; rule-based selector (Manual / AI-Assisted / Fully-Automated with guards). Rule-based, not trained. Tested on synthetic markets only, which shows the logic works, not that it makes money |
| Hardening (Phase 2 master prompt) | Durable orders/positions/signals/risk/kill switch, explicit order state machine, unknown-order recovery, reconciliation, users + roles, audit log, vault key versions, live gate. Phase 3.2 adds PostgreSQL 16 (verified with the suite), a real startup lifecycle, supervised workers, balance reconciliation and a real-SIGKILL crash test. **No live order adapter; real FastAPI HTTP tests have not been run. Readiness: NOT READY.** See `PHASE3_2_HARDENING_AUDIT.md`, `docs/LIVE_TRADING_READINESS.md`, `docs/TESTING.md`, `docs/BACKUP_RECOVERY.md` |
| PostgreSQL models, auth, WebSockets | Not started |
| Order book / funding / open interest | Binance only; not stored historically |
| Momentum, Mean Reversion, Breakout, AI strategies | Metadata in the prototype only |

## Architecture

```
Market data -> Market State -> Strategy / AI -> Signal -> Risk Engine -> Order Engine -> Exchange Adapter -> Exchange
```

Rules the code enforces:

- Signals never place orders. The order engine only accepts an approved `RiskDecision` (`execution/order_engine.py`).
- Risk limits are immutable. `ai`, `strategy` and `bot` actors cannot change them or release the emergency stop (`risk/engine.py`).
- A stop loss is mandatory on every entry.
- A timeout after sending an order is reconciled with the exchange, never resent. Unresolvable orders become `UNKNOWN`.
- The backtester gives a strategy only the data up to the current bar and fills at the next bar open. Every run records its assumptions and a reproducible run id.
- Strategies only see `BASE/QUOTE` symbols. Exchange formats live behind adapters.

## Run

```bash
cd backend
pip install -e ".[dev]"
pytest
uvicorn dewako.api.main:app --reload     # http://localhost:8000/health
```

Prototype: open `frontend/prototype/index.html` in a browser, or `docker compose up prototype` and visit http://localhost:8080.

Full stack: `cp .env.example .env`, set `ENCRYPTION_KEY`, then `docker compose up`.

## Layout

```
backend/dewako/   core, strategies, exchanges, risk, execution, paper_trading, backtesting, api (+ empty module folders for later phases)
backend/tests/
frontend/prototype/
plugins/          strategies, exchanges, data-providers, ai-models, indicators
docs/DEVELOPER.md
```

## Before any real-money work

Phase 2 stores candles with publication time and revisions, so `as_of` queries cannot see the future. Exchange candles are only part of the picture: news, macro and on-chain history need a paid provider with real publication timestamps, otherwise backtests will look better than reality. Live trading stays out until paper results hold up across walk-forward windows.

Demo figures are simulated. Historical or simulated performance does not predict future results.


## Status (Phase 3.4)
PHASE 3 INCOMPLETE. Live trading is LOCKED. See `PHASE3_4_FINAL_HARDENING_AUDIT.md` and `docs/LIVE_TRADING_READINESS.md`. HTTP tests, CI and Docker have not been executed.
