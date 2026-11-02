"""FastAPI entry point.

Auth (Phase 1, temporary): every mutating route needs `Authorization: Bearer <DEWAKO_API_TOKEN>`.
If DEWAKO_API_TOKEN is not set, mutating routes are disabled (503). This is a single shared
admin token, not user accounts. Real login and roles arrive with the auth phase.
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from datetime import datetime, timedelta, timezone

from dewako.api.runtime_state import Runtime, get_application_factory, get_runtime, get_runtime_if_set, set_runtime
from dewako.api.security import correlation_id, current_actor, permission
from dewako.auth.service import AuthError, Forbidden, RateLimited
from dewako.exchanges.manager import ExchangeRejected
from dewako.runtime.application import Application, Config
from dewako.runtime.health import health_report
from dewako.safety.controller import NotAuthorized, ReadinessError
from dewako.market_data.backfill import backfill
from dewako.market_data.binance_public import BinancePublicProvider
from dewako.market_data.bybit_public import BybitPublicProvider
from dewako.market_data.hub import AllProvidersFailed, MarketDataHub
from dewako.market_data.freshness import CandleFreshnessError
from dewako.market_data.okx_public import OKXPublicProvider
from dewako.market_data.provider import ProviderError
from dewako.market_data.state_builder import InsufficientData, build_market_state
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import SECONDS
from dewako.risk.engine import RiskLimits
from dewako.risk.controls import control_status
from dewako.runtime.bot_service import BotError
from dewako.services.credentials import CredentialVault, VaultError
from dewako.strategies import base as strategy_base
from dewako.strategies import library  # noqa: F401  (registers the built-in strategies)
from dewako.strategies.base import all_strategies
from dewako.strategies.selector import recommend
from dewako.strategies.signal_engine import SignalEngine
from dewako.strategies.switching import PolicyRequired, PositionPolicy, StrategySwitcher, SwitchBlocked
from dewako.strategies.versions import StrategyVersionConflict, StrategyVersionLock
from dewako.strategies.loader import PluginsDisabled, load_code_plugins
from dewako.strategies.rule_builder import RuleSpec, build_strategy_class
from dewako.strategy_packages.archive import MAX_ARCHIVE_BYTES, PackageRejected
from dewako.strategy_packages.manager import TransitionRefused

@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Startup: ordered, recorded steps (runtime/application.py). A failed critical step leaves the process up but live BLOCKED.
    A strategy fingerprint conflict does NOT stop the API (so an admin can inspect it) but it does block live trading.
    Shutdown: stop accepting, drain workers, close adapters and the database."""
    existing = get_runtime_if_set()
    factory = get_application_factory()
    application = factory(existing) if factory is not None else Application(Config.from_env(), rt=existing)
    await application.startup()
    set_runtime(application.rt)
    app_.state.application = application
    try:
        yield
    finally:
        await application.shutdown()
        set_runtime(None)


app = FastAPI(title="DEWAKO AI Trading Engine", version="0.5.0", lifespan=lifespan)
_hub: MarketDataHub | None = None
_store: CandleStore | None = None


def hub() -> MarketDataHub:
    global _hub
    if _hub is None:
        _hub = MarketDataHub([BinancePublicProvider(), BybitPublicProvider(), OKXPublicProvider()])
    return _hub


def store() -> CandleStore:
    global _store
    if _store is None:
        _store = CandleStore(os.environ.get("DEWAKO_DB_PATH", "dewako_market.db"))
    return _store


def _check_market(symbol: str, timeframe: str) -> None:
    if "/" not in symbol:
        raise HTTPException(422, "symbol must look like BTC/USDT")
    if timeframe not in SECONDS:
        raise HTTPException(422, f"timeframe must be one of {sorted(SECONDS, key=SECONDS.get)}")


class AddExchange(BaseModel):
    exchange: str
    label: str = Field("", max_length=60)
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=512)
    passphrase: str = Field("", max_length=128)


