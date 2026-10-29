# AI provider manager (Phase 4.3)

**What AI is allowed to do here:** produce an *advisory suggestion* (market commentary, a draft rule spec). **What it cannot do:** place or cancel orders, change risk limits, touch the kill switch or live switch, register a strategy, read credentials, or call tools. Enforced structurally: the `ai` package imports nothing from execution/risk/safety/exchanges/reconciliation/auth/api/runtime, and none of those import `ai` (tests check both). Every model output is parsed as JSON, validated against a strict versioned schema (unknown fields such as `order` are errors), and returned with `advisory: true`.

## Modes (`config.mode`)
| Mode | Behaviour |
|---|---|
| `LOCAL_ONLY` (default) | Only local providers are constructed or called. Zero cloud calls, even if a cloud provider is configured and the local one is down. |
| `CLOUD_ONLY` | Only cloud providers. |
| `HYBRID` | `hybrid_primary` (`local` default) first. Falling back **to the cloud** happens only if `allow_cloud_fallback: true`; falling back to local needs no opt-in. Fallback use is flagged in the result and the audit row. |

AI failure returns `status: UNAVAILABLE`; `ask` never raises, and trading never waits on it.

## Configure a local model (Ollama or LM Studio)
1. Run the model server on this machine (Ollama default `http://127.0.0.1:11434`; LM Studio local server default `http://127.0.0.1:1234`).
2. `PUT /ai/settings` (needs `risk_settings`):
```json
{"config": {"mode": "LOCAL_ONLY", "timeout": 30},
 "providers": [{"name": "ollama1", "type": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "llama3:8b"}]}
```
Use `"type": "lmstudio"` for LM Studio. A *local* endpoint must be `localhost` or a loopback/private IP literal (no DNS), so a mistyped public name cannot turn "local" into a cloud call.
3. Try it: `POST /ai/commentary` `{"symbol":"XAUUSD","timeframe":"1m","indicators":{"rsi":41.2}}`.

## Configure a cloud provider (generic OpenAI-compatible)
1. Set `ENCRYPTION_KEYS` (the existing credential vault).
2. `PUT /ai/settings` with `"mode": "CLOUD_ONLY"` (or `HYBRID` + `"allow_cloud_fallback": true`) and a provider `{"name":"cloud1","type":"openai_compat","endpoint":"https://api.example.com/v1","model":"<model>"}`. Cloud endpoints must be `https`, public, with no credentials in the URL.
3. `PUT /ai/providers/cloud1/credential` `{"api_key": "..."}` (needs `exchange_credentials`). The key is encrypted by the vault, only its last 4 characters are ever returned, and it never appears in logs or audit rows.

## Privacy policy
* **SECRET** (keys, tokens, passwords, anything shaped like one): removed everywhere; no model sees it.
* **PRIVATE** (balances, positions, order ids, account/user identifiers, emails): local models only. A cloud destination **refuses** the request (`PRIVACY_BLOCKED`) instead of silently stripping, including when it would be a fallback.
* **PUBLIC** (market data, indicator values, rule text): anywhere.
The `/ai/commentary` route accepts numbers only. The classifier is key-name and pattern based and can miss a secret in an unusual shape; do not feed it free-form private text.

## Failure handling
Timeout (default 20 s), bounded response (64 KiB) and prompt (12,000 chars), one retry by default and only for timeouts/unavailability (inference is read-only; invalid output is never retried), circuit breaker per provider (3 failures → open 60 s → one half-open probe), token-bucket rate limit (30/min). Prompt-injection hygiene: untrusted data is fenced, control characters stripped, the fence token neutralised, suspicious phrases flagged (informational). The real defence is that output can only be a schema-valid suggestion.

## Not verified
The Ollama, LM Studio and cloud adapters have **never been run against a real server** (no network here); they are tested through an injected transport and mock providers. Their request shapes follow the public API conventions and may need adjustment. Rollback: set `mode` to `LOCAL_ONLY` with no providers, or remove the routes; tables `ai_settings` and `ai_credentials` (migration 7) are referenced by nothing else.
