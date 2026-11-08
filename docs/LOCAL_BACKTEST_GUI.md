# Local backtest GUI

A small web page that runs on YOUR computer and drives the same command-line tools as the CI (import, resample, backtest). Standard library only; it connects to no exchange, downloads nothing, holds no credentials, places no orders, never saves evidence records (every run is `--dry-run`) and listens on 127.0.0.1 only.

## Start
Needs Python 3.12+ and the repository (no pip packages for the GUI itself; the backend's own requirements must be installed as for the tests).

    cd backend
    python -m dewako.backtesting.gui --work-dir ../my_backtests

Your browser opens `http://127.0.0.1:8765/`. Relative file names in the page are relative to `--work-dir`. Ctrl+C stops it.

## Typical use with BTCUSDT / ETHUSDT 5m data from 2020
1. **Data**: give the store file name (e.g. `candles.db`) and the path of each Binance monthly archive `.zip`; press *Import zip* (one zip at a time; repeat per file or per symbol). A file that fails any check is refused whole, with the reason. Press *Show what is in the store* to see symbol / timeframe / source / date range.
2. **Resample**: for years of data use 1h or 4h. Six years of 5m is about 630,000 bars per symbol and a full 5m validation takes many hours. Resample 5m to 1h (source label becomes `binance_archive:resampled:5m`; put that label in section 3).
3. **Backtest**: pick strategy and version, symbol, timeframe, dates. Fee per side cannot go below 0.001. *Screen* = one replay, minutes, NOT evidence. *Full* = cost stress at 1x/2x/3x, hold-out, 4 periods, Monte Carlo and a PASS/FAIL verdict, simulated and not saved.
4. If the data check refuses a price spike (2020-03-12 and 2020-03-13 are likely), the page lists the times. Look at them; if they are real, press *Accept these spikes and re-run*. Nothing is accepted automatically.

Notes: the platform is spot long-only today. Short signals are not traded, so results for strategies that also short are not what TradingView shows. The risk engine halts permanently at 10% drawdown: a result ending near -10% is a halt, not a full-period result.

You can point *your own strategy file* at a `.py` that registers a strategy with `@register` (the same way `dewako/strategies/*.py` do). That file runs with your user's permissions: only use files you wrote or read.

## Same thing without the page
    python -m dewako.market_data.archive_import --zip BTCUSDT-5m-2024-10.zip --store candles.db
    python -m dewako.market_data.resample --store candles.db --symbol BTC/USDT --from 5m --to 1h --source binance_archive --start 2020-01-01 --end 2026-11-01
    python -m dewako.backtesting.cli --candles candles.db --symbol BTC/USDT --timeframe 1h --source binance_archive:resampled:5m --start 2020-01-01 --end 2026-11-01 --strategy "Trend AI" --version 2.3 --streak-reset daily --dry-run --screen
