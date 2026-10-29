# Phase 5.2 Independent Review — Consolidation Notes

Date: 2026-10-29

## Scope
Review of the supplied Phase 5.2 ZIP, focused on the Phase 5.1 acceptance findings:
SELL send-time exchange-rule validation, conservative executable-price accounting,
audit-event behavior, and the Chrome prototype launch path.

## Independently executed checks
- `PYTHONPATH=. python -m pytest -q tests/test_executable_price_accounting.py`: **22 passed**.
- `PYTHONPATH=. python -m pytest -q tests/test_market_execution_guards.py tests/test_risk_reservations.py tests/test_lifecycle_failure.py tests/test_live_activation.py`: **60 passed**.
- `node --test frontend/tests/*.test.js`: **9 passed**.
- `python -m compileall -q backend/dewako backend/tests scripts`: **passed**.
- `python scripts/scan_secrets.py`: **0 findings**.

## Environment limitations
- A full `python -m pytest -q` run exceeded the 180-second execution limit in this review environment. This is recorded as **NOT REPRODUCED / TIME LIMIT**, not as a test failure or a pass.
- `frontend/browser_tests/test_dashboard_browser.py` could not launch Chromium because the Playwright Chromium executable is not installed in this environment. Real-app browser verification is therefore **BLOCKED here**.
- Ruff was not available as a command in this review environment. The package contains a prior Ruff evidence log; this review did not independently reproduce it.
- No production exchange endpoints, real credentials, testnet orders, or live orders were used.

## Code findings
- SELL send-time revalidation is present in `backend/dewako/execution/engine.py`: current quote is validated, then SELL exchange rules are checked against a conservative price basis before `SUBMIT_ATTEMPT`.
- The targeted tests cover a SELL order that passes initial validation but falls below minimum notional before submission; it is blocked without `SUBMIT_ATTEMPT` or retry.
- The earlier duplicate `ORDER_SUBMITTED` audit event was not observed in the reviewed submission path.
- Quote-based/max-quantity sizing remains explicitly **NOT IMPLEMENTED / OPEN**. Current behavior is a safe block if the conservative executable-price check breaches a cap. This limitation is retained in `PHASE5_READINESS_MATRIX.md`.
- The HTML prototype has the HTML5 doctype, `lang`, and viewport declaration. This package adds `run_prototype.bat` and `scripts/run_prototype.py` plus launch instructions to make the backend-served Chrome path easier to start.

## Readiness
- Paper/simulation development: **READY WITH TRACKED LIMITATIONS**.
- Testnet: **NOT READY**.
- Live trading: **NOT READY — KEEP LOCKED**.
- This review does not claim venue-level execution, exchange-side idempotency, or real-browser integration has been verified.
