"""Audit log: scrubbed, hash-chained, append-only.

Each row stores prev_hash and row_hash = SHA-256 over (prev_hash, all fields). Altering, deleting or reordering a row breaks every later
hash, which `verify_chain()` finds. The database also refuses UPDATE/DELETE/TRUNCATE on audit_log (triggers). Limits, stated plainly:
a database superuser can drop the triggers and rewrite the whole chain, and removing the newest rows leaves a valid shorter chain. Real
tamper-evidence therefore needs the head hash (`head()`, also in /health/detailed) copied to a place the database owner cannot edit.
Rows written before migration 5 have no hashes; they are reported as `legacy_rows`, not verified.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from dewako.persistence.db import Database, IntegrityError, now_iso

_SECRET = re.compile(r"(secret|password|passwd|token|api_?key|passphrase|authorization|private)", re.I)
GENESIS = "0" * 64
_ADVISORY_KEY = 7_770_002


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
