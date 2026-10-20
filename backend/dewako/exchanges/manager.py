"""Exchange Manager: add, list and remove exchange connections.

Safety rules:
- Credentials are encrypted before storage and never returned (only masked).
- The key's permissions are read from the exchange. Unknown permissions or withdraw=True means the key is REJECTED and nothing is stored.
- The platform has no withdraw operation anywhere.
"""
from __future__ import annotations

import re

import itertools
from dataclasses import dataclass, field
from typing import Callable

from dewako.exchanges.base import ExchangeAdapter, ExchangeError
from dewako.exchanges.registry import REGISTRY
from dewako.services.credentials import CredentialVault, mask


_SECRETISH = re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passphrase|signature|key)\b(\s*[=:]\s*)\S+")


def _redact(text: str, creds: dict | None = None) -> str:
    """Mask supplied credential values and anything that looks like `key=...` in an exchange's error text."""
    for v in (creds or {}).values():
        if isinstance(v, str) and len(v) >= 4:
            text = text.replace(v, "***")
    return _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class ExchangeRejected(Exception):
    pass


@dataclass
class ExchangeRecord:
    id: int
    exchange: str
    label: str
    status: str  # CONNECTED
    permissions: dict[str, bool]
    api_key_masked: str
    blob: bytes = field(repr=False)

    def public(self) -> dict:
        return {"id": self.id, "exchange": self.exchange, "label": self.label, "status": self.status,
                "permissions": self.permissions, "api_key": self.api_key_masked}


class ExchangeManager:
    def __init__(self, vault: CredentialVault, factory: Callable[[str, dict], ExchangeAdapter] | None = None):
        self.vault = vault
        self._factory = factory or self._default_factory
        self._records: dict[int, ExchangeRecord] = {}
        self._ids = itertools.count(1)
        self.audit: list[dict] = []

    @staticmethod
    def _default_factory(exchange: str, creds: dict) -> ExchangeAdapter:
        cls = REGISTRY.get(exchange.lower())
        if cls is None:
            raise ExchangeRejected(f"unsupported exchange {exchange!r}. Supported: {sorted(REGISTRY)}")
        return cls(api_key=creds.get("api_key"), api_secret=creds.get("api_secret"),
                   passphrase=creds.get("passphrase"))

    async def add_exchange(self, exchange: str, label: str, api_key: str, api_secret: str,
                           passphrase: str = "", actor: str = "admin") -> ExchangeRecord:
        exchange = exchange.lower().strip()
        if exchange not in REGISTRY:
            raise ExchangeRejected(f"unsupported exchange {exchange!r}. Supported: {sorted(REGISTRY)}")
        if not api_key or not api_secret:
            raise ExchangeRejected("api_key and api_secret are required")
        if exchange == "okx" and not passphrase:
            raise ExchangeRejected("OKX requires a passphrase")
        creds = {"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}
        perms = await self._verify(exchange, creds, actor)
        rec = ExchangeRecord(id=next(self._ids), exchange=exchange, label=label or exchange, status="CONNECTED",
                             permissions={"read": True, "trade": bool(perms.get("trade")), "withdraw": False},
                             api_key_masked=mask(api_key), blob=self.vault.encrypt(creds))
        self._records[rec.id] = rec
        self._log("added", exchange, actor, f"id={rec.id}")
        return rec

    async def _verify(self, exchange: str, creds: dict, actor: str) -> dict:
        """Read the key's permissions from the exchange. Unknown or withdraw-enabled keys are rejected."""
        adapter = self._factory(exchange, creds)
        try:
            await adapter.connect()
            perms = await adapter.get_api_permissions()
        except ExchangeError as e:
            safe = _redact(str(e), creds)       # exchange error text may echo the key or secret
            self._log("rejected_unverified", exchange, actor, safe)
            raise ExchangeRejected(f"could not verify the key with {exchange}: {safe}") from None
        finally:
            try:
                await adapter.disconnect()
            except Exception:
                pass
        if perms.get("withdraw", True):  # missing key counts as enabled
            self._log("rejected_withdraw_enabled", exchange, actor, "")
            raise ExchangeRejected("this key has withdrawal permission. Create a new key with Withdraw disabled and try again")
        if not perms.get("read"):
            raise ExchangeRejected("this key cannot read account data")
        return perms

    def list(self) -> list[dict]:
        return [r.public() for r in self._records.values()]

    def remove(self, record_id: int, actor: str = "admin") -> None:
        rec = self._records.pop(record_id, None)
        if rec is None:
            raise KeyError(record_id)
        self._log("removed", rec.exchange, actor, f"id={record_id}")

    def credentials_for_adapter(self, record_id: int) -> dict[str, str]:
        """Server-internal only. Never expose through the API."""
        return self.vault.decrypt(self._records[record_id].blob)

    def _log(self, event: str, exchange: str, actor: str, detail: str) -> None:
        self.audit.append({"event": event, "exchange": exchange, "actor": actor, "detail": detail})
