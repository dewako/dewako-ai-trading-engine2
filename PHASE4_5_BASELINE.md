# Phase 4.5 baseline (recorded before any 4.5 change)
- Source revision: not a Git repository. Input = Phase 4.4 ZIP, SHA-256 `4b0f32fa0230f6e757a1715e47225cab150f3b53ffc894958283b7ac14ed0bfc` (`dewako_phase4_4.zip`).
- Environment: Linux sandbox, Python 3.13.16, no pytest/FastAPI/httpx, no Docker daemon, Chromium + Playwright present, local PostgreSQL 16.
- Package index: re-tried at the start of 4.5. Direct pip: DNS failure for pypi.org. Via the sandbox proxy: `403 Forbidden` tunnel (policy denial). Not bypassed.

| Area | Status at baseline |
|---|---|
| 4.1 adapter capability contract, 4.2 package import, 4.3 AI manager, 4.4 activity + dashboard pages | Implemented; verified by the custom runner on SQLite (344 pass/25 skip) and PG16 (347/22 skip) |
| FastAPI routes incl. all 4.4 routes | Implemented; **NOT VERIFIED over HTTP** (22 `test_api_testclient` tests skip) |
| Browser tests | Passed only against the stand-in server (not FastAPI) |
| CSP on `/ui` | Needs `unsafe-inline` (known gap) |
| CI workflow (`.github/workflows/ci.yml`), Docker, Windows | Written, **never executed** |
| Other dashboard pages | Simulated demo data |
| Phase 3 | INCOMPLETE; live LOCKED; no exchange verified |
Historical audits (`PHASE3_*_AUDIT.md`, Phase 4.1–4.4 reports) are preserved unmodified; new evidence is added in new documents or appended sections.
