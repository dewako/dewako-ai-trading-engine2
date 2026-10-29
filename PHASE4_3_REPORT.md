# Phase 4.3 report: AI provider manager

**Live trading: LOCKED. Phase 3: INCOMPLETE. No exchange verified. No order sent. No real model or cloud service was contacted.**

## Summary
Provider-neutral interface (prompt string in, response string out: no tools), Ollama / LM Studio / generic OpenAI-compatible adapters, modes LOCAL_ONLY (default) / CLOUD_ONLY / HYBRID with explicit cloud-fallback opt-in, classification + redaction, strict versioned output schemas, prompt hygiene, circuit breaker, rate limit, bounded sizes, persisted settings, vault-encrypted cloud key, 4 guarded routes.

## Files
New: `ai/{errors,redaction,prompts,schemas,breaker,providers,manager,settings}.py`, `tests/test_ai_providers.py` (22 tests), `docs/AI_PROVIDERS.md`. Edited: `persistence/migrations.py` (migration 7: `ai_settings`, `ai_credentials`), `api/main.py` (routes `/ai/settings` GET/PUT, `/ai/providers/{name}/credential` PUT, `/ai/commentary` POST), `tests/test_config_and_migrations.py` (the 5→later upgrade test now also expects the AI tables).

## Data flow
route → settings store → manager.build (LOCAL_ONLY constructs no cloud provider) → classify/redact per destination (cloud + private data = refused, no call) → fenced prompt → provider (breaker, rate limit, bounded retry on timeouts only) → strict schema validation → `AIResult(advisory=True)`. Audit row records provider, outcome, fallback flag: never prompt or response text. Nothing downstream consumes a suggestion as an instruction.

## Security implications
Structural isolation tested in both directions (AI imports no acting module; no acting module imports AI). Engine executes and kill switch still blocks while every AI provider is down. Keys: vault-encrypted, last 4 only on reads, absent from audit (tested), sent only as a header. Local endpoints restricted to loopback/private literals. Mutation checks (local-only list, cloud privacy check, schema `extra=forbid`, fallback opt-in each disabled in turn) each made tests fail, then restored.

## Results (observed, Linux, Python 3.13, custom runner)
`ai_providers` 22/22. SQLite full: 320 passed / 0 failed / 25 skipped. PostgreSQL 16 full: 323 passed / 0 failed / 22 skipped (skips: 22 HTTP tests need FastAPI; 3 PostgreSQL-only tests skip on SQLite). Logs: `docs/evidence/phase4_3_*.log`. Real pytest, HTTP tests, CI, Docker: NOT RUN / BLOCKED as before.

## Limitations
- Adapters never exercised against a real Ollama/LM Studio/cloud server. Request shapes are from convention, not from a live check.
- Classification is key-name/pattern based: it can miss a secret or private value in an unusual shape or inside free text.
- Rate limiter and breaker state are per process and in memory. No streaming. No hybrid cost controls.
- `/ai/commentary` is the only consumer; no strategy-selection or backtest integration (deliberately: nothing may act on AI output yet).
- Routes tested only by source-level permission checks, not over HTTP.

## Run / rollback
`cd backend/tests && python3 ../../scripts/run_tests_standalone.py ai_providers`. Rollback: `PUT /ai/settings` with `LOCAL_ONLY` and no providers, or remove the routes/package and migration 7 (unreferenced elsewhere).
