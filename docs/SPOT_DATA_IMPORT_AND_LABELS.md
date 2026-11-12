# Spot data import and strategy market labels (2026-11-06)

## Importer: `python -m dewako.market_data.archive_import --zip X --store candles.db --timeframe 5m`
Offline. Imports Binance SPOT monthly kline files as source `binance_archive`. Refuses a whole file on any bad row (non-numeric, non-positive
or non-finite price, high/low not containing open/close, off-grid or duplicate or out-of-order time, wrong close time, candle outside the month
in the file name, unit change inside the file). Refuses delivery-futures names. Accepts ms and µs timestamps. Verifies `.CHECKSUM` files when
the archive has them; otherwise the file is marked UNVERIFIED. Gaps and zero-volume candles are reported, never filled.

## Real files received (owner-supplied, not downloaded here)
| File | Result |
|---|---|
| BTCUSDT-5m, 2024-03 .. 2026-10 (32 months) | 280,800 rows, no gaps, no refusals, passes `build_dataset` for the whole range. No checksum file: UNVERIFIED. |
| ETHUSDT-5m, same range | 280,800 rows, no gaps, no refusals. `build_dataset` REJECTS 4 candles as SUSPECT_SPIKE (2024-08-05 01:05 and 01:10, 2025-02-03 02:05, 2025-10-10 21:55). They are continuous with their neighbours and carry heavy volume, but acknowledging them is a human decision (`--ack-spike`). |
| BTCUSD_YYMMDD-5m (COIN-M delivery futures, 53 files) | Not imported: separate expiring contracts, no rolling rule, padded zero-volume bars, one missing day (2026-10-28), contract-size volume. Futures is NOT_IMPLEMENTED in the platform. |

## Strategy labels (`strategies/labels.py`, `Strategy.market_support()`, `describe()`)
All five built-in strategies (six versions) emit both BUY and SELL entries (read from source; a test compares labels with the source).
Spot refuses SELL entries, so a spot backtest trades only the long half. FUTURES is NOT_IMPLEMENTED for every strategy. Rule-built strategies are
labelled from their spec. Labels sit outside the class source so no fingerprint changes (verified identical to HEAD).
Limit: the label says what a strategy CAN signal, not that it did on any data; a synthetic run triggered shorts for only 2 of 6 versions.
