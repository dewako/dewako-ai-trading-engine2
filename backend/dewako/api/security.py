"""FastAPI dependencies: authentication, per-permission authorization, rate limiting, correlation ids."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import Depends, Header, HTTPException, Request

from dewako.api.runtime_state import Runtime, get_runtime
from dewako.auth.service import AuthError, Forbidden, RateLimited
from dewako.api.http_policy import SECURITY_HEADERS, cors_origins  # noqa: F401  (re-exported)


def correlation_id(request: Request) -> str:
    cid = getattr(request.state, "correlation_id", None)
    if not cid:
        cid = (request.headers.get("x-correlation-id") or "")[:64] or uuid.uuid4().hex
        request.state.correlation_id = cid
    return cid


def current_actor(request: Request, authorization: str = Header(default=""), rt: Runtime = Depends(get_runtime)) -> dict:
    cid = correlation_id(request)
    token = authorization.removeprefix("Bearer ").strip()
    try:
        # Two buckets. The client address bucket cannot be dodged by inventing tokens (an attacker controls the token string, not the
        # socket address). Behind a reverse proxy configure it to pass the real client address, or this bucket is shared by everyone.
        rt.limiter.check("ip:" + (request.client.host if request.client else "anon"))
        if token:
            rt.limiter.check("tok:" + hashlib.sha256(token.encode()).hexdigest()[:16])
        actor = rt.auth.authenticate(token)
    except RateLimited as e:
        raise HTTPException(429, str(e))
    except AuthError:
        raise HTTPException(401, "authentication required")
    return {**actor, "cid": cid}


def permission(name: str):
    def dep(actor: dict = Depends(current_actor), rt: Runtime = Depends(get_runtime)) -> dict:
        try:
            return rt.auth.require(actor, name, actor["cid"])
        except Forbidden as e:
            raise HTTPException(403, str(e))
    return dep