@app.middleware("http")
async def add_correlation_header(request: Request, call_next):
    cid = correlation_id(request)
    resp = await call_next(request)
    resp.headers["X-Correlation-ID"] = cid
    return resp


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    from dewako.api.http_policy import SECURITY_HEADERS
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


_origins = __import__("dewako.api.http_policy", fromlist=["cors_origins"]).cors_origins(os.environ)
if _origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"])


@app.middleware("http")
async def reject_when_not_accepting(request: Request, call_next):
    application = getattr(request.app.state, "application", None)
    if application is not None and not application.accepting and request.url.path not in ("/health", "/health/live"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "service is shutting down or not ready"}, status_code=503)
    return await call_next(request)


@app.get("/health/live")
def health_live():
    """Liveness only: the process is up and answering. It says nothing about whether trading protection works; use /health for that."""
    return {"status": "alive"}


@app.get("/health")
def health(request: Request):
    """Public and minimal READINESS, computed from the CURRENT state of the application, not the startup verdict. When workers are running
    and a critical worker (exit monitor, risk equity, ...) is not healthy it answers 503 {"status": "degraded", "state": "SYSTEM_DEGRADED"}.
    Unknown health is never reported as ready. No internal detail; detail is at /health/detailed (authenticated)."""
    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "not_ready", "state": "UNKNOWN"}
    ready, state = application.readiness()
    if ready:
        return {"status": "ok", "state": state}
    if state == "SYSTEM_DEGRADED":
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "degraded", "state": state}, status_code=503)
    return {"status": "not_ready", "state": state}


