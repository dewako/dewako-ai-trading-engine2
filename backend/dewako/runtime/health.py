"""Health reports. Safe to expose to an authenticated user: states only, never a credential, token, key or raw error message."""
from __future__ import annotations

from dewako.reconciliation.state import ReconState
from dewako.safety.controller import TradingState


def _word(ok: bool) -> str:
    return "healthy" if ok else "unhealthy"


def health_report(db, safety, workers=None, vault=None, last_market_data_ok: bool | None = None, strategy_ok: bool | None = None,
                  startup=None) -> dict:
    out: dict = {}
    try:
        with db.read() as c:
            c.execute("SELECT 1").fetchone()
        out["database"] = "healthy"
        db_ok = True
    except Exception:
        out["database"] = "unhealthy"
        return {"database": "unhealthy", "live_trading": "blocked", "kill_switch": True, "trading_state": "TRADING_BLOCKED",
                "workers": "unknown", "risk": "unknown", "reconciliation": "unknown", "strategy_integrity": "unknown"}
    try:
        _res = safety.audit.verify_chain()
        out["audit_log"] = "healthy" if _res["ok"] else "TAMPERED"
        out["audit_head"] = _res.get("head")
    except Exception:
        out["audit_log"] = "unknown"
    out["market_data"] = "unknown" if last_market_data_ok is None else _word(last_market_data_ok)
    if workers is not None:
        ok, _ = workers.healthy()
        out["workers"] = _word(ok)
        out["worker_detail"] = {n: {k: v[k] for k in ("status", "critical", "restart_count") if k in v} for n, v in workers.status().items()}
    else:
        out["workers"] = "not_running"
    out["risk"] = _word(safety._get("risk_healthy") == "1")
    blocking = ReconState(db).blocking()
    with db.read() as c:
        unresolved = c.execute("SELECT COUNT(*) FROM orders WHERE state IN ('SUBMITTING','UNKNOWN','RECONCILING')").fetchone()[0]
    out["order_engine"] = "healthy" if unresolved == 0 else f"unresolved_orders:{unresolved}"
    out["reconciliation"] = "healthy" if not blocking else "unhealthy"
    if blocking:
        out["reconciliation_scopes"] = {s: st for s, st in blocking}
    if strategy_ok is None:
        with db.read() as c:
            bad = c.execute("SELECT COUNT(*) FROM strategy_versions WHERE release_status='INVALID'").fetchone()[0]
        strategy_ok = bad == 0
    out["strategy_integrity"] = _word(strategy_ok)
    out["credential_vault"] = "not_configured" if vault is None else "healthy"
    out["live_trading"] = safety.live_switch().value.lower()
    out["kill_switch"] = safety.kill_switch_active()
    out["trading_state"] = safety.trading_state().value
    if startup is not None:
        out["startup"] = startup
    return out
