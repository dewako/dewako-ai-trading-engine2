# Strategy packages (Phase 4.2)

A package is a ZIP of **data**. Importing one never runs code, never writes the archive's contents to disk, and gives the package no access to credentials, adapters, the execution engine or the network. A package can only become a declarative rule strategy (the same `RuleSpec` the no-code builder uses). Its signals travel the normal route: signal engine → risk engine → execution authorization → execution engine → audit.

## Layout
```
manifest.json          required   schema_version 1; name, version, runtime, instruments, timeframes, files{path: sha256}
strategy/rules.json    required   a RuleSpec (entry_long / entry_short conditions, atr_len, atr_stop, take_profit_rr)
parameters.json        required   overrides; only atr_len, atr_stop, take_profit_rr
metadata.json          required   up to 20 string values (<= 500 chars)
tests/<name>.json      optional   data vectors; parsed for validity, never executed
README.md              optional
```
Manifest rules: unknown fields are an error (`install_script`, `hooks`, ... are not ignored). `runtime` must be `declarative-rules`. `dependencies` and `requested_capabilities` must be empty (the dependency allowlist is empty; network, credentials and filesystem access are never granted). `files` must list every other file with its SHA-256, exactly.

Build one: `python scripts/make_strategy_package.py <folder> out.zip --name "Gold Scalp" --version 1.0 --instruments XAUUSD --timeframes 1m,5m`

## What is refused (each has a test in `tests/test_strategy_packages.py`)
Not a ZIP / empty / over 1 MiB; more than 40 entries; any member over 256 KiB or total over 1 MiB; compression ratio over 100:1; headers that lie about size; encrypted entries; symlinks and special files; absolute, `..`, backslash, drive, hidden, empty-segment or non-ASCII paths; duplicate (case-insensitively) paths; any `.py/.sh/.exe/.dll/.js/...`, `setup.py`, `requirements.txt`, `package.json`, `Makefile`; unlisted entries; missing required files; malformed, duplicate-key, NaN or deeply nested JSON; manifest schema errors; runtimes `python`, `pine`, `wasm` or unknown; hash mismatch and file-list mismatch; rules that fail `RuleSpec`; parameter keys outside the allowlist; name/version mismatch between manifest and rules; a name equal to a built-in strategy; a (name, version) that already exists with different bytes (versions are immutable); byte-identical re-imports.

## Lifecycle
`IMPORTED → VALIDATED → BACKTEST_ELIGIBLE → PAPER_ELIGIBLE → TESTNET_ELIGIBLE`, plus `VALIDATION_FAILED`, `DISABLED`, `QUARANTINED`. Promotion is one step at a time and by a human with the right permission (backtests: `backtests`; paper/testnet/disable: `trade_actions`; manual quarantine: `kill_switch_trigger`; leaving quarantine: ADMIN). Demotion is allowed. A disabled package must be re-validated. A quarantined package is released to `DISABLED` by an admin, then re-validated.
* The strategy is registered in the strategy registry only while `BACKTEST_ELIGIBLE` or higher.
* The fingerprint (same function as every strategy) is stored at import and re-checked at every promotion and at every start. A mismatch quarantines the package and unregisters it.
* `TESTNET_ELIGIBLE` is a **label**. This build cannot place testnet orders (see `EXCHANGE_ADAPTERS.md`).
* Imported strategies are `experimental`; startup records them as `DRAFT`, never `RELEASED`, so the live gate refuses them. Nothing in the import path writes a `RELEASED` row.
* Switching a bot between strategies still goes through `StrategySwitcher` (open/unknown positions need an explicit policy).

## API (authenticated; `strategies` permission minimum; the manager re-checks per transition)
`POST /strategy-packages` `{archive_base64}` · `GET /strategy-packages` · `GET /strategy-packages/{id}` · `POST /strategy-packages/{id}/transition` `{to, reason}`. **These routes have been exercised only by direct manager tests and a source-level check: no HTTP test has been run (FastAPI is not installable in the build environment).**

## Why no Python (or Pine) runtime
A Python subprocess is not a sandbox: it can read the credential vault's files, the environment and the network. Until strong isolation exists (separate unprivileged container/VM, no network, no secrets, resource limits, reviewed), `python` packages are refused. Pine Script does not run natively here.

### Future "Dewako Algo Studio" contract (not implemented)
A future runtime would receive normalized `MarketState` (read-only) and return `Signal` proposals only; no I/O, no clocks, no randomness; bounded CPU and memory; deterministic replay; the same manifest hashes; its signals pass through exactly the pipeline above. Converting Pine to the declarative spec would be a translation step with an explicit unsupported-feature report, never "native Pine".

## Rollback
Disable or quarantine a package (`DISABLED`/`QUARANTINED`) to unregister it immediately. Removing the feature: drop the `/strategy-packages` routes and the `strategy_packages` table (migration 6); no other table references it.


## Dashboard import workflow (Phase 4.4)
Open the dashboard (`/ui`), sign in, open **Strategy Manager**, press **Import Algo ZIP** (or drop a .zip). The browser sends base64 JSON to `POST /strategy-packages` using XHR (real upload progress, cancellable).
The page shows a result **only after the server answers**: accepted (name, version, server-computed SHA-256 and fingerprint, lifecycle state) or rejected (code and bounded reason). Client checks (size ≤ 1 MiB) are advisory; the server re-validates everything.
Importing never enables, selects or trades a package; it lands as `VALIDATED` at most, and promotion is a separate permissioned step. Cancelling an upload does not prove nothing was imported; the page says so and refreshes the list.
Every import, rejection, quarantine and transition appears in Live Activity (category `strategy_package`, same correlation id) with name/version/hash and no file contents.
Lifecycle labels shown are exactly the backend states; there is no "Released" state for packages.
