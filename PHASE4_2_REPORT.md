# Phase 4.2 report: strategy ZIP package manager

**Live trading: LOCKED. Phase 3: INCOMPLETE. No exchange is verified. No order of any kind was sent.**

## Step 1 (Phase 4.1 re-verification), observed
- Real pytest: **BLOCKED**. The sandbox egress proxy refuses pypi.org (403, policy), so pytest/FastAPI/httpx cannot be installed. I did not work around it.
- HTTP tests (22): **NOT RUN** (skipped: FastAPI absent). CI workflow: **NOT RUN**. Docker: **NOT RUN** (no daemon). Windows: **NOT VERIFIED**.
- Custom runner, SQLite: 298 passed / 0 failed / 25 skipped. PostgreSQL 16: 301 passed / 0 failed / 22 skipped. Skips: 22 HTTP tests need FastAPI; 3 crash tests are PostgreSQL-only (skip on SQLite by design).
- Verified by tests: capability checks run before any adapter call (counting adapter sees zero calls), an ambiguous `create_order` outcome goes UNKNOWN and reconciles (never resent; later orders blocked with RECONCILIATION_REQUIRED), live lock intact.
- Correction: my Phase 4.1 report wrongly said "0 skipped"; fixed in `PHASE4_1_REPORT.md`.
- Not a Git repository here, so there is no diff to inspect; each stage ZIP is the checkpoint.

## Summary
Packages are data: manifest + declarative `RuleSpec` + parameters + metadata (+ inert test vectors). The archive is read in memory with bounded reads; nothing is extracted or executed. Imported strategies are registered only at BACKTEST_ELIGIBLE or higher, are always `experimental` (startup stores them as DRAFT, never RELEASED), so the live gate refuses them.

## Files
New: `strategy_packages/{__init__,schema,archive,manager}.py`, `tests/test_strategy_packages.py` (22 tests), `scripts/make_strategy_package.py`, `docs/STRATEGY_PACKAGES.md`. Edited: `persistence/migrations.py` (migration 6, `strategy_packages` table), `runtime/application.py` (REQUIRED_TABLES; startup step `strategy_packages`, non-critical), `api/runtime_state.py`, `api/main.py` (4 routes), `tests/test_config_and_migrations.py`, `tests/test_lifecycle_failure.py`.

## Test changes disclosed (not weakening)
- `test_upgrade_from_previous_schema...`: it assumed the previous release was the one before the latest migration (schema 5 → "worker_leases absent"). I pinned its "previous release" to schema 4 so it still tests the audit-hash-chain upgrade with a legacy row, expects `strategy_packages` after upgrade, and added `test_upgrade_from_schema_5_adds_strategy_packages_and_keeps_data`.
- `test_startup_runs_steps_in_documented_order...`: expected step list gains `strategy_packages`.
- Defect found and fixed by my own tests: the archive reader rejected ZIPs written with permission bits but no file-type bits (common) as "special files"; it now judges only the type bits.

## Data flow
ZIP bytes → `open_package` (bounded, in memory) → manifest schema → hash/file-list match → RuleSpec → fingerprint → row IMPORTED → VALIDATED. Human promotions → registry. Signals: `SignalEngine` → risk → authorization → engine, unchanged.

## Security implications
Package cannot import code, read credentials, reach adapters or the network (static test: the package module imports no exchange/execution/risk/safety/vault/api modules and contains no eval/exec/subprocess). `python`, `pine`, `wasm` runtimes are refused with reasons. Mutation checks (hash check, symlink check, experimental flag each disabled in turn) made tests fail, then restored.

## Results (observed, Linux, Python 3.13, custom runner)
`strategy_packages` 22/22. SQLite full 298 pass / 0 fail / 25 skip. PostgreSQL 16 full 301 pass / 0 fail / 22 skip. Logs: `docs/evidence/phase4_2_*.log`.

## Limitations
- The 4 routes are covered only by direct manager tests and a source-level permission check; **no HTTP test has run**.
- No Python/Pine runtime by design. Rule coverage is limited to the 7 whitelisted conditions (no XAUUSD-specific features such as spread filters or session windows yet).
- TESTNET_ELIGIBLE is a label only. No package-to-bot assignment UI. Packages are not stored as files (only the validated spec + hashes), so the original ZIP cannot be re-downloaded.

## Run / rollback
`cd backend/tests && python3 ../../scripts/run_tests_standalone.py strategy_packages` (or pytest). Rollback: quarantine/disable packages, or remove the routes, the startup step and migration 6 (table is unreferenced elsewhere).
