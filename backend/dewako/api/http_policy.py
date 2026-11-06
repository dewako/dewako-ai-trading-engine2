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


# ---------------------------------------------------------------- dashboard (/ui) policy
# Scripts and stylesheets are external files served from this origin; there is NO 'unsafe-inline' for scripts and no 'unsafe-eval'.
# One scoped exception remains: style ATTRIBUTES ('style-src-attr'), because the prototype builds markup with inline style="..." in many
# places. Style attributes cannot execute script; connect-src/img-src/font-src stay same-origin so injected CSS has nowhere to send data.
UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
          "font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
UI_ASSETS = {"activity.js": "text/javascript; charset=utf-8", "packages.js": "text/javascript; charset=utf-8", "backtests.js": "text/javascript; charset=utf-8",
             "app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
UI_HEADERS = {"Content-Security-Policy": UI_CSP, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
              "Referrer-Policy": "no-referrer"}
