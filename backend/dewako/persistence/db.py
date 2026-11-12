"""Database access for SQLite (development/tests) and PostgreSQL (the intended durable store).

`Database("path/to/file.db")` uses SQLite. `Database("postgresql://user:pw@host:5432/dbname")` uses PostgreSQL through
`pgwire`. Application SQL is written once with `?` placeholders in the portable subset; this module translates it.

Rules the application code relies on
- Writes happen inside `with db.tx() as c:` — all-or-nothing. Any exception rolls the whole thing back.
- Uniqueness is decided by the database. Code never does "check then insert"; it uses `ON CONFLICT DO NOTHING` / catches
  `IntegrityError` ONLY in a place where the transaction is allowed to be abandoned (PostgreSQL aborts a transaction after an error).
- Errors from the driver are mapped to `IntegrityError` / `DatabaseUnavailable` so callers never import a driver.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from dewako.core import timesource as _timesource
from dewako.persistence import pgwire
from dewako.persistence.migrations import MIGRATIONS, MIGRATION_EXTRA


def now_iso() -> str:
    return _timesource.now().isoformat()


class MigrationError(Exception):
    pass


class IntegrityError(Exception):
    """Unique / foreign-key / not-null / check violation (either database)."""


class DatabaseUnavailable(Exception):
    """The database cannot be reached or the connection broke. Callers must treat this as 'unknown' and fail closed."""


class Cursor:
    def __init__(self, rows: list, rowcount: int, lastrowid: Optional[int] = None):
        self._rows, self.rowcount, self.lastrowid, self._i = rows, rowcount, lastrowid, 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None

    def fetchall(self) -> list:
        r, self._i = self._rows[self._i:], len(self._rows)
        return r

    def __iter__(self):
        return iter(self.fetchall())


# ---------------------------------------------------------------- SQL translation
_ID_TABLE = re.compile(r"CREATE TABLE (\w+) \(\s*id\s", re.I)
ID_TABLES = {m.group(1) for _, _, sql in MIGRATIONS for m in _ID_TABLE.finditer(sql)}
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


def _qmarks(sql: str) -> str:
    """Replace ? with $1, $2... outside single-quoted literals."""
    out, n, inq = [], 0, False
    for ch in sql:
        if ch == "'":
            inq = not inq
        if ch == "?" and not inq:
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


_TS_COL = re.compile(r"\b(\w+_at|\w+_until|timestamp)\s+TEXT\b")


def to_pg_ddl(sql: str) -> str:
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    sql = _TS_COL.sub(lambda m: f"{m.group(1)} TIMESTAMPTZ", sql)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
    sql = re.sub(r"\bINTEGER\b", "BIGINT", sql)
    return sql


def split_statements(sql: str) -> list[str]:
    out, buf = [], []
    for line in sql.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]


# ---------------------------------------------------------------- connection wrappers
class SqliteConn:
    dialect = "sqlite"

    def __init__(self, raw: sqlite3.Connection):
        self.raw = raw

    def execute(self, sql: str, args: tuple | list = ()) -> Cursor:
        try:
            cur = self.raw.execute(sql, tuple(args))
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from None
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "unable to open" in str(e):
                raise DatabaseUnavailable(str(e)) from None
            raise
        rows = cur.fetchall() if cur.description else []
        return Cursor(rows, cur.rowcount, cur.lastrowid)

    def close(self) -> None:
        self.raw.close()

    @property
    def broken(self) -> bool:
        return False


class PgConn:
    dialect = "postgresql"

    def __init__(self, raw: pgwire.PGConnection):
        self.raw = raw

    def execute(self, sql: str, args: tuple | list = ()) -> Cursor:
        returning = False
        m = _INSERT.match(sql)
        if m and m.group(1).lower() in ID_TABLES and "RETURNING" not in sql.upper():
            sql, returning = sql.rstrip().rstrip(";") + " RETURNING id", True
        try:
            rows, count, _cols = self.raw.query(_qmarks(sql), tuple(args))
        except pgwire.PGIntegrityError as e:
            raise IntegrityError(str(e)) from None
        except pgwire.PGConnectionError as e:
            raise DatabaseUnavailable(str(e)) from None
        last = rows[0][0] if returning and rows else None
        return Cursor([] if returning else rows, count, last)

    def close(self) -> None:
        self.raw.close()

    @property
    def broken(self) -> bool:
        return self.raw.closed


class ConfigError(Exception):
    """Configuration that must not be guessed. The process refuses to start rather than fall back to something weaker."""


def database_target_from_env(environ=None) -> str:
    """Resolve the trading database. DEWAKO_ENV=production REQUIRES a PostgreSQL URL in DEWAKO_DATABASE_URL: it never falls back to
    SQLite, to a default file, or to DEWAKO_TRADING_DB. Development (default) may use a SQLite path."""
    import os
    e = environ if environ is not None else os.environ
    env = (e.get("DEWAKO_ENV") or "development").strip().lower()
    url = (e.get("DEWAKO_DATABASE_URL") or "").strip()
    if env == "production":
        if not url.startswith(("postgresql://", "postgres://")):
            raise ConfigError("DEWAKO_ENV=production requires DEWAKO_DATABASE_URL=postgresql://... (no SQLite fallback in production)")
        return url
    return url or e.get("DEWAKO_TRADING_DB", "dewako_trading.db")


# ---------------------------------------------------------------- database
class Database:
    _instances: "weakref.WeakSet[Database]" = weakref.WeakSet()

    @classmethod
    def close_all_idle(cls) -> int:
        """Close every idle pooled connection of every live Database object (the objects stay usable and reconnect on demand).
        Used by test runners so a long single-process run does not exhaust the server's connection limit."""
        n = 0
        for d in list(cls._instances):
            n += len(d._pool)
            d.close()
        return n

    def __init__(self, target: str, schema: Optional[str] = None, pool_size: int = 8):
        Database._instances.add(self)
        self.path = self.target = target          # `.path` kept for older callers
        self.is_pg = target.startswith(("postgresql://", "postgres://"))
        m = re.search(r"[?&]schema=([a-z_][a-z0-9_]*)", target)  # private schema (tests, multi-tenant dev); reopenable from the URL
        schema = schema or (m.group(1) if m else None)
        self.schema = schema
        self._pool: list[pgwire.PGConnection] = []
        self._pool_lock = threading.Lock()
        self._pool_size = pool_size
        if self.is_pg and schema:
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", schema):
                raise ValueError("invalid schema name")
            raw = self._new_pg(with_schema=False)
            try:
                raw.execute_script(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            finally:
                raw.close()
        self.migrate()

    @property
    def dialect(self) -> str:
        return "postgresql" if self.is_pg else "sqlite"

    # ---- connections
    def _new_pg(self, with_schema: bool = True) -> pgwire.PGConnection:
        c = pgwire.PGConnection(self.target, schema=(self.schema if with_schema else None))
        c.execute_script("SET TIME ZONE 'UTC'")
        return c

    def connect(self):
        if self.is_pg:
            with self._pool_lock:
                while self._pool:
                    raw = self._pool.pop()
                    if not raw.closed:
                        return PgConn(raw)
            try:
                return PgConn(self._new_pg())
            except pgwire.PGConnectionError as e:
                raise DatabaseUnavailable(str(e)) from None
        try:
            c = sqlite3.connect(self.target, timeout=30, isolation_level=None)
        except sqlite3.Error as e:
            raise DatabaseUnavailable(str(e)) from None
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        if self.target != ":memory:":
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
        return SqliteConn(c)

    def _release(self, conn) -> None:
        if not self.is_pg:
            conn.close()
            return
        raw = conn.raw
        if raw.closed or raw.status != b"I":
            raw.close()
            return
        with self._pool_lock:
            if len(self._pool) < self._pool_size:
                self._pool.append(raw)
                return
        raw.close()

    def _begin(self, sql: str):
        """A connection with its transaction started. A pooled PostgreSQL connection can be dead (server restart, network drop); nothing
        has been executed on it yet, so discarding it and trying once with a fresh connection is safe. If that also fails the caller gets
        DatabaseUnavailable and must treat the database as down (fail closed)."""
        for attempt in (0, 1):
            c = self.connect()
            try:
                c.execute(sql)
                return c
            except (pgwire.PGConnectionError, DatabaseUnavailable) as e:
                if self.is_pg:
                    c.raw.close()
                else:
                    self._release(c)
                if not self.is_pg or attempt == 1:
                    raise (e if isinstance(e, DatabaseUnavailable) else DatabaseUnavailable(str(e))) from None
            except BaseException:
                self._release(c)
                raise

    @contextmanager
    def tx(self) -> Iterator[Any]:
        """All-or-nothing write transaction. Any exception (even KeyboardInterrupt) rolls everything back."""
        c = self._begin("BEGIN IMMEDIATE" if not self.is_pg else "BEGIN")
        try:
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            self._release(c)

    @contextmanager
    def read(self) -> Iterator[Any]:
        c = self._begin("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY") if self.is_pg else self.connect()
        try:
            try:
                yield c
            finally:
                if self.is_pg:
                    try:
                        c.execute("ROLLBACK")
                    except Exception:
                        pass
        finally:
            self._release(c)

    def close(self) -> None:
        with self._pool_lock:
            for raw in self._pool:
                raw.close()
            self._pool.clear()

    def drop_schema(self) -> None:
        """Test helper: remove the private schema this Database was created with."""
        if self.is_pg and self.schema:
            self.close()
            raw = self._new_pg(with_schema=False)
            try:
                raw.execute_script(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
            finally:
                raw.close()

    # ---- migrations
    def migrate(self) -> list[int]:
        return self._migrate_pg() if self.is_pg else self._migrate_sqlite()

    def _migrate_sqlite(self) -> list[int]:
        applied: list[int] = []
        c = self.connect()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            done = {r["version"] for r in c.execute("SELECT version FROM schema_version")}
            known = {v for v, _, _ in MIGRATIONS}
            if done - known:
                raise MigrationError(f"database has migrations this code does not know: {sorted(done - known)}")
            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in split_statements(sql):
                        c.execute(stmt)
                    for stmt in MIGRATION_EXTRA.get(version, {}).get("sqlite", []):
                        c.execute(stmt)
                    c.execute("INSERT INTO schema_version VALUES (?,?,?)", (version, name, now_iso()))
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
                applied.append(version)
        finally:
            c.close()
        return applied

    def _migrate_pg(self) -> list[int]:
        """Each migration is ONE transaction (PostgreSQL DDL is transactional). A session-level advisory lock stops two
        processes migrating at the same moment."""
        applied: list[int] = []
        raw = self._new_pg()
        try:
            raw.query("SELECT pg_advisory_lock($1)", (7_770_001,))
            raw.execute_script("CREATE TABLE IF NOT EXISTS schema_version (version BIGINT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL)")
            done = {r[0] for r in raw.query("SELECT version FROM schema_version")[0]}
            known = {v for v, _, _ in MIGRATIONS}
            if done - known:
                raise MigrationError(f"database has migrations this code does not know: {sorted(done - known)}")
            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                raw.query("BEGIN")
                try:
                    for stmt in split_statements(to_pg_ddl(sql)):
                        raw.query(stmt)
                    for stmt in MIGRATION_EXTRA.get(version, {}).get("postgres", []):
                        raw.query(stmt)
                    raw.query("INSERT INTO schema_version VALUES ($1,$2,$3)", (version, name, now_iso()))
                    raw.query("COMMIT")
                except BaseException:
                    raw.query("ROLLBACK")
                    raise
                applied.append(version)
            raw.query("SELECT pg_advisory_unlock($1)", (7_770_001,))
        except pgwire.PGConnectionError as e:
            raise DatabaseUnavailable(str(e)) from None
        finally:
            raw.close()
        return applied

    def table_names(self) -> set[str]:
        with self.read() as c:
            if self.is_pg:
                return {r["table_name"] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")}
            return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
