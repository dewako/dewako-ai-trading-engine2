"""Activity event schema (version 1) and normalizers.

Every event is derived from a row that already exists in an authoritative table; nothing here is generated, simulated or inferred beyond
mapping a stored state to a display field. Order events keep the engine's state: an unresolved submission is shown as UNKNOWN with
`reconciliation_required: true`, never as filled or failed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = 1
SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")
CATEGORIES = ("system", "auth", "safety", "signal", "risk", "order", "reconciliation", "incident", "strategy_package", "ai", "config")
SOURCES = ("audit", "order_events", "reconciliation_events")
MAX_SUMMARY, MAX_DETAILS_CHARS = 240, 2000

_SECRET_KEY = re.compile(r"(secret|password|passwd|token|api_?key|passphrase|authorization|private|credential|seed|cookie|session|blob)", re.I)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}|[A-Fa-f0-9]{40,}|[A-Za-z0-9+/_\-]{40,}={0,2}|"
                           r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_AUDIT_CATEGORY = [
    (("LOGIN", "LOGOUT", "ACCESS_DENIED", "USER_", "ROLE_CHANGED"), "auth"),
    (("KILL_SWITCH", "LIVE_", "EVIDENCE_", "ALERT"), "safety"),
    (("ORDER_BLOCKED",), "order"),
    (("RISK_",), "risk"), (("SIGNAL_",), "signal"), (("STRATEGY_PACKAGE",), "strategy_package"), (("AI_",), "ai"),
    (("RECONCILIATION_",), "reconciliation"), (("INCIDENT_",), "incident"), (("CREDENTIAL_", "EXCHANGE_"), "config"), (("ENGINE_",), "system"),
]
# Order lifecycle rows are canonical in order_events (every state change, including partial fills and unknown outcomes), so the audit
# duplicates of them are left out of the feed. ORDER_BLOCKED is not a lifecycle row (no order exists) and stays.
AUDIT_EXCLUDED_PREFIXES = ("ORDER_CREATED", "ORDER_SUBMITTED", "ORDER_UNKNOWN", "ORDER_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED")


def utc(v: Any) -> str:
    """Canonical UTC ISO-8601 text for a stored timestamp (SQLite text or PostgreSQL datetime)."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v[:40]
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    return str(v)[:40]


def _clean(o: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if isinstance(o, dict):
        return {str(k)[:60]: ("[redacted]" if _SECRET_KEY.search(str(k)) else _clean(v, depth + 1)) for k, v in list(o.items())[:30]}
    if isinstance(o, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(o)[:20]]
    if isinstance(o, str):
        return _SECRET_VALUE.sub("[redacted]", _CTRL.sub(" ", o))[:300]
    if o is None or isinstance(o, (bool, int, float)):
        return o
    return str(o)[:100]


def sanitize_details(raw: Any) -> dict:
    """Parse (if text), redact secrets, bound depth/width/size. Malformed input yields {} plus a flag; it never raises."""
    flag = {}
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return {"_malformed": True}
    if not isinstance(raw, dict):
        return {"_malformed": True} if raw not in (None, "") else {}
    out = _clean(raw)
    text = json.dumps(out, default=str)
    if len(text) > MAX_DETAILS_CHARS:
        return {"_truncated": True, "keys": sorted(out)[:30]}
    return {**out, **flag}


def _one_line(s: Any, n: int = MAX_SUMMARY) -> str:
    return _SECRET_VALUE.sub("[redacted]", _CTRL.sub(" ", str(s)))[:n]


def _event(*, source: str, rid: Any, ts: Any, type: str, category: str, severity: str, summary: str, status: str, correlation_id: str = "",
           environment: Optional[str] = None, instrument: Optional[str] = None, adapter: Optional[str] = None, strategy: Optional[str] = None,
           strategy_version: Optional[str] = None, fingerprint: Optional[str] = None, related_id: Optional[str] = None, details: Any = None,
           actor: Optional[str] = None, reconciliation_required: bool = False) -> dict:
    assert severity in SEVERITIES and category in CATEGORIES and source in SOURCES
    return {"schema_version": SCHEMA_VERSION, "event_id": f"{source}:{rid}", "ts": utc(ts), "type": type, "category": category, "severity": severity,
            "environment": environment, "correlation_id": _one_line(correlation_id or "", 64), "strategy": strategy, "strategy_version": strategy_version,
            "strategy_fingerprint": fingerprint, "instrument": instrument, "adapter": adapter, "related_id": related_id, "summary": _one_line(summary),
            "details": sanitize_details(details), "status": status, "source": source, "actor": actor, "reconciliation_required": reconciliation_required}


def _audit_category(action: str) -> str:
    for prefixes, cat in _AUDIT_CATEGORY:
        if action.startswith(prefixes):
            return cat
    return "system"


_BAD_RESULTS = {"DENIED": "WARNING", "REJECTED": "WARNING", "BLOCKED": "WARNING", "FAILED": "ERROR", "ERROR": "ERROR", "UNKNOWN": "ERROR",
                "VERSION_CONFLICT": "WARNING", "DUPLICATE": "INFO", "QUARANTINED": "ERROR", "DEGRADED": "WARNING", "UNAVAILABLE": "WARNING"}


def from_audit(r: dict) -> Optional[dict]:
    action = str(r["action"])
    if action.startswith(AUDIT_EXCLUDED_PREFIXES):
        return None
    result = str(r["result"] or "").upper()
    sev = _BAD_RESULTS.get(result, "INFO")
    if action.startswith(("KILL_SWITCH_ENABLED", "INCIDENT_OPENED")) or action == "ALERT":
        sev = "CRITICAL" if action.startswith("KILL_SWITCH") else max(sev, "WARNING", key=SEVERITIES.index)
    if action == "ACCESS_DENIED":
        sev = "WARNING"
    meta = r["metadata"]
    try:
        m = json.loads(meta) if isinstance(meta, (str, bytes)) else dict(meta or {})
    except (ValueError, TypeError, RecursionError):
        m = meta if isinstance(meta, str) else {}          # unparseable text is passed on so the sanitizer flags it as malformed
    who = r["username"] or "system"
    res = r["resource"] or ""
    summary = f"{action.replace('_', ' ').title()} by {who}" + (f" ({res})" if res else "") + (f": {result}" if result and result != "OK" else "")
    pkg = action.startswith("STRATEGY_PACKAGE")
    md = m if isinstance(m, dict) else {}
    if pkg and isinstance(md.get("name"), str) and md["name"]:
        label = md["name"]
        if isinstance(md.get("version"), str) and md["version"]:
            label += f" v{md['version']}"
        summary += f" — {label}"
    return _event(source="audit", rid=r["id"], ts=r["created_at"], type=action, category=_audit_category(action), severity=sev, summary=summary,
                  status=result or "OK", correlation_id=r["correlation_id"], environment=md.get("environment"),
                  strategy=md.get("name") if pkg else md.get("strategy"), strategy_version=md.get("version"), fingerprint=md.get("fingerprint"),
                  instrument=md.get("symbol"), adapter=md.get("provider"), related_id=res or None, details=m, actor=who)


_ORDER_STATUS = {"FILLED": ("INFO", "FILLED"), "PARTIALLY_FILLED": ("INFO", "PARTIALLY_FILLED"), "CANCELLED": ("INFO", "CANCELLED"),
                 "REJECTED": ("WARNING", "REJECTED"), "FAILED": ("ERROR", "FAILED"), "OPEN": ("INFO", "OPEN"), "SUBMITTED": ("INFO", "ACKNOWLEDGED"),
                 "SUBMITTING": ("INFO", "SUBMITTING"), "CREATED": ("INFO", "REQUESTED"), "PENDING": ("INFO", "REQUESTED")}


def from_order_event(r: dict) -> dict:
    new = str(r["new_state"])
    unknown = new in ("UNKNOWN", "RECONCILING") or str(r["event_type"]) == "OUTCOME_UNKNOWN"
    if unknown:
        sev, status = "ERROR", "UNKNOWN"                 # never success, never failure: the engine does not know, and says so
    else:
        sev, status = _ORDER_STATUS.get(new, ("INFO", new))
    side = f" {r['side']}" if r.get("side") else ""
    tail = " — OUTCOME UNKNOWN, RECONCILIATION REQUIRED" if unknown else ""
    summary = f"Order {r['order_id']}{side} {r.get('symbol') or ''}: {r.get('previous_state') or 'new'} → {new}{tail}"
    err = r.get("error")
    return _event(source="order_events", rid=r["id"], ts=r["created_at"], type=f"ORDER_{r['event_type']}", category="order", severity=sev, summary=summary,
                  status=status, correlation_id=r["correlation_id"], environment=r.get("environment"), instrument=r.get("symbol"), adapter=r.get("exchange"),
                  strategy=r.get("strategy_id"), strategy_version=r.get("strategy_version"), fingerprint=r.get("fingerprint"), related_id=f"order:{r['order_id']}",
                  details={"previous_state": r.get("previous_state"), "new_state": new, "error": err, "exchange_order_id": r.get("exchange_order_id"),
                           "filled": r.get("filled"), "quantity": r.get("quantity")}, actor=r.get("actor"), reconciliation_required=unknown)


def from_recon_event(r: dict) -> dict:
    new = str(r["new_state"])
    sev = "INFO" if new == "HEALTHY" else ("ERROR" if any(k in new for k in ("FAIL", "REQUIRED", "BLOCK")) else "WARNING")
    return _event(source="reconciliation_events", rid=r["id"], ts=r["created_at"], type="RECONCILIATION_STATE", category="reconciliation", severity=sev,
                  summary=f"Reconciliation {r['scope']}: {r.get('previous_state') or 'new'} → {new}", status=new, correlation_id=r["correlation_id"],
                  related_id=f"recon:{r['scope']}", details={"reason": r.get("reason")}, actor=r.get("actor"), reconciliation_required=new != "HEALTHY")


def sort_key(e: dict) -> tuple:
    return (e["ts"], e["event_id"])
