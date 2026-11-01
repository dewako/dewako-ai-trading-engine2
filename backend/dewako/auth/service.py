"""Authentication and role-based authorization. Framework independent, so it is testable without FastAPI.

- Passwords: scrypt with a per-user salt. Never stored or logged in plaintext.
- Sessions: random opaque tokens; only a SHA-256 hash is stored. Tokens expire and can be revoked.
- Lockout after repeated failures. Login failures give one generic message (no user enumeration).
- Authorization is a server-side permission table; UI visibility is irrelevant.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, IntegrityError, now_iso

ROLES = ("ADMIN", "TRADER", "ANALYST", "VIEWER")

PERMISSIONS: dict[str, set[str]] = {
    "VIEWER": {"read"},
    "ANALYST": {"read", "market_data", "strategies", "backtests", "reports"},
    "TRADER": {"read", "market_data", "strategies", "backtests", "reports", "paper_trading", "kill_switch_trigger", "trade_actions"},
    "ADMIN": {"read", "market_data", "strategies", "backtests", "reports", "paper_trading", "kill_switch_trigger", "trade_actions",
              "exchange_credentials", "risk_settings", "live_trading", "kill_switch_release", "user_management"},
}
MIN_PASSWORD = 12
SESSION_TTL = timedelta(hours=8)
MAX_FAILS, LOCK_FOR = 5, timedelta(minutes=15)
GENERIC_FAIL = "invalid username or password"


class AuthError(Exception):
    """Authentication failed (HTTP 401)."""


class Forbidden(Exception):
    """Authenticated but not allowed (HTTP 403)."""


class RateLimited(Exception):
    pass


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, s, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


class RateLimiter:
    def __init__(self, limit: int, per_seconds: float, clock=time.monotonic):
        self.limit, self.per, self.clock = limit, per_seconds, clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now, q = self.clock(), self._hits[key]
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.limit:
            raise RateLimited(f"too many requests; limit is {self.limit} per {int(self.per)}s")
        q.append(now)


class AuthService:
    def __init__(self, db: Database, audit: Optional[AuditLog] = None):
        self.db, self.audit = db, audit or AuditLog(db)

    # ---- users
    def create_user(self, actor: Optional[dict], username: str, password: str, role: str, correlation_id: str = "") -> int:
        first = self._user_count() == 0
        if not first:  # the very first user bootstraps the system; everyone after needs an ADMIN
            self.require(actor, "user_management", correlation_id)
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if first and role != "ADMIN":
            raise ValueError("the first user must be an ADMIN")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        if not username.strip() or len(username) > 64:
            raise ValueError("invalid username")
        ts = now_iso()
        with self.db.tx() as c:
            if first:
                # Atomic bootstrap: the "no users yet" decision is re-made INSIDE the transaction under a table lock (SQLite's BEGIN IMMEDIATE
                # already serialises writers), so concurrent first-user requests cannot each see an empty table and each create an ADMIN.
                if self.db.is_pg:
                    c.execute("LOCK TABLE users IN EXCLUSIVE MODE")
                if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
                    raise AuthError("bootstrap already done")
            try:
                uid = c.execute("INSERT INTO users(username,password_hash,role,created_at,updated_at) VALUES (?,?,?,?,?)",
                                (username.strip(), hash_password(password), role, ts, ts)).lastrowid
            except IntegrityError:
                raise ValueError("username already exists") from None
            self.audit.record("USER_CREATED", "OK", correlation_id, actor=actor, resource=f"user:{uid}", metadata={"role": role}, conn=c)
        return uid

    def _user_count(self) -> int:
        with self.db.read() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def set_role(self, actor: dict, user_id: int, role: str, correlation_id: str = "") -> None:
        self.require(actor, "user_management", correlation_id)
        if role not in ROLES:
            raise ValueError("bad role")
        with self.db.tx() as c:
            if actor.get("user_id") == user_id and role != "ADMIN":
                raise Forbidden("an admin cannot demote themselves")
            c.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, now_iso(), user_id))
            c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), user_id))  # old role must not live on
            self.audit.record("ROLE_CHANGED", "OK", correlation_id, actor=actor, resource=f"user:{user_id}", metadata={"role": role}, conn=c)

    def disable_user(self, actor: dict, user_id: int, correlation_id: str = "") -> None:
        self.require(actor, "user_management", correlation_id)
        with self.db.tx() as c:
            c.execute("UPDATE users SET status='DISABLED', updated_at=? WHERE id=?", (now_iso(), user_id))
            c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_iso(), user_id))
            self.audit.record("USER_DISABLED", "OK", correlation_id, actor=actor, resource=f"user:{user_id}", conn=c)

    # ---- login / sessions
    def login(self, username: str, password: str, correlation_id: str = "") -> str:
        now = datetime.now(timezone.utc)
        token = None
        # Failure bookkeeping (counters, lockout, audit) must COMMIT, so errors are raised only after the transaction ends.
        with self.db.tx() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if u is None:
                verify_password(password, "scrypt$AAAA$AAAA")  # keep timing similar
                self.audit.record("LOGIN", "FAILED", correlation_id, resource="user:unknown", conn=c)
            else:
                who = {"user_id": u["id"], "username": u["username"], "role": u["role"]}
                if u["status"] != "ACTIVE" or (u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now):
                    self.audit.record("LOGIN", "REFUSED", correlation_id, actor=who, conn=c)
                elif not verify_password(password, u["password_hash"]):
                    fails = u["failed_logins"] + 1
                    lock = (now + LOCK_FOR).isoformat() if fails >= MAX_FAILS else None
                    c.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (0 if lock else fails, lock, u["id"]))
                    self.audit.record("LOGIN", "FAILED", correlation_id, actor=who, conn=c)
                else:
                    token = secrets.token_urlsafe(32)
                    c.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (u["id"],))
                    c.execute("INSERT INTO sessions(user_id,token_hash,created_at,expires_at) VALUES (?,?,?,?)",
                              (u["id"], _hash_token(token), now.isoformat(), (now + SESSION_TTL).isoformat()))
                    self.audit.record("LOGIN", "OK", correlation_id, actor=who, conn=c)
        if token is None:
            raise AuthError(GENERIC_FAIL)
        return token

    def authenticate(self, token: str) -> dict:
        """Returns {'user_id','username','role'} or raises AuthError. The role is read from the DB every time."""
        if not token:
            raise AuthError("missing token")
        with self.db.read() as c:
            r = c.execute("SELECT s.expires_at, s.revoked_at, u.id, u.username, u.role, u.status FROM sessions s JOIN users u ON u.id=s.user_id "
                          "WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
        if r is None or r["revoked_at"] or r["status"] != "ACTIVE" or datetime.fromisoformat(r["expires_at"]) < datetime.now(timezone.utc):
            raise AuthError("invalid or expired token")
        return {"user_id": r["id"], "username": r["username"], "role": r["role"]}

    def logout(self, token: str, correlation_id: str = "") -> None:
        try:
            actor = self.authenticate(token)
        except AuthError:
            return
        with self.db.tx() as c:
            c.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now_iso(), _hash_token(token)))
            self.audit.record("LOGOUT", "OK", correlation_id, actor=actor, conn=c)

    # ---- authorization
    def require(self, actor: Optional[dict], permission: str, correlation_id: str = "") -> dict:
        if not actor:
            raise AuthError("authentication required")
        if permission not in PERMISSIONS.get(actor.get("role", ""), set()):
            self.audit.record("ACCESS_DENIED", "DENIED", correlation_id, actor=actor, resource=permission)
            raise Forbidden(f"role {actor.get('role')} lacks permission {permission!r}")
        return actor
