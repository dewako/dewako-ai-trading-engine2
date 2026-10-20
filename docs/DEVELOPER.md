# Developer guide

## Create a strategy

```python
from dewako.strategies.base import Strategy, register
from dewako.core.types import MarketState, Signal, Side

@register
class MyStrategy(Strategy):
    name = "My Strategy"
    version = "1.0"                 # versions are immutable: change behaviour => new version
    default_parameters = {"length": 20}

    def generate_signal(self, state: MarketState):
        # Pure function of state + self.parameters. No exchange calls, no I/O.
        ...
        return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.7, strategy=self.name,
                      strategy_version=self.version, reason=["why"], entry_price=px, stop_price=stop)
```

Always set `stop_price`. The Risk Engine rejects entries without one. Registering the same name and version twice raises.

## Create an exchange adapter

Subclass `ExchangeAdapter` (`exchanges/base.py`). Translate to and from exchange formats inside the adapter only. Use `core/symbols.py` for symbols. On a timeout after submitting an order, raise `OrderOutcomeUnknown`. Support lookup by `client_order_id` in `get_order` so the order engine can reconcile.

## Create a data provider

Planned interface (Phase 2): `MarketDataProvider` returning normalized OHLCV, trades, order books, funding, open interest and liquidations, each wrapped with `DataQuality` (source, data timestamp, publication timestamp, revision).

## Run a backtest

```python
from dewako.backtesting.engine import BacktestConfig, run
result = run(TrendAI(), candles, BacktestConfig(symbol="BTC/USDT", timeframe="15m"))
result.record    # strategy, version, parameters, config, data period, run_id
result.metrics   # net profit, ROI, win rate, profit factor, drawdown, ...
```

Never judge a strategy on one run. Use `overfitting_warnings` and walk-forward windows.

## Add an exchange manually (API)

```bash
curl -X POST http://localhost:8000/exchanges \
  -H "Authorization: Bearer $DEWAKO_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"exchange":"binance","label":"Main","api_key":"...","api_secret":"..."}'
```

The server reads the key's permissions from the exchange. A key with withdrawal enabled, or whose permissions cannot be read, is rejected and nothing is stored. Secrets are encrypted with `ENCRYPTION_KEY` and the API only ever returns the last 4 characters of the key.

Today only Binance can verify a key (`exchanges/binance.py`, not yet tested against the live exchange). Bybit and OKX are stubs, so adding them is rejected until their adapters implement `get_api_permissions()`.

## Add a strategy manually

No-code (safe, no code runs):

```bash
curl -X POST http://localhost:8000/strategies \
  -H "Authorization: Bearer $DEWAKO_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"RSI Dip","version":"1.0","entry_long":[{"type":"rsi_below","length":14,"value":30},{"type":"price_above_ema","length":200}],"atr_stop":2}'
```

Allowed conditions: `price_above_ema`, `price_below_ema`, `ema_fast_above_slow`, `ema_fast_below_slow`, `rsi_below`, `rsi_above`, `volume_ratio_above`. Name and version together are immutable.

Code plugin: set `DEWAKO_ALLOW_CODE_PLUGINS=1`, drop a file in `plugins/strategies/`, then `POST /strategies/plugins/reload`. Only on a machine you control.

Note: exchanges and rule strategies are held in memory in Phase 1 and are lost on restart until the database layer exists.

## Market data (Phase 2)

```
Provider (Binance / Bybit / OKX public APIs) -> normalized CandleRecord + DataQuality
  -> CandleStore (SQLite, point in time) -> build_market_state() -> MarketState -> strategies / AI
```

- `market_data/provider.py`: the `MarketDataProvider` contract. Providers return closed candles only, oldest first, `end` exclusive.
- Publication time: a candle is known when it closes. `CandleStore.get_candles(..., as_of=T)` returns only candles closed by T, and for corrected candles only the version we had ingested by T. Use `as_of` in every backtest.
- `backfill()` pages through a range, backs off on rate limits and can be re-run safely.
- `MarketDataHub` fails over between providers and always reports which source answered. `divergences()` flags candles where two venues disagree by more than a tolerance.
- `build_market_state()` is a pure, deterministic rule set (EMA 20/50 trend, momentum vs ATR, volatility vs its own history, volume ratio). It is not a trained model.

```bash
curl "http://localhost:8000/markets/candles?symbol=BTC/USDT&timeframe=15m&limit=100"
curl "http://localhost:8000/markets/state?symbol=BTC/USDT&timeframe=1h"
curl -X POST http://localhost:8000/markets/backfill -H "Authorization: Bearer $DEWAKO_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USDT","timeframe":"1h","provider":"binance","start":"2026-01-01T00:00:00Z","end":"2026-03-01T00:00:00Z"}'
curl "http://localhost:8000/markets/stored/candles?symbol=BTC/USDT&timeframe=1h&source=binance&start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&as_of=2026-01-01T12:00:00Z"
```

Exchange candle history is free but is not a substitute for a paid point-in-time provider (Kaiko, Coin Metrics) when you need revisions, news or macro data. Add one by implementing `MarketDataProvider`.

## Phase 3: strategy engine

- Strategies live in `backend/dewako/strategies/`; import `library` to register them all.
- A released `(name, version)` must never change. `StrategyVersionLock` stores a fingerprint in `DEWAKO_STRATEGY_LOCK` and the API refuses to start if it differs. Change behaviour by shipping a new version.
- `SignalEngine` validates signals (symbol, stop/target direction, finite prices, reasons) and gives deterministic ids. A crashing strategy yields no signal.
- `StrategySwitcher` requires an explicit policy (KEEP / CLOSE / PREVIOUS_MANAGES) when a position is open or unknown. The API has no position source yet, so a second switch of the same bot always requires a policy.
- Routes: `GET /strategies/{name}/versions`, `GET /strategies/{name}/signal-preview` (read-only), `GET /strategies/recommendation`, `POST /strategies/switch` (admin token, paper only).
- Not in Phase 3: connecting signals to the Risk Engine in a running loop, walk-forward backtests, and the Phase 2 hardening list (persistence, order state machine, auth/RBAC).

## Hardening phase
Run the tests with `pytest backend/tests` (needs `pip install -r backend/requirements.txt`). Start the API with `DEWAKO_BOOTSTRAP_TOKEN`, `ENCRYPTION_KEYS`, then create the first admin via `POST /auth/bootstrap`. See `docs/` for the state machine, reconciliation, auth, credentials and readiness checklist.

## Phase 3: strategy engine

- Strategies live in `backend/dewako/strategies/`; import `library` to register them all.
- A released `(name, version)` must never change. `StrategyVersionLock` stores a fingerprint in `DEWAKO_STRATEGY_LOCK` and the API refuses to start if it differs. Change behaviour by shipping a new version.
- `SignalEngine` validates signals (symbol, stop/target direction, finite prices, reasons) and gives deterministic ids. A crashing strategy yields no signal.
- `StrategySwitcher` requires an explicit policy (KEEP / CLOSE / PREVIOUS_MANAGES) when a position is open or unknown. The API has no position source yet, so a second switch of the same bot always requires a policy.
- Routes: `GET /strategies/{name}/versions`, `GET /strategies/{name}/signal-preview` (read-only), `GET /strategies/recommendation`, `POST /strategies/switch` (admin token, paper only).
- Not in Phase 3: connecting signals to the Risk Engine in a running loop, walk-forward backtests, and the Phase 2 hardening list (persistence, order state machine, auth/RBAC).
