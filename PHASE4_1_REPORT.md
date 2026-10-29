# Phase 4.1 report: exchange-agnostic adapter framework

**Live trading: LOCKED. Phase 3: INCOMPLETE. No exchange is live- or testnet-verified. No real or testnet order was sent.**

## Summary
Added an explicit capability contract, a guarded adapter registry, normalized errors and engine-side capability enforcement. The engine is still the only submission route. No testnet adapter was written (no network access, no schema support): TESTNET execution is explicitly BLOCKED.

## Files
New: `exchanges/contract.py`, `tests/test_adapter_contract.py` (16 tests), `PHASE4_REPOSITORY_ASSESSMENT.md`. Rewritten: `exchanges/registry.py` (backward-compatible `REGISTRY`). Edited: `exchanges/manager.py` (shared `redact`), `execution/engine.py` (`_refusals`, `_send`), `tests/test_hardening_safety.py` (one assertion, see below), `docs/EXCHANGE_ADAPTERS.md`.

## Data flow
decision → risk-approved → `_refusals` (live lock, environment, capability contract) → persist SUBMITTING → kill-switch/lease recheck → `create_order` → ack | UNKNOWN → reconcile by client order id. No adapter call happens before `_refusals` passes.

## Security-relevant changes
1. Engine refuses adapters lacking `order_submission`, `instrument_metadata`, `account_snapshot`, or (non-PAPER) `lookup_by_client_id`; unknown environments; TESTNET.
2. **Defect fixed:** a generic `NotConnected` from `create_order` was treated as "not sent" (FAILED), which could allow a duplicate if the connection dropped after transmission. Now only `NotImplementedForExchange` is definitive; everything else goes UNKNOWN → reconcile, never resent (regression test included).
3. Registry cannot grant live/testnet verification; allowlists are empty.

## Test change disclosed
`test_stub_exchange_returns_not_implemented_never_success` previously asserted the message "NOT_IMPLEMENTED". The stub is now blocked earlier by the capability gate, so I changed the expected reason to "does not declare order_submission" and added an assertion that zero order rows exist. It still requires BLOCKED; the new check is stricter. Direct stub calls raising `NotImplementedForExchange` are covered in the new file.

## Results (observed, Linux, Python 3.13)
- SQLite full suite: **PASS** 275 passed, 0 failed, **25 skipped** (22 HTTP tests: FastAPI not installed; 3 PostgreSQL-only crash tests: not applicable on SQLite) (baseline 259). `docs/evidence/phase4_1_sqlite.log`
- PostgreSQL 16 full suite: **PASS** 278 passed, 0 failed, **22 skipped** (the HTTP tests; FastAPI not installed) (baseline 262). `docs/evidence/phase4_1_pg16.log`
- Real pytest: NOT RUN (custom runner + shim; `pytest.raises(match=)` unsupported by the shim). HTTP tests (FastAPI): NOT RUN. CI, Docker, Windows: NOT RUN / NOT VERIFIED.

## Prompt's required tests: coverage
Conformance, normalization, env mismatch, duplicate protection, kill switch, no-call-on-auth-failure, disconnection/recovery, unknown outcome: new file. Metadata/precision, lookup/reconciliation, cancellation/fills, startup with unresolved orders, no risk-increasing orders while reconciling: pre-existing suites (`test_crash_and_recon`, `test_adapters_and_fills`, `test_hardening_safety`), still passing. Timeout/malformed/rejection normalization at the engine level: pre-existing FakeLive modes.

## Limitations
- Remaining live blockers: no verified exchange adapter, no testnet adapter, no XAUUSD venue, no operator evidence.
- After an ambiguous submission is reconciled, trading remains RECONCILIATION_REQUIRED until cleared. Conservative by design; operators must clear it.
- Mock/fake adapters prove contract behavior only.

## Run / rollback
Run: `cd backend/tests && python3 ../../scripts/run_tests_standalone.py adapter_contract` (or `python -m pytest test_adapter_contract.py` with pytest installed). Rollback: restore the previous `registry.py`, `manager.py`, `engine.py`; delete `contract.py`; no schema change was made.


## Correction (added in Phase 4.2)
The first version of this report said "0 skipped". That was wrong: I read the runner's TOTAL line, which does not print skips. The per-module lines in the evidence logs show the skips above. The 22 HTTP tests have never executed in this environment.