@app.get("/health/detailed")
def health_detailed(request: Request, actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    application = getattr(request.app.state, "application", None)
    return application.health() if application is not None else health_report(rt.db, rt.safety)


# ---- authentication
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.post("/auth/login")
def login(body: LoginBody, request: Request, rt: Runtime = Depends(get_runtime)):
    cid = correlation_id(request)
    try:
        rt.login_limiter.check(request.client.host if request.client else "anon")
        return {"token": rt.auth.login(body.username, body.password, cid), "token_type": "Bearer"}
    except RateLimited as e:
        raise HTTPException(429, str(e))
    except AuthError as e:
        raise HTTPException(401, str(e))


@app.get("/auth/me")
def whoami(actor: dict = Depends(current_actor)):
    """Who am I and what may I do (names only). The UI uses it to decide which buttons to show; the server still enforces every action."""
    from dewako.auth.service import PERMISSIONS
    return {"username": actor.get("username"), "role": actor.get("role"), "permissions": sorted(PERMISSIONS.get(actor.get("role", ""), set()))}


@app.post("/auth/logout", status_code=204)
def logout(request: Request, actor: dict = Depends(current_actor), authorization: str = Header(default=""), rt: Runtime = Depends(get_runtime)):
    """Protected: only a currently valid session can be logged out (missing, invalid, expired or already-revoked credentials get 401, like every
    other protected route). Revokes exactly the session that presented the token."""
    rt.auth.logout(authorization.removeprefix("Bearer ").strip(), correlation_id(request))


class BootstrapBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


@app.post("/auth/bootstrap", status_code=201)
def bootstrap(body: BootstrapBody, request: Request, x_bootstrap_token: str = Header(default=""), rt: Runtime = Depends(get_runtime)):
    """Creates the first ADMIN. Works only while there are no users AND DEWAKO_BOOTSTRAP_TOKEN is set and matches."""
    try:
        rt.login_limiter.check("bootstrap:" + (request.client.host if request.client else "anon"))    # rate-limited like login
    except RateLimited:
        raise HTTPException(429, "too many attempts")
    expected = os.environ.get("DEWAKO_BOOTSTRAP_TOKEN", "")
    if not expected or not hmac.compare_digest(x_bootstrap_token, expected):
        raise HTTPException(403, "bootstrap is disabled")
    try:
        return {"id": rt.auth.create_user(None, body.username, body.password, "ADMIN", correlation_id(request))}
    except (ValueError, AuthError) as e:
        raise HTTPException(409, "bootstrap already done" if isinstance(e, AuthError) else str(e))


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    role: str


@app.post("/users", status_code=201)
def create_user(body: NewUser, actor: dict = Depends(permission("user_management")), rt: Runtime = Depends(get_runtime)):
    try:
        return {"id": rt.auth.create_user(actor, body.username, body.password, body.role, actor["cid"])}
    except ValueError as e:
        raise HTTPException(422, str(e))


class RoleBody(BaseModel):
    role: str


@app.put("/users/{user_id}/role")
def change_role(user_id: int, body: RoleBody, actor: dict = Depends(permission("user_management")), rt: Runtime = Depends(get_runtime)):
    try:
        rt.auth.set_role(actor, user_id, body.role, actor["cid"])
    except Forbidden as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


# ---- safety: status, kill switch, live switch
@app.get("/safety/status")
def safety_status(actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    return health_report(rt.db, rt.safety)


class Reason(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@app.post("/safety/kill-switch")
def kill_switch_on(body: Reason, actor: dict = Depends(permission("kill_switch_trigger")), rt: Runtime = Depends(get_runtime)):
    rt.safety.activate_kill_switch(actor, body.reason, actor["cid"])
    return {"kill_switch": "ON"}


@app.delete("/safety/kill-switch")
def kill_switch_off(actor: dict = Depends(permission("kill_switch_release")), rt: Runtime = Depends(get_runtime)):
    try:
        rt.safety.release_kill_switch(actor, actor["cid"])
    except NotAuthorized as e:
        raise HTTPException(403, str(e))
    return {"kill_switch": "OFF"}


@app.post("/safety/live/enable")
def live_enable(actor: dict = Depends(permission("live_trading")), rt: Runtime = Depends(get_runtime)):
    """Readiness is re-evaluated server-side from stored state; nothing in the request can influence it. 409 when readiness blocks the transition."""
    try:
        rt.safety.enable_live(actor, actor["cid"])
    except ReadinessError as e:
        raise HTTPException(409, {"error": "live_readiness_failed", "reasons": e.reasons, "live_trading": rt.safety.live_switch().value})
    except NotAuthorized as e:
        raise HTTPException(403, str(e))
    return {"live_trading": rt.safety.live_switch().value}


@app.post("/safety/live/disable")
def live_disable(actor: dict = Depends(permission("trade_actions")), rt: Runtime = Depends(get_runtime)):
    rt.safety.disable_live(actor, actor["cid"])
    return {"live_trading": rt.safety.live_switch().value}


@app.get("/audit")
def audit_log(limit: int = 100, action: str | None = None, actor: dict = Depends(permission("user_management")), rt: Runtime = Depends(get_runtime)):
    return rt.audit.recent(min(max(limit, 1), 500), action)


# ---- read-only trading state (there is deliberately NO route that places an order: orders come only from server-side bots)
@app.get("/orders")
def list_orders(limit: int = 100, actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    with rt.db.read() as c:
        return [dict(r) for r in c.execute("SELECT id,client_order_id,exchange,environment,symbol,side,quantity,filled,state,created_at,correlation_id FROM orders ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))]


@app.get("/orders/{order_id}/events")
def order_events(order_id: int, actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    with rt.db.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))]


@app.get("/positions")
def list_positions(actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    with rt.db.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 500")]


# ---- exchanges
@app.get("/exchanges")
def list_exchanges(actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    try:
        return rt.manager().list()
    except VaultError as e:
        raise HTTPException(503, str(e))


@app.post("/exchanges", status_code=201)
async def add_exchange(body: AddExchange, actor: dict = Depends(permission("exchange_credentials")), rt: Runtime = Depends(get_runtime)):
    try:
        return await rt.manager().add_exchange(body.exchange, body.label, body.api_key, body.api_secret, body.passphrase,
                                               actor["username"], actor, actor["cid"])
    except ExchangeRejected as e:
        raise HTTPException(422, str(e))
    except VaultError as e:
        raise HTTPException(503, str(e))


@app.post("/exchanges/{record_id}/enable-live")
async def enable_exchange_live(record_id: int, actor: dict = Depends(permission("live_trading")), rt: Runtime = Depends(get_runtime)):
    try:
        await rt.manager().enable_for_live(record_id, actor, actor["cid"])
    except KeyError:
        raise HTTPException(404, "no such exchange")
    except ExchangeRejected as e:
        raise HTTPException(422, str(e))
    return {"enabled": True}


@app.delete("/exchanges/{record_id}", status_code=204)
def remove_exchange(record_id: int, actor: dict = Depends(permission("exchange_credentials")), rt: Runtime = Depends(get_runtime)):
    try:
        rt.manager().remove(record_id, actor["username"], actor, actor["cid"])
    except KeyError:
        raise HTTPException(404, "no such exchange")


# ---- strategies
@app.get("/strategies")
def strategies(actor: dict = Depends(permission("strategies"))):
    return [s().describe() for s in all_strategies()]


_switcher = StrategySwitcher()  # no position source yet, so unknown position = policy required (fail closed)


@app.get("/strategies/{name}/versions")
def strategy_versions(name: str, actor: dict = Depends(permission("strategies"))):
    vs = strategy_base.versions(name)
    if not vs:
        raise HTTPException(404, "no such strategy")
    return {"name": name, "versions": vs, "latest": vs[-1]}


@app.get("/strategies/{name}/signal-preview")
async def signal_preview(name: str, symbol: str, timeframe: str = "15m", version: str | None = None, actor: dict = Depends(permission("strategies"))):
    """Read-only: what would this strategy say right now? Never sizes or places anything."""
    _check_market(symbol, timeframe)
    try:
        cls = strategy_base.get(name, version) if version else strategy_base.latest(name)
    except KeyError:
        raise HTTPException(404, "no such strategy or version")
    try:
        recs, used = await hub().candles(symbol, timeframe, limit=200)
        st = build_market_state(symbol, [r.candle for r in recs], datetime.now(timezone.utc), timeframe)
    except AllProvidersFailed as e:
        raise HTTPException(502, str(e))
    except InsufficientData as e:
        raise HTTPException(422, str(e))
    except CandleFreshnessError as e:
        raise HTTPException(503, str(e))
    res = SignalEngine(cls()).evaluate(st)
    return {"preview_only": True, "strategy": cls.name, "version": cls.version, "source": used,
            "signal": res.signal.model_dump(mode="json") if res.signal else None, "reason": res.reason}


@app.get("/strategies/recommendation")
async def strategy_recommendation(symbol: str, timeframe: str = "15m", actor: dict = Depends(permission("strategies"))):
    """Rule-based recommendation with reasons. It only recommends; it cannot switch anything."""
    _check_market(symbol, timeframe)
    try:
        recs, used = await hub().candles(symbol, timeframe, limit=200)
        st = build_market_state(symbol, [r.candle for r in recs], datetime.now(timezone.utc), timeframe)
    except AllProvidersFailed as e:
        raise HTTPException(502, str(e))
    except InsufficientData as e:
        raise HTTPException(422, str(e))
    except CandleFreshnessError as e:
        raise HTTPException(503, str(e))
    return {**recommend(st).model_dump(), "source": used}


class SwitchBody(BaseModel):
    bot_id: str = Field(min_length=1, max_length=64)
    strategy: str
    version: str | None = None
    position_policy: PositionPolicy | None = None


@app.post("/strategies/switch")
def switch_strategy(body: SwitchBody, actor: dict = Depends(permission("trade_actions"))):
    """Paper bots only for now. An open or unknown position requires an explicit position_policy."""
    try:
        r = _switcher.assign(body.bot_id, body.strategy, body.version, mode="PAPER", actor=actor["username"], policy=body.position_policy)
    except PolicyRequired as e:
        raise HTTPException(409, str(e))
    except SwitchBlocked as e:
        raise HTTPException(422, str(e))
    return {"bot_id": body.bot_id, "strategy": r.binding.strategy, "version": r.binding.version, "mode": r.binding.mode,
            "entries_blocked": r.binding.entries_blocked, "actions": r.actions}


@app.post("/strategies", status_code=201)
def add_rule_strategy(spec: RuleSpec, actor: dict = Depends(permission("risk_settings"))):
    """Create a strategy from whitelisted rules. No code is executed."""
    try:
        cls = build_strategy_class(spec)
    except ValueError as e:  # duplicate name+version
        raise HTTPException(409, str(e))
    return cls().describe()


@app.post("/strategies/plugins/reload")
def reload_code_plugins(actor: dict = Depends(permission("risk_settings"))):
    try:
        return {"loaded": load_code_plugins(Path(os.environ.get("DEWAKO_PLUGIN_DIR", "plugins/strategies")))}
    except PluginsDisabled as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


def _application(rt: Runtime):
    a = rt.application
    if a is None or a.bot_service is None or a.limits_store is None:
        raise HTTPException(503, "the trading application is not running")
    return a


@app.get("/risk/limits")
def risk_limits(rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("read"))):
    """The limits in force (stored version, or DEFAULT if none was ever set) and which controls are actually enforced right now."""
    a = _application(rt)
    limits, meta = a.limits_store.current()
    equity_ok = (a.supervisor.status().get("risk_equity", {}).get("status") == "HEALTHY") if a.supervisor is not None else False
    return {"limits": limits.model_dump(), **meta, "controls": control_status(equity_feed_ok=equity_ok)}


class RiskLimitsBody(BaseModel):
    limits: dict
    reason: str = Field(default="", max_length=300)


@app.put("/risk/limits")
def set_risk_limits(body: RiskLimitsBody, rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("risk_settings"))):
    a = _application(rt)
    try:
        return a.limits_store.set(body.limits, actor=actor, reason=body.reason)
    except ValidationError as e:
        raise HTTPException(422, [{"field": ".".join(str(x) for x in err["loc"]), "problem": err["msg"]} for err in e.errors()])
    except ValueError as e:
        raise HTTPException(422, str(e))


class BotBody(BaseModel):
    bot_id: str = Field(min_length=1, max_length=40)
    strategy: str
    version: str
    symbol: str
    timeframe: str
    exchange: str = "paper"
    environment: str = "PAPER"


@app.get("/bots")
def list_bots(rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("read"))):
    return _application(rt).bot_service.list()


@app.post("/bots", status_code=201)
async def create_bot(body: BotBody, rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("risk_settings"))):
    try:
        return await _application(rt).bot_service.create(bot_id=body.bot_id, strategy=body.strategy, version=body.version, symbol=body.symbol,
                                                         timeframe=body.timeframe, environment=body.environment, exchange=body.exchange, actor=actor)
    except BotError as e:
        raise HTTPException(409, str(e))


@app.post("/bots/{bot_id}/enable")
def enable_bot(bot_id: str, rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("risk_settings"))):
    try:
        return _application(rt).bot_service.set_enabled(bot_id, True, actor)
    except BotError as e:
        raise HTTPException(404, str(e))


@app.post("/bots/{bot_id}/disable")
def disable_bot(bot_id: str, rt: Runtime = Depends(get_runtime), actor: dict = Depends(permission("risk_settings"))):
    try:
        return _application(rt).bot_service.set_enabled(bot_id, False, actor)
    except BotError as e:
        raise HTTPException(404, str(e))


# ---- market data (Phase 2). Live routes call the exchanges' public APIs; stored routes read the local point-in-time store.
@app.get("/markets/providers")
def market_providers(actor: dict = Depends(permission("market_data"))):
    return [{"name": p.name, "max_candles_per_request": p.max_candles_per_request} for p in hub().providers]


@app.get("/markets/candles")
async def live_candles(symbol: str, timeframe: str = "15m", limit: int = 200, source: str | None = None, actor: dict = Depends(permission("market_data"))):
    _check_market(symbol, timeframe)
    try:
        recs, used = await hub().candles(symbol, timeframe, limit=min(max(limit, 1), 1000), prefer=source)
    except AllProvidersFailed as e:
        raise HTTPException(502, str(e))
    return {"source": used, "candles": [{"t": r.candle.t, "o": r.candle.o, "h": r.candle.h, "l": r.candle.l, "c": r.candle.c,
                                          "v": r.candle.v, "published": r.quality.publication_timestamp} for r in recs]}


@app.get("/markets/state")
async def live_market_state(symbol: str, timeframe: str = "15m", actor: dict = Depends(permission("market_data"))):
    _check_market(symbol, timeframe)
    try:
        recs, used = await hub().candles(symbol, timeframe, limit=200)
        st = build_market_state(symbol, [r.candle for r in recs], datetime.now(timezone.utc), timeframe)
    except AllProvidersFailed as e:
        raise HTTPException(502, str(e))
    except InsufficientData as e:
        raise HTTPException(422, str(e))
    except CandleFreshnessError as e:
        raise HTTPException(503, str(e))
    d = st.model_dump(exclude={"candles"})
    d["source"] = used
    return d


@app.get("/markets/stored/candles")
def stored_candles(symbol: str, timeframe: str, source: str, start: datetime, end: datetime, as_of: datetime | None = None, actor: dict = Depends(permission("market_data"))):
    """Candles from the local store as known at `as_of` (point in time). Datetimes must include a timezone."""
    _check_market(symbol, timeframe)
    for d in (start, end, as_of):
        if d is not None and d.tzinfo is None:
            raise HTTPException(422, "datetimes must include a timezone, e.g. 2026-01-01T00:00:00Z")
    recs = store().get_candles(symbol, timeframe, source, start, end, as_of)
    return [{"t": r.candle.t, "o": r.candle.o, "h": r.candle.h, "l": r.candle.l, "c": r.candle.c, "v": r.candle.v,
             "revision": r.quality.revision} for r in recs]


@app.get("/markets/stored/gaps")
def stored_gaps(symbol: str, timeframe: str, source: str, start: datetime, end: datetime, actor: dict = Depends(permission("market_data"))):
    _check_market(symbol, timeframe)
    for d in (start, end):
        if d.tzinfo is None:
            raise HTTPException(422, "datetimes must include a timezone")
    return {"missing": store().find_gaps(symbol, timeframe, source, start, end)}


class BackfillRequest(BaseModel):
    symbol: str
    timeframe: str = "15m"
    provider: str = "binance"
    start: datetime
    end: datetime


@app.post("/markets/backfill")
async def run_backfill(body: BackfillRequest, actor: dict = Depends(permission("market_data"))):
    _check_market(body.symbol, body.timeframe)
    if body.start.tzinfo is None or body.end.tzinfo is None:
        raise HTTPException(422, "datetimes must include a timezone")
    if body.end <= body.start or body.end - body.start > timedelta(days=366):
        raise HTTPException(422, "end must be after start, and the range at most 366 days per request")
    try:
        prov = hub().by_name(body.provider)
    except KeyError:
        raise HTTPException(404, f"unknown provider {body.provider!r}")
    try:
        rep = await backfill(prov, store(), body.symbol, body.timeframe, body.start, body.end)
    except ProviderError as e:
        raise HTTPException(502, str(e))
    return rep.__dict__


# ---------------------------------------------------------------- strategy packages (Phase 4.2). Data only: nothing in a package is executed.
class PackageUpload(BaseModel):
    archive_base64: str = Field(max_length=(MAX_ARCHIVE_BYTES * 4) // 3 + 16)


class PackageTransition(BaseModel):
    to: str = Field(max_length=32)
    reason: str = Field("", max_length=300)


@app.post("/strategy-packages", status_code=201)
def import_strategy_package(body: PackageUpload, request: Request, actor: dict = Depends(permission("strategies")), rt: Runtime = Depends(get_runtime)):
    import base64, binascii
    try:
        data = base64.b64decode(body.archive_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "archive_base64 is not valid base64")
    try:
        return rt.packages.import_package(data, actor, correlation_id(request))
    except PackageRejected as e:
        raise HTTPException(409 if e.code in ("VERSION_CONFLICT", "DUPLICATE_PACKAGE") else 422, {"code": e.code, "detail": e.detail[:300]})
    except TransitionRefused as e:
        raise HTTPException(403, str(e))


@app.get("/strategy-packages")
def list_strategy_packages(actor: dict = Depends(permission("strategies")), rt: Runtime = Depends(get_runtime)):
    return rt.packages.list()


@app.get("/strategy-packages/{package_id}")
def get_strategy_package(package_id: int, actor: dict = Depends(permission("strategies")), rt: Runtime = Depends(get_runtime)):
    try:
        return rt.packages.get(package_id)
    except KeyError:
        raise HTTPException(404, "no such package")


@app.post("/strategy-packages/{package_id}/transition")
def transition_strategy_package(package_id: int, body: PackageTransition, request: Request, actor: dict = Depends(permission("strategies")),
                                rt: Runtime = Depends(get_runtime)):
    """The manager enforces the per-transition permission again; this route only requires an authenticated strategies user."""
    try:
        return rt.packages.transition(package_id, body.to, actor, body.reason, correlation_id(request))
    except KeyError:
        raise HTTPException(404, "no such package")
    except TransitionRefused as e:
        raise HTTPException(403 if "permission" in str(e) else 409, str(e))


# ---------------------------------------------------------------- AI provider manager (Phase 4.3). Output is advisory data; no route lets a model act.
class AISettingsBody(BaseModel):
    config: dict = Field(default_factory=dict)
    providers: list[dict] = Field(default_factory=list, max_length=8)


class AICredentialBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


class AICommentaryBody(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,12}(/[A-Z0-9]{2,12})?$")
    timeframe: str = Field(pattern=r"^(1m|3m|5m|15m|30m|1h|4h|1d)$")
    indicators: dict[str, float] = Field(default_factory=dict, max_length=20)   # numbers only: no free text reaches the model from this route


def _ai_store(rt: Runtime):
    from dewako.ai.settings import AISettingsStore
    return AISettingsStore(rt.db, rt.audit)


@app.get("/ai/settings")
def get_ai_settings(actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    return _ai_store(rt).public()


@app.put("/ai/settings")
def put_ai_settings(body: AISettingsBody, request: Request, actor: dict = Depends(permission("risk_settings")), rt: Runtime = Depends(get_runtime)):
    from dewako.ai.errors import AIError
    try:
        return _ai_store(rt).save(body.config, body.providers, actor, correlation_id(request))
    except AIError as e:
        raise HTTPException(422, e.detail or e.code)
    except TypeError:
        raise HTTPException(422, "unknown configuration field")


@app.put("/ai/providers/{name}/credential")
def put_ai_credential(name: str, body: AICredentialBody, request: Request, actor: dict = Depends(permission("exchange_credentials")),
                      rt: Runtime = Depends(get_runtime)):
    from dewako.ai.errors import AIError
    try:
        return _ai_store(rt).set_credential(name, body.api_key, CredentialVault(), actor, correlation_id(request))   # the key is never echoed back
    except VaultError:
        raise HTTPException(503, "credential vault is not configured")
    except AIError as e:
        raise HTTPException(422, e.detail or e.code)


@app.post("/ai/commentary")
async def ai_commentary(body: AICommentaryBody, request: Request, actor: dict = Depends(permission("strategies")), rt: Runtime = Depends(get_runtime)):
    """Advisory market commentary. The result is a suggestion; nothing in it is ever executed, and a provider outage returns status UNAVAILABLE."""
    mgr = _ai_store(rt).build_manager(CredentialVault)
    res = await mgr.ask("Comment briefly on the market state.", "market_commentary", 1, body.model_dump(), correlation_id=correlation_id(request))
    return {"status": res.status, "advisory": True, "provider": res.provider, "used_fallback": res.used_fallback, "suggestion": res.suggestion,
            "attempts": res.attempts}


# ---------------------------------------------------------------- Live Activity (Phase 4.4). Read-only: these routes never write or act.
_stream_limiter = None


def _activity(rt: Runtime):
    from dewako.activity.service import ActivityService
    return ActivityService(rt.db)


def _filters(q: dict):
    from dewako.activity.service import BadRequest, Filters
    try:
        return Filters(**{k: v for k, v in q.items() if v not in (None, "")})
    except BadRequest as e:
        raise HTTPException(422, str(e))


@app.get("/activity")
def activity_history(limit: int = 50, cursor: str | None = None, category: str | None = None, severity: str | None = None, status: str | None = None,
                     strategy: str | None = None, environment: str | None = None, instrument: str | None = None, source: str | None = None,
                     correlation_id: str | None = None, type: str | None = None, q: str | None = None, since: str | None = None, until: str | None = None,
                     actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    from dewako.activity.service import BadRequest
    flt = _filters(dict(category=category, severity=severity, status=status, strategy=strategy, environment=environment, instrument=instrument,
                        source=source, correlation_id=correlation_id, type=type, q=q, since=since, until=until))
    try:
        return _activity(rt).query(flt, limit=limit, cursor=cursor)
    except BadRequest as e:
        raise HTTPException(422, str(e))


@app.get("/activity/status")
def activity_status(actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    application = getattr(app.state, "application", None)
    health = application.health() if application is not None else None
    return _activity(rt).status(health)


@app.get("/activity/stream")
async def activity_stream(request: Request, cursor: str | None = None, category: str | None = None, severity: str | None = None,
                          strategy: str | None = None, last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                          authorization: str = Header(default=""), actor: dict = Depends(permission("read")), rt: Runtime = Depends(get_runtime)):
    """Server-Sent Events. Auth is the normal bearer header (the browser client uses fetch streaming, not EventSource, so it can send it)."""
    global _stream_limiter
    from fastapi.responses import StreamingResponse
    from dewako.activity.service import BadRequest, decode_cursor
    from dewako.activity.stream import StreamLimiter, event_stream
    _stream_limiter = _stream_limiter or StreamLimiter()
    flt = _filters(dict(category=category, severity=severity, strategy=strategy))
    try:
        marks = decode_cursor(last_event_id or cursor) or None
    except BadRequest as e:
        raise HTTPException(422, str(e))
    if not _stream_limiter.acquire():
        raise HTTPException(429, "too many activity streams")
    token = authorization.removeprefix("Bearer ").strip()

    def still_ok() -> bool:
        try:
            rt.auth.authenticate(token)
            return True
        except Exception:
            return False

    async def gen():
        try:
            async for chunk in event_stream(_activity(rt), marks=marks, flt=flt, is_authorized=still_ok):
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            _stream_limiter.release()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# The dashboard itself, served same-origin so no CORS is needed. Shared policy: dewako/api/http_policy.py (no 'unsafe-inline' for scripts).
from dewako.api.http_policy import UI_ASSETS, UI_CSP, UI_HEADERS  # noqa: E402
_UI_CSP = UI_CSP


def _ui_dir() -> Path:
    return Path(os.environ.get("DEWAKO_UI_DIR") or Path(__file__).resolve().parents[3] / "frontend" / "prototype")


@app.get("/ui", include_in_schema=False)
def ui_redirect():
    """The page references its scripts relatively, so it must be served from /ui/ (otherwise they would resolve to /app.js)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/ui/", status_code=307, headers=UI_HEADERS)


@app.get("/ui/", include_in_schema=False)
def ui_index():
    from fastapi.responses import FileResponse, PlainTextResponse
    f = _ui_dir() / "index.html"
    if not f.is_file():
        return PlainTextResponse("dashboard files not found", status_code=404, headers=UI_HEADERS)
    return FileResponse(f, media_type="text/html; charset=utf-8", headers=UI_HEADERS)


@app.get("/ui/{name}", include_in_schema=False)
def ui_asset(name: str):
    from fastapi.responses import FileResponse, PlainTextResponse
    if name not in UI_ASSETS:          # an explicit allowlist: no path is ever built from user input
        return PlainTextResponse("not found", status_code=404, headers=UI_HEADERS)
    f = _ui_dir() / name
    if not f.is_file():
        return PlainTextResponse("not found", status_code=404, headers=UI_HEADERS)
    return FileResponse(f, media_type=UI_ASSETS[name], headers=UI_HEADERS)
