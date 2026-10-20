"""Minimal PostgreSQL client (wire protocol v3) using only the standard library.

Why this exists: the sandbox this project was built in could not install psycopg (package index blocked by policy), and the
test suite must run against a REAL PostgreSQL server. In production prefer a maintained driver (psycopg 3); this module is
small, deliberately limited and tested against PostgreSQL 16, but it is not a general-purpose driver.

Supported: trust / cleartext / md5 / SCRAM-SHA-256 authentication, optional TLS (sslmode=require), the extended query protocol
with server-side parameter binding (no client-side string escaping), simple-query for DDL scripts, transactions, error
SQLSTATE codes. Not supported: COPY, LISTEN/NOTIFY, binary formats, prepared-statement caching, SCRAM channel binding.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import ssl
import struct
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse


class PGError(Exception):
    def __init__(self, message: str, sqlstate: str = "", fields: Optional[dict] = None):
        super().__init__(message)
        self.sqlstate, self.fields = sqlstate, fields or {}


class PGIntegrityError(PGError):
    """SQLSTATE class 23: unique / foreign key / not null / check violation."""


class PGConnectionError(PGError):
    pass


def parse_url(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError("not a postgresql:// URL")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    return {"host": u.hostname or "127.0.0.1", "port": u.port or 5432, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "dbname": (u.path or "/").lstrip("/") or "postgres",
            "sslmode": q.get("sslmode", "prefer" if False else "disable"), "options": q.get("options", "")}


class Row(tuple):
    """Tuple that also answers row['column'] and keys(), like sqlite3.Row."""
    _cols: tuple = ()

    def __new__(cls, values, cols):
        r = super().__new__(cls, values)
        r._cols = cols
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            try:
                return tuple.__getitem__(self, self._cols.index(k))
            except ValueError:
                raise KeyError(k) from None
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._cols)


def _decode(oid: int, raw: Optional[bytes]) -> Any:
    if raw is None:
        return None
    if oid in (20, 21, 23, 26):
        return int(raw)
    if oid in (700, 701, 1700):
        return float(raw)
    if oid == 16:
        return raw == b"t"
    if oid == 17:
        s = raw.decode()
        return bytes.fromhex(s[2:]) if s.startswith("\\x") else raw
    return raw.decode()


def _encode_param(v: Any) -> Optional[bytes]:
    if v is None:
        return None
    if isinstance(v, bool):
        return b"1" if v else b"0"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return b"\\x" + bytes(v).hex().encode()
    if isinstance(v, float):
        return repr(v).encode()
    return str(v).encode()


class PGConnection:
    def __init__(self, url: str, timeout: float = 30.0, schema: Optional[str] = None):
        p = parse_url(url)
        self.params, self.schema = p, schema
        self.status = b"I"  # I idle, T in transaction, E failed transaction
        self._closed = False
        try:
            self.sock = socket.create_connection((p["host"], p["port"]), timeout=timeout)
        except OSError as e:
            raise PGConnectionError(f"cannot connect to PostgreSQL at {p['host']}:{p['port']}: {e.__class__.__name__}") from None
        self.sock.settimeout(timeout)
        try:
            if p["sslmode"] in ("require", "verify-full", "verify-ca"):
                self._start_tls(p["sslmode"], p["host"])
            self._startup()
        except BaseException:
            self.close()
            raise

    # ---- low level io
    def _recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise PGConnectionError("connection closed by the server")
            buf += chunk
        return bytes(buf)

    def _read_msg(self) -> tuple[bytes, bytes]:
        t = self._recv(1)
        (ln,) = struct.unpack("!I", self._recv(4))
        return t, self._recv(ln - 4)

    def _send(self, t: bytes, payload: bytes = b"") -> None:
        self.sock.sendall(t + struct.pack("!I", len(payload) + 4) + payload)

    def _start_tls(self, mode: str, host: str) -> None:
        self.sock.sendall(struct.pack("!II", 8, 80877103))
        if self._recv(1) != b"S":
            raise PGConnectionError("server does not support TLS but sslmode requires it")
        ctx = ssl.create_default_context()
        if mode == "require":  # encrypt only; certificate is not verified in this mode (same as libpq 'require')
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        self.sock = ctx.wrap_socket(self.sock, server_hostname=host)

    # ---- startup + auth
    def _startup(self) -> None:
        p = self.params
        kv = {"user": p["user"], "database": p["dbname"], "client_encoding": "UTF8"}
        # Server-side bounds so that no wait (row/advisory lock, runaway statement, abandoned transaction) can block forever:
        # the wait fails with a clear SQLSTATE error and the caller fails closed. Overridable for tests / slow servers.
        opts = [f"-c lock_timeout={int(os.environ.get('DEWAKO_PG_LOCK_TIMEOUT_MS', '20000'))}",
                f"-c statement_timeout={int(os.environ.get('DEWAKO_PG_STATEMENT_TIMEOUT_MS', '60000'))}",
                f"-c idle_in_transaction_session_timeout={int(os.environ.get('DEWAKO_PG_IDLE_TX_TIMEOUT_MS', '120000'))}"]
        if self.schema:
            opts.append(f"-c search_path={self.schema}")
        kv["options"] = " ".join(opts)
        body = struct.pack("!I", 196608) + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in kv.items()) + b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        while True:
            t, d = self._read_msg()
            if t == b"R":
                (code,) = struct.unpack("!I", d[:4])
                if code == 0:
                    continue
                if code == 3:
                    self._send(b"p", p["password"].encode() + b"\0")
                elif code == 5:
                    inner = hashlib.md5(p["password"].encode() + p["user"].encode()).hexdigest()
                    self._send(b"p", b"md5" + hashlib.md5(inner.encode() + d[4:8]).hexdigest().encode() + b"\0")
                elif code == 10:
                    self._scram(d[4:])
                elif code in (11, 12):
                    continue
                else:
                    raise PGError(f"unsupported authentication method {code}")
            elif t == b"E":
                raise self._error(d)
            elif t == b"Z":
                self.status = d[:1]
                return
            # S (parameter status), K (backend key data), N (notice): ignored

    def _scram(self, mechs: bytes) -> None:
        if b"SCRAM-SHA-256" not in mechs:
            raise PGError("server offers no SCRAM-SHA-256")
        pw = self.params["password"]
        nonce = base64.b64encode(os.urandom(18)).decode()
        first_bare = f"n=,r={nonce}"
        msg = ("n,," + first_bare).encode()
        self._send(b"p", b"SCRAM-SHA-256\0" + struct.pack("!I", len(msg)) + msg)
        t, d = self._read_msg()
        if t == b"E":
            raise self._error(d)
        server_first = d[4:].decode()
        fields = dict(x.split("=", 1) for x in server_first.split(","))
        if not fields["r"].startswith(nonce):
            raise PGError("SCRAM nonce mismatch")
        salt, iters = base64.b64decode(fields["s"]), int(fields["i"])
        salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
        ckey = hmac.new(salted, b"Client Key", "sha256").digest()
        stored = hashlib.sha256(ckey).digest()
        final_no_proof = f"c=biws,r={fields['r']}"
        auth_msg = f"{first_bare},{server_first},{final_no_proof}".encode()
        sig = hmac.new(stored, auth_msg, "sha256").digest()
        proof = base64.b64encode(bytes(a ^ b for a, b in zip(ckey, sig))).decode()
        self._send(b"p", f"{final_no_proof},p={proof}".encode())
        t, d = self._read_msg()
        if t == b"E":
            raise self._error(d)
        skey = hmac.new(salted, b"Server Key", "sha256").digest()
        expect = base64.b64encode(hmac.new(skey, auth_msg, "sha256").digest()).decode()
        if f"v={expect}" != d[4:].decode():
            raise PGError("server signature mismatch (SCRAM)")

    # ---- errors
    @staticmethod
    def _error(d: bytes) -> PGError:
        f: dict[str, str] = {}
        for part in d.split(b"\0"):
            if part:
                f[chr(part[0])] = part[1:].decode(errors="replace")
        state = f.get("C", "")
        cls = PGIntegrityError if state.startswith("23") else PGError
        return cls(f.get("M", "database error"), state, f)

    # ---- queries
    def _drain_to_ready(self) -> None:
        while True:
            t, d = self._read_msg()
            if t == b"Z":
                self.status = d[:1]
                return

    def execute_script(self, sql: str) -> None:
        """Simple-query protocol: several statements, no parameters (used for migrations)."""
        self._send(b"Q", sql.encode() + b"\0")
        err = None
        while True:
            t, d = self._read_msg()
            if t == b"E" and err is None:
                err = self._error(d)
            elif t == b"Z":
                self.status = d[:1]
                break
        if err:
            raise err

    def query(self, sql: str, params: tuple | list = ()) -> tuple[list[Row], int, tuple]:
        """Run one statement with bound parameters. Returns (rows, rowcount, column_names)."""
        if self._closed:
            raise PGConnectionError("connection is closed")
        try:
            return self._query(sql, params)
        except (OSError, socket.timeout) as e:
            self.close()
            raise PGConnectionError(f"connection lost: {e.__class__.__name__}") from None

    def _query(self, sql: str, params) -> tuple[list[Row], int, tuple]:
        enc = [_encode_param(v) for v in params]
        parse = b"\0" + sql.encode() + b"\0" + struct.pack("!H", 0)
        bind = b"\0\0" + struct.pack("!H", 0) + struct.pack("!H", len(enc))
        for e in enc:
            bind += struct.pack("!i", -1) if e is None else struct.pack("!i", len(e)) + e
        bind += struct.pack("!H", 0)
        self.sock.sendall(b"".join(
            m[:1] + struct.pack("!I", len(m) - 1 + 4) + m[1:] for m in (
                b"P" + parse, b"B" + bind, b"D" + b"P\0", b"E" + b"\0" + struct.pack("!I", 0), b"S")))
        rows: list[Row] = []
        cols: tuple = ()
        oids: list[int] = []
        count = 0
        err: Optional[PGError] = None
        while True:
            t, d = self._read_msg()
            if t == b"T":
                (n,) = struct.unpack("!H", d[:2])
                pos, names, oids = 2, [], []
                for _ in range(n):
                    end = d.index(b"\0", pos)
                    names.append(d[pos:end].decode())
                    pos = end + 1
                    _tbl, _att, oid, _sz, _mod, _fmt = struct.unpack("!IHIhih", d[pos:pos + 18])
                    oids.append(oid)
                    pos += 18
                cols = tuple(names)
            elif t == b"D":
                (n,) = struct.unpack("!H", d[:2])
                pos, vals = 2, []
                for i in range(n):
                    (ln,) = struct.unpack("!i", d[pos:pos + 4])
                    pos += 4
                    if ln == -1:
                        vals.append(None)
                    else:
                        vals.append(_decode(oids[i], d[pos:pos + ln]))
                        pos += ln
                rows.append(Row(vals, cols))
            elif t == b"C":
                tag = d.rstrip(b"\0").decode().split()
                if tag and tag[-1].isdigit():
                    count = int(tag[-1])
            elif t == b"E":
                err = self._error(d)
            elif t == b"Z":
                self.status = d[:1]
                break
        if err:
            raise err
        return rows, count, cols

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.sock.sendall(b"X" + struct.pack("!I", 4))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed
