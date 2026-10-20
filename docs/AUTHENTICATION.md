# Authentication and authorization

* Users in `users` (roles ADMIN, TRADER, ANALYST, VIEWER). Passwords: scrypt with per-user salt, minimum 12 characters. Never stored or logged in plaintext.
* Sessions: random 32-byte tokens, only the SHA-256 hash is stored, 8 h expiry, revocable. Role changes and user disabling revoke sessions. The role is re-read from the DB on every request.
* Lockout: 5 failed logins -> 15 minutes. Failure messages are generic. Login and request rate limits exist (in-memory, per process).
* First admin: `POST /auth/bootstrap` with header `X-Bootstrap-Token` equal to `DEWAKO_BOOTSTRAP_TOKEN`; works only while no users exist. Unset the token afterwards.
* Permission table in `auth/service.py`; every route in `api/main.py` depends on `permission(...)` (checked by a route-guard script). Kill-switch release, live enabling, credentials, risk settings and user management are ADMIN-only; TRADER may trigger the kill switch and switch live OFF.
* Every login, denial, user/role change and sensitive action writes `audit_log` with user, role, action, resource, result, correlation id, scrubbed metadata. `X-Correlation-ID` is accepted or generated and returned.

**Limits:** no MFA, no password reset flow, rate limiter is per process, tokens travel as Bearer headers so TLS must be terminated in front.
