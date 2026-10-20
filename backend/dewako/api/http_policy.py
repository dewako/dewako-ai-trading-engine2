"""HTTP policy that needs no web framework (so it can be unit-tested anywhere): security headers and CORS origins."""
from __future__ import annotations


# ---------------------------------------------------------------- response headers and CORS (pure functions, unit-tested)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",                               # API responses carry account data: never cached by browsers or proxies
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def cors_origins(environ) -> list[str]:
    """Allowed browser origins from DEWAKO_CORS_ORIGINS (comma separated). Default: none (same-origin only). A wildcard is refused
    because the API uses bearer tokens and must never be callable from any website."""
    raw = (environ.get("DEWAKO_CORS_ORIGINS") or "").strip()
    out = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    for o in out:
        if o == "*" or not o.startswith(("http://", "https://")):
            raise ValueError(f"invalid CORS origin {o!r}: wildcards and non-http(s) origins are not allowed")
    return out
