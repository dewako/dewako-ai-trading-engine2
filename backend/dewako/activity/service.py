"""Read model for the Live Activity feed.

History: keyset pagination with one id cursor per source (opaque to the client). Merging by (ts, event_id) is correct because within a
source ids and timestamps rise together; a page re-reads at most the rows it did not emit, so nothing is skipped or repeated.
Stream: per-source id watermarks, so a reconnect with the last cursor resumes exactly after what the client saw (no gap, no replay), and
a late-arriving row with an older timestamp is still delivered because it has a higher id.
Bounds: page size <= 200, scan <= 2000 rows per request, <= 200 rows per source per poll. Read-only: nothing here writes to the database.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dewako.activity import events as E
from dewako.persistence.db import Database

MAX_LIMIT, SCAN_CAP, BATCH = 200, 2000, 200
SUBMITTING_GRACE = 60
SRC_KEYS = {"audit": "a", "order_events": "o", "reconciliation_events": "r"}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]+)?(Z|[+-]\d{2}:\d{2})?$")


class BadRequest(ValueError):
    pass


def encode_cursor(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")


def decode_cursor(s: Optional[str]) -> dict:
    """Strict: only keys a/o/r with non-negative ints, bounded length. Anything else is a 400, never a query."""
    if not s:
        return {}
    if len(s) > 200:
        raise BadRequest("cursor too long")
    try:
        d = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    except Exception:
        raise BadRequest("invalid cursor")
    if not isinstance(d, dict) or set(d) - set(SRC_KEYS.values()) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 2**53 for v in d.values()):
        raise BadRequest("invalid cursor")
    return d


class Filters:
    def __init__(self, category=None, severity=None, status=None, strategy=None, environment=None, instrument=None, source=None,
                 correlation_id=None, q=None, since=None, until=None, type=None):
        def lst(v, allowed=None):
            if v is None or v == "":
                return None
            items = [x.strip() for x in (v if isinstance(v, (list, tuple)) else str(v).split(",")) if x.strip()][:10]
            if allowed is not None and any(i not in allowed for i in items):
                raise BadRequest("unknown filter value")
            return set(items)
        self.category, self.severity = lst(category, E.CATEGORIES), lst(severity, E.SEVERITIES)
        self.source = lst(source, E.SOURCES)
        self.status, self.type = lst(status), lst(type)
        self.strategy, self.environment, self.instrument = (str(strategy)[:60] if strategy else None, str(environment)[:20] if environment else None,
                                                            str(instrument)[:30] if instrument else None)
        self.correlation_id = str(correlation_id)[:64] if correlation_id else None
        self.q = str(q).strip().lower()[:100] if q else None
        for n, v in (("since", since), ("until", until)):
            if v and not _ISO.match(str(v)):
                raise BadRequest(f"{n} must be an ISO-8601 time")
        self.since, self.until = (E.utc(since) if since else None), (E.utc(until) if until else None)

    def match(self, e: dict) -> bool:
        if self.category and e["category"] not in self.category: return False
        if self.severity and e["severity"] not in self.severity: return False
        if self.source and e["source"] not in self.source: return False
        if self.status and e["status"] not in self.status: return False
        if self.type and e["type"] not in self.type: return False
        if self.strategy and (e["strategy"] or "").lower() != self.strategy.lower(): return False
        if self.environment and e["environment"] != self.environment: return False
        if self.instrument and (e["instrument"] or "").upper() != self.instrument.upper(): return False
        if self.correlation_id and e["correlation_id"] != self.correlation_id: return False
        if self.since and e["ts"] < self.since: return False
        if self.until and e["ts"] > self.until: return False
        if self.q and self.q not in " ".join(str(e[k] or "") for k in ("summary", "type", "correlation_id", "strategy", "instrument", "related_id", "status")).lower(): return False
        return True


_SQL = {
    "audit": ("SELECT id, created_at, username, role, action, resource, result, correlation_id, metadata FROM audit_log", "id", E.from_audit),
    "order_events": ("SELECT oe.id, oe.order_id, oe.event_type, oe.previous_state, oe.new_state, oe.exchange_order_id, oe.error, oe.actor, oe.correlation_id, "
                     "oe.created_at, o.environment, o.symbol, o.exchange, o.side, o.filled, o.quantity, s.strategy_id, s.strategy_version, sv.fingerprint "
                     "FROM order_events oe JOIN orders o ON o.id = oe.order_id LEFT JOIN execution_intents i ON i.id = o.intent_id "
                     "LEFT JOIN signals s ON s.signal_id = i.signal_id LEFT JOIN strategy_versions sv ON sv.strategy_id = s.strategy_id AND sv.strategy_version = s.strategy_version",
                     "oe.id", E.from_order_event),
    "reconciliation_events": ("SELECT id, scope, previous_state, new_state, reason, actor, correlation_id, created_at FROM reconciliation_events", "id", E.from_recon_event),
}


class ActivityService:
    def __init__(self, db: Database):
        self.db = db

    def _fetch(self, src: str, *, below: Optional[int] = None, above: Optional[int] = None, limit: int = BATCH) -> list[tuple[int, Optional[dict]]]:
        sql, idcol, norm = _SQL[src]
        if above is not None:
            q, args = f"{sql} WHERE {idcol} > ? ORDER BY {idcol} ASC LIMIT ?", (above, limit)
        elif below is not None:
            q, args = f"{sql} WHERE {idcol} < ? ORDER BY {idcol} DESC LIMIT ?", (below, limit)
        else:
            q, args = f"{sql} ORDER BY {idcol} DESC LIMIT ?", (limit,)
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute(q, args).fetchall()]
        out = []
        for r in rows:
            try:
                out.append((int(r["id"]), norm(r)))
            except Exception:                       # a malformed stored row must not break the feed; it is skipped, not invented
                out.append((int(r["id"]), None))
        return out

    # ------------------------------------------------------------------ history
    def query(self, flt: Filters, *, limit: int = 50, cursor: Optional[str] = None) -> dict:
        limit = max(1, min(int(limit), MAX_LIMIT))
        cur = decode_cursor(cursor)
        pos = {s: cur.get(k) for s, k in SRC_KEYS.items()}          # exclusive upper id bound per source (None = from the newest)
        done: dict[str, bool] = {s: False for s in SRC_KEYS}
        cand: dict[str, list] = {s: [] for s in SRC_KEYS}           # matched events not yet emitted, per source, newest first
        scanned = 0
        truncated = False
        while scanned < SCAN_CAP:
            active = [s for s in SRC_KEYS if not done[s] and (not flt.source or s in flt.source)]
            have = sum(len(v) for v in cand.values())
            if not active or have > limit:
                break
            for s in active:
                rows = self._fetch(s, below=pos[s])
                scanned += len(rows)
                if len(rows) < BATCH:
                    done[s] = True
                for rid, ev in rows:
                    pos[s] = rid if pos[s] is None else min(pos[s], rid)
                    if ev is not None and flt.match(ev):
                        cand[s].append((rid, ev))
                if rows and flt.since:                               # everything older than `since` cannot match: stop this source
                    last = [ev for _, ev in rows if ev is not None]
                    if last and min(e["ts"] for e in last) < flt.since:
                        done[s] = True
        else:
            truncated = True
        merged = sorted(((ev, s, rid) for s, lst in cand.items() for rid, ev in lst), key=lambda t: E.sort_key(t[0]), reverse=True)
        page, rest = merged[:limit], merged[limit:]
        new_cur = dict(cur)
        for s, k in SRC_KEYS.items():
            mine = [rid for ev, src, rid in page if src == s]
            if mine:
                new_cur[k] = min(mine)
            elif not cand[s] and pos[s] is not None:
                new_cur[k] = pos[s]                                   # scanned rows, none matched: safe to move past them
        more = bool(rest) or any(not done[s] and (not flt.source or s in flt.source) for s in SRC_KEYS) or truncated
        seen, events = set(), []
        for ev, _, _ in page:
            if ev["event_id"] not in seen:
                seen.add(ev["event_id"]); events.append(ev)
        return {"schema_version": E.SCHEMA_VERSION, "events": events, "next_cursor": encode_cursor(new_cur) if more and page else None,
                "has_more": more, "scan_truncated": truncated}

    # ------------------------------------------------------------------ live stream
    def watermarks_now(self) -> dict:
        tables = {"audit": "audit_log", "order_events": "order_events", "reconciliation_events": "reconciliation_events"}
        with self.db.read() as c:
            return {SRC_KEYS[s]: int(c.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]) for s, t in tables.items()}

    def poll(self, marks: dict, flt: Optional[Filters] = None, limit: int = BATCH) -> tuple[list[dict], dict]:
        """Events with id above each watermark (oldest first, merged by time), and the advanced watermarks. Filtered-out rows still advance
        the watermark, so a quiet filter never makes the server re-read the same rows forever."""
        flt = flt or Filters()
        new = dict(marks)
        out = []
        for s, k in SRC_KEYS.items():
            if flt.source and s not in flt.source:
                continue
            for rid, ev in self._fetch(s, above=int(marks.get(k, 0)), limit=min(limit, BATCH)):
                new[k] = max(new.get(k, 0), rid)
                if ev is not None and flt.match(ev):
                    out.append(ev)
        out.sort(key=E.sort_key)
        return out, new

    # ------------------------------------------------------------------ status
    def status(self, health: Optional[dict] = None) -> dict:
        """Derived from stored records only. IDLE means nothing is reporting (no worker rows), not that anything is wrong."""
        with self.db.read() as c:
            workers = [dict(r) for r in c.execute("SELECT name, critical, status, last_heartbeat FROM worker_health").fetchall()]
            pend = [dict(r) for r in c.execute("SELECT state, updated_at FROM orders WHERE state IN ('SUBMITTING','UNKNOWN','RECONCILING')").fetchall()]
            inc = [dict(r) for r in c.execute("SELECT severity, kind FROM incidents WHERE status='OPEN'").fetchall()]
            start = c.execute("SELECT result, created_at FROM audit_log WHERE action='ENGINE_STARTUP' ORDER BY id DESC LIMIT 1").fetchone()
        now = datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SUBMITTING_GRACE)).isoformat()
        unresolved = sum(1 for p in pend if p["state"] in ("UNKNOWN", "RECONCILING") or E.utc(p["updated_at"]) < cutoff)   # a fresh SUBMITTING is just in flight
        h = health or {}
        reasons: list[str] = []
        level = "RUNNING"
        if not workers:
            level = "IDLE"; reasons.append("no worker is reporting")
        failed = [w["name"] for w in workers if w["status"] == "FAILED"]
        degraded = [w["name"] for w in workers if w["status"] == "DEGRADED" or (w["status"] != "HEALTHY" and w["critical"])]
        if degraded or failed:
            level = "DEGRADED"; reasons.append("workers not healthy: " + ", ".join(sorted(set(degraded + failed))[:5]))
        if start is not None and start["result"] != "OK":
            level = "DEGRADED"; reasons.append("last engine start-up was degraded")
        crit = [i for i in inc if i["severity"] == "CRITICAL"]
        if unresolved:
            level = "ERROR"; reasons.append(f"{unresolved} order(s) with unresolved outcome: reconciliation required")
        if crit:
            level = "ERROR"; reasons.append(f"{len(crit)} critical incident(s) open")
        if h.get("audit_log") == "TAMPERED":
            level = "ERROR"; reasons.append("audit chain verification failed")
        if h.get("database") == "unhealthy":
            level = "ERROR"; reasons.append("database unreachable")
        return {"level": level, "reasons": reasons, "unresolved_orders": int(unresolved), "open_incidents": len(inc),
                "kill_switch": h.get("kill_switch"), "live_trading": h.get("live_trading"), "trading_state": h.get("trading_state"),
                "reconciliation": h.get("reconciliation"), "audit_log": h.get("audit_log")}
