# Phase 5.2 Stage 3A - Changelog

Paper research only. Live trading stays LOCKED. Branch `stage3a-dataset-layer` (from main `d650bb8`, which contains Stage 2 via PR #5, merged by the owner; main CI was green on that merge commit).

## 3A-1 Dataset layer (`dewako/backtesting/datasets.py`)
Built on the existing point-in-time `CandleStore` and backfill (no duplicate of either). A dataset is validated, hashed and frozen, or refused.

- **Refused (ERROR):** empty range; naive time; off-grid, out-of-range, duplicate or out-of-order candles; non-finite or non-positive price; non-finite or negative volume; high/low that do not bound open/close; any missing candle not explained by the caller (reported as runs with position and length); an "explained" gap that actually exists; a close-to-close jump or in-candle range above `spike_limit` (default 10 %) unless that exact candle is acknowledged.
- **Recorded (INFO):** explained gaps, zero-volume candles, candles using a revision published before `as_of`, candles with confidence below 1.0.
- **Identity:** `content_hash` = SHA-256 over symbol, timeframe, source and every value of every candle (floats as exact hex), order-independent. `Dataset.record()` is what a run record will store (hash, source, range, `as_of`, counts, explained gaps, acknowledged spikes, finding summary).
- **Point in time:** data is read as known at `as_of` (default: end of range). A correction published after `as_of` cannot change an old dataset; unclosed candles are excluded.
- **Provider-agnostic:** the source is a name in the store. Exchange adapters added in a later phase fill the same store; nothing here changes.
- **Tests:** `tests/test_stage3a_datasets.py`, 22 tests. Mutation checks, each fails at least one test (code restored, bytecode caching disabled): off-grid, duplicate, order, OHLC, non-positive price, volume, every-gap-explained, spikes never refused, acknowledgement ignored, close-to-close check, explained-but-present, hash identity / high / volume / open / time / input-order, `as_of` ignored, rejection removed. Two hash mutations (high, volume) first SURVIVED; `test_every_field_of_every_candle_is_part_of_the_hash` was added. One earlier mutation run was invalid (stale bytecode and a pipeline that killed the restore) and was rerun cleanly.
- **Findings while testing:** the store already refuses NaN (NOT NULL), so NaN is tested on raw lists; a spike produces two suspect candles (the jump and the jump back), and both must be acknowledged.
- **Limits:** the spike limit is a heuristic, 10 % may be wrong for a calm instrument or a volatile 1m crypto candle (adjustable per call); gap explanations are supplied by a human and are not verified against exchange calendars; real downloads from public providers were not run here (no network to exchanges); the validation does not detect plausible but wrong values.
- **Evidence (local):** SQLite 610 passed, 39 skipped (36 HTTP tests need fastapi, 3 PostgreSQL-only); PG16 613 passed, 36 skipped (the same HTTP tests); ruff real-error check clean. Hosted CI run 41938811121 on `a7578ea`: all jobs green.
