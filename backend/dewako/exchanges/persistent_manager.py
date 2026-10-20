"""Database-backed exchange manager: exchanges and encrypted credentials survive restarts.

Rules kept from the in-memory manager (key permissions read from the exchange, withdraw=yes or unknown -> rejected),
plus: key versions, rotation, and a separate admin step to enable an exchange for LIVE. Enabling re-reads the
permissions from the exchange, so a key that gained Withdraw after it was added is caught.
"""
from __future__ import annotations

import json

from dewako.exchanges.manager import ExchangeManager, ExchangeRejected
from dewako.exchanges.registry import REGISTRY
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, now_iso
from dewako.services.credentials import CredentialVault, mask


class PersistentExchangeManager(ExchangeManager):
    def __init__(self, db: Database, vault: CredentialVault, factory=None, audit: AuditLog | None = None):
        super().__init__(vault, factory)
        self.db, self.audit_log = db, audit or AuditLog(db)

    async def add_exchange(self, exchange, label, api_key, api_secret, passphrase="", actor="admin", actor_info=None, correlation_id=""):
        exchange = exchange.lower().strip()
        if exchange not in REGISTRY:
            raise ExchangeRejected(f"unsupported exchange {exchange!r}. Supported: {sorted(REGISTRY)}")
        if not api_key or not api_secret:
            raise ExchangeRejected("api_key and api_secret are required")
        if exchange == "okx" and not passphrase:
            raise ExchangeRejected("OKX requires a passphrase")
        creds = {"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}
        perms = await self._verify(exchange, creds, actor)
        stored = {"read": True, "trade": bool(perms.get("trade")), "withdraw": False}
        ts = now_iso()
        with self.db.tx() as c:
            eid = c.execute("INSERT INTO exchanges(user_id,exchange,label,environment,enabled,permissions,verified_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            ((actor_info or {}).get("user_id"), exchange, label or exchange, "LIVE", 0, json.dumps(stored), ts, ts, ts)).lastrowid
            c.execute("INSERT INTO credentials(exchange_id,encrypted_blob,encryption_key_version,last4,withdrawal_permission,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                      (eid, self.vault.encrypt(creds), self.vault.current_version, api_key[-4:], 0, ts, ts))
            self.audit_log.record("CREDENTIAL_CREATED", "OK", correlation_id, actor=actor_info, resource=f"exchange:{eid}", metadata={"exchange": exchange}, conn=c)
        return {"id": eid, "exchange": exchange, "label": label or exchange, "status": "CONNECTED", "enabled": False, "permissions": stored, "api_key": mask(api_key)}

    def list(self) -> list[dict]:
        with self.db.read() as c:
            rows = c.execute("SELECT e.*, k.last4, k.encryption_key_version FROM exchanges e LEFT JOIN credentials k ON k.exchange_id=e.id AND k.id=(SELECT MAX(id) FROM credentials WHERE exchange_id=e.id) ORDER BY e.id").fetchall()
        return [{"id": r["id"], "exchange": r["exchange"], "label": r["label"], "environment": r["environment"], "enabled": bool(r["enabled"]),
                 "permissions": json.loads(r["permissions"]), "api_key": "•" * 8 + (r["last4"] or ""), "key_version": r["encryption_key_version"]} for r in rows]

    def remove(self, record_id, actor="admin", actor_info=None, correlation_id=""):
        with self.db.tx() as c:
            r = c.execute("SELECT exchange FROM exchanges WHERE id=?", (record_id,)).fetchone()
            if r is None:
                raise KeyError(record_id)
            c.execute("DELETE FROM credentials WHERE exchange_id=?", (record_id,))
            c.execute("UPDATE exchanges SET enabled=0, updated_at=? WHERE id=?", (now_iso(), record_id))
            self.audit_log.record("CREDENTIAL_REMOVED", "OK", correlation_id, actor=actor_info, resource=f"exchange:{record_id}", conn=c)

    def credentials_for_adapter(self, record_id: int) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT encrypted_blob, encryption_key_version FROM credentials WHERE exchange_id=? ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()
        if r is None:
            raise KeyError(record_id)
        return self.vault.decrypt(r["encrypted_blob"], r["encryption_key_version"])

    async def enable_for_live(self, record_id: int, actor_info: dict, correlation_id: str = "") -> None:
        """Re-verify with the exchange, then enable. Withdraw enabled (or unknowable) blocks it."""
        with self.db.read() as c:
            r = c.execute("SELECT exchange FROM exchanges WHERE id=?", (record_id,)).fetchone()
        if r is None:
            raise KeyError(record_id)
        creds = self.credentials_for_adapter(record_id)
        try:
            perms = await self._verify(r["exchange"], creds, actor_info.get("username", ""))
        except ExchangeRejected as e:
            with self.db.tx() as c:
                c.execute("UPDATE exchanges SET enabled=0, updated_at=? WHERE id=?", (now_iso(), record_id))
                if "withdrawal permission" in str(e):
                    c.execute("UPDATE credentials SET withdrawal_permission=1 WHERE exchange_id=?", (record_id,))
                self.audit_log.record("LIVE_ENABLED", "BLOCKED", correlation_id, actor=actor_info, resource=f"exchange:{record_id}", metadata={"why": "verification failed"}, conn=c)
            raise
        if not perms.get("trade"):
            raise ExchangeRejected("this key cannot trade")
        with self.db.tx() as c:
            c.execute("UPDATE exchanges SET enabled=1, permissions=?, verified_at=?, updated_at=? WHERE id=?",
                      (json.dumps({"read": True, "trade": True, "withdraw": False}), now_iso(), now_iso(), record_id))
            self.audit_log.record("EXCHANGE_ENABLED", "OK", correlation_id, actor=actor_info, resource=f"exchange:{record_id}", conn=c)

    async def rotate_credentials(self, record_id: int, api_key: str, api_secret: str, passphrase: str, actor_info: dict, correlation_id: str = "") -> None:
        """Replace the API key itself (new key must also pass the no-withdraw check)."""
        with self.db.read() as c:
            r = c.execute("SELECT exchange FROM exchanges WHERE id=?", (record_id,)).fetchone()
        if r is None:
            raise KeyError(record_id)
        creds = {"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}
        await self._verify(r["exchange"], creds, actor_info.get("username", ""))
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("UPDATE credentials SET encrypted_blob=?, encryption_key_version=?, last4=?, withdrawal_permission=0, updated_at=?, rotated_at=? WHERE exchange_id=?",
                      (self.vault.encrypt(creds), self.vault.current_version, api_key[-4:], ts, ts, record_id))
            c.execute("UPDATE exchanges SET verified_at=?, updated_at=? WHERE id=?", (ts, ts, record_id))
            self.audit_log.record("CREDENTIAL_ROTATED", "OK", correlation_id, actor=actor_info, resource=f"exchange:{record_id}", conn=c)

    def reencrypt_all(self, actor_info: dict | None = None, correlation_id: str = "") -> int:
        """Re-encrypt every credential under the newest vault key (after adding a new key version). Returns rows changed."""
        n = 0
        with self.db.tx() as c:
            for r in c.execute("SELECT id, encrypted_blob, encryption_key_version FROM credentials").fetchall():
                if r["encryption_key_version"] == self.vault.current_version:
                    continue
                blob, ver = self.vault.rotate(r["encrypted_blob"], r["encryption_key_version"])
                c.execute("UPDATE credentials SET encrypted_blob=?, encryption_key_version=?, rotated_at=? WHERE id=?", (blob, ver, now_iso(), r["id"]))
                n += 1
            self.audit_log.record("CREDENTIAL_ROTATED", "OK", correlation_id, actor=actor_info, resource="vault", metadata={"rows": n}, conn=c)
        return n
