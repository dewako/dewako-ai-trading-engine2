"""Audit log: scrubbed, hash-chained, append-only.

Each row stores prev_hash and row_hash = SHA-256 over (prev_hash, all fields). Altering, deleting or reordering a row breaks every later
hash, which `verify_chain()` finds. The database also refuses UPDATE/DELETE/TRUNCATE on audit_log (triggers). Limits, stated plainly:
a database superuser can drop the triggers and rewrite the whole chain, and removing the newest rows leaves a valid shorter chain. Real
tamper-evidence therefore needs the head hash (`head()`, also in /health/detailed) copied to a place the database owner cannot edit.
Rows written before migration 5 have no hashes; they are reported as `legacy_rows`, not verified.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import re
import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from dewako.persistence.db import Database, IntegrityError, now_iso

_SECRET = re.compile(r"(secret|password|passwd|token|api_?key|passphrase|authorization|private)", re.I)
GENESIS = "0" * 64
_ADVISORY_KEY = 7_770_002


log = logging.getLogger("dewako.audit")
MAX_PENDING = 1000          # audit events held in memory while storage is failing; beyond this the OLDEST are lost and that is reported
_RECENT_ROWS_CHECKED = 500  # how far back a late write looks for its own id before writing (idempotent recovery)


class AuditFailureState:
    """Audit events that could NOT be persisted, shared by every AuditLog on the same Database.

    Rules (Stage 2 WP6): an audit-storage failure never changes what the trading operation does and is never silent. The event, with the
    original exception's class and message, is kept here and logged at CRITICAL; the system reports audit persistence as DEGRADED (new
    risk-increasing orders are blocked, exits are not) until every held event has been written. Held events live in MEMORY only: a crash
    while degraded loses them, and the CRITICAL log line is then the only trace. If more than MAX_PENDING pile up, the oldest are dropped
    and counted as lost; `lost` stays non-zero (degraded) until a human acknowledges it."""

    def __init__(self, max_pending: int = MAX_PENDING):
        self.lock = threading.RLock()
        self.pending: "collections.deque[dict]" = collections.deque()
        self.max_pending = max_pending
        self.failed_total = 0
        self.lost = 0
        self.flushed = 0
        self.last_error: Optional[dict] = None
        self.first_failure_at: Optional[str] = None


def _state_of(db: Any) -> AuditFailureState:
    st = getattr(db, "_audit_failure_state", None)
    if st is None:
        st = AuditFailureState()
        try:
            db._audit_failure_state = st
        except AttributeError:
            pass
    return st


def scrub(meta: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(meta, dict):
        return {k: ("[redacted]" if _SECRET.search(str(k)) else scrub(v, depth + 1)) for k, v in meta.items()}
    if isinstance(meta, (list, tuple)):
        return [scrub(v, depth + 1) for v in meta]
    return meta if isinstance(meta, (str, int, float, bool)) or meta is None else str(meta)


def _ts(v: Any) -> str:
    """One canonical text form for a timestamp whether the database hands back text (SQLite, or PostgreSQL's '... +00') or a datetime."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else str(v)


def _hash(prev: str, created_at: Any, user_id: Any, username: Any, role: Any, action: str, resource: Any, result: str,
          correlation_id: str, metadata: str) -> str:
    body = json.dumps([prev, _ts(created_at), None if user_id is None else int(user_id), username, role, action, resource or "", result,
                       correlation_id, metadata], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def _append(self, c: Any, row: tuple) -> None:
        created_at, user_id, username, role, action, resource, result, cid, meta = row
        if self.db.is_pg:
            c.execute("SELECT pg_advisory_xact_lock(?)", (_ADVISORY_KEY,))     # serialises chain writers until the transaction ends
        tip = c.execute("SELECT row_hash FROM audit_log WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        prev = tip["row_hash"] if tip else GENESIS
        h = _hash(prev, created_at, user_id, username, role, action, resource, result, cid, meta)
        c.execute("INSERT INTO audit_log(created_at,user_id,username,role,action,resource,result,correlation_id,metadata,prev_hash,row_hash) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*row, prev, h))

    def record(self, action: str, result: str, correlation_id: str, *, actor: Optional[dict] = None,
               resource: str = "", metadata: Optional[dict] = None, conn: Optional[Any] = None) -> None:
        """Pass `conn` to write inside the caller's transaction so the audit row commits or rolls back with the change."""
        a = actor or {}
        row = (now_iso(), a.get("user_id"), a.get("username", "system"), a.get("role", "SYSTEM"), action, resource, result,
               correlation_id, json.dumps(scrub(metadata or {}), default=str))
        if conn is not None:
            self._append(conn, row)
            return
        for attempt in range(6):
            try:
                with self.db.tx() as c:
                    self._append(c, row)
                return
            except IntegrityError:          # chain fork lost a race (SQLite, separate processes): re-read the tip and retry
                if attempt == 5:
                    raise

    # ------------------------------------------------------------------ failure handling (Stage 2 WP6)
    @property
    def failures(self) -> AuditFailureState:
        return _state_of(self.db)

    def record_safely(self, action: str, result: str, correlation_id: str, *, actor: Optional[dict] = None,
                      resource: str = "", metadata: Optional[dict] = None) -> bool:
        """Like `record`, for an audit write that happens AFTER or BESIDE a trading operation whose outcome must not depend on it.
        Returns True when the event is durably written. On failure it returns False after holding the event (see AuditFailureState); it
        never raises. Use `record(..., conn=...)` instead when the audit row must commit with the change (it then rolls back with it)."""
        event_id = uuid.uuid4().hex
        meta = dict(metadata or {})
        meta["audit_event_id"] = event_id
        try:
            self.record(action, result, correlation_id, actor=actor, resource=resource, metadata=meta)
        except Exception as e:                                  # noqa: BLE001 - this is the one place an audit failure is caught, and it is recorded
            self._hold(dict(action=action, result=result, correlation_id=correlation_id, actor=actor, resource=resource, metadata=meta,
                            event_id=event_id, original_time=now_iso()), e)
            return False
        if self.failures.pending:
            self.flush_pending()                                # storage works again: write what was held, in order
        return True

    def _hold(self, event: dict, exc: BaseException) -> None:
        st = self.failures
        info = {"error_class": type(exc).__name__, "error_message": str(exc)[:200], "at": now_iso(), "action": event["action"],
                "traceback": "".join(traceback.format_exception_only(type(exc), exc)).strip()[:400]}
        with st.lock:
            event["failure"] = info
            st.failed_total += 1
            st.last_error = info
            st.first_failure_at = st.first_failure_at or info["at"]
            if len(st.pending) >= st.max_pending:
                st.pending.popleft()
                st.lost += 1
            st.pending.append(event)
        log.critical("AUDIT PERSISTENCE FAILED for %s (%s: %s); event held in memory, %d pending, %d lost. Trading operations are unaffected; "
                     "new risk-increasing orders are blocked until it is written.", event["action"], info["error_class"], info["error_message"],
                     len(st.pending), st.lost, exc_info=exc)

    def _already_written(self, event_id: str) -> bool:
        with self.db.read() as c:
            rows = c.execute("SELECT metadata FROM audit_log ORDER BY id DESC LIMIT ?", (_RECENT_ROWS_CHECKED,)).fetchall()
        return any(event_id in (r["metadata"] or "") for r in rows)

    def flush_pending(self) -> int:
        """Write held events in their original order, marked as late writes with the original failure. Idempotent: an event whose id is already
        in the log (the write committed but the caller saw an error) is not written twice. Stops at the first failure and keeps the rest.
        When the last held event is written a durable AUDIT_PERSISTENCE_RECOVERED row records the gap (counts, never the content)."""
        st = self.failures
        written = 0
        with st.lock:
            if not st.pending:
                return 0
            try:
                while st.pending:
                    ev = st.pending[0]
                    if not self._already_written(ev["event_id"]):
                        meta = dict(ev["metadata"], late_write=True, original_time=ev["original_time"], original_failure=ev["failure"])
                        self.record(ev["action"], ev["result"], ev["correlation_id"], actor=ev["actor"], resource=ev["resource"], metadata=meta)
                    st.pending.popleft()
                    st.flushed += 1
                    written += 1
            except Exception as e:                              # noqa: BLE001 - storage still failing: keep the rest, say so
                log.critical("audit flush stopped after %d event(s): %s: %s; %d still pending", written, type(e).__name__, str(e)[:200], len(st.pending))
                return written
            summary = {"flushed": st.flushed, "failed_total": st.failed_total, "lost": st.lost, "first_failure_at": st.first_failure_at}
            try:
                self.record("AUDIT_PERSISTENCE_RECOVERED", "OK" if not st.lost else "DEGRADED", "audit-recovery", resource="audit_log", metadata=summary)
            except Exception as e:                              # noqa: BLE001
                log.critical("audit recovery marker could not be written: %s", type(e).__name__)
            st.flushed, st.failed_total, st.first_failure_at = 0, 0, None
        return written

    def degraded(self) -> bool:
        st = self.failures
        with st.lock:
            return bool(st.pending) or st.lost > 0

    def status(self) -> dict:
        """What may and may not be claimed about audit durability right now (also reported by /health/detailed)."""
        st = self.failures
        with st.lock:
            return {"persistence": "DEGRADED" if (st.pending or st.lost) else "HEALTHY", "pending": len(st.pending), "lost": st.lost,
                    "failed_total": st.failed_total, "last_error": dict(st.last_error) if st.last_error else None}

    def acknowledge_lost(self, actor: dict, reason: str) -> int:
        """A human accepts that `lost` events can never be recovered. Audited. Returns how many were acknowledged."""
        if not actor or not actor.get("username") or not reason.strip():
            raise ValueError("acknowledging lost audit events needs a named actor and a reason")
        st = self.failures
        with st.lock:
            n = st.lost
            self.record("AUDIT_LOSS_ACKNOWLEDGED", "OK", "audit-recovery", actor=actor, resource="audit_log", metadata={"lost": n, "reason": reason[:200]})
            st.lost = 0
        return n

    def recent(self, limit: int = 100, action: Optional[str] = None) -> list[dict]:
        with self.db.read() as c:
            q, args = "SELECT * FROM audit_log", []
            if action:
                q += " WHERE action=?"
                args.append(action)
            q += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in c.execute(q, args)]

    def head(self) -> Optional[str]:
        with self.db.read() as c:
            r = c.execute("SELECT row_hash FROM audit_log WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return r["row_hash"] if r else None

    def verify_chain(self) -> dict:
        """Recompute every hash in id order. ok=False names the first row that does not match."""
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY id")]
        legacy = sum(1 for r in rows if r["row_hash"] is None)
        prev, checked = GENESIS, 0
        for r in rows:
            if r["row_hash"] is None:
                if checked:
                    return {"ok": False, "first_bad_id": r["id"], "reason": "unchained row after the chain started", "rows": checked, "legacy_rows": legacy, "head": prev}
                continue
            if r["prev_hash"] != prev:
                return {"ok": False, "first_bad_id": r["id"], "reason": "chain link broken (row removed, inserted or reordered)", "rows": checked, "legacy_rows": legacy, "head": prev}
            want = _hash(prev, r["created_at"], r["user_id"], r["username"], r["role"], r["action"], r["resource"], r["result"], r["correlation_id"], r["metadata"])
            if want != r["row_hash"]:
                return {"ok": False, "first_bad_id": r["id"], "reason": "row content was altered", "rows": checked, "legacy_rows": legacy, "head": prev}
            prev, checked = r["row_hash"], checked + 1
        return {"ok": True, "first_bad_id": None, "reason": "", "rows": checked, "legacy_rows": legacy, "head": prev if checked else None}
