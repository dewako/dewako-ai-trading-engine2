"""Process-wide services for the API, built lazily from environment configuration. No secrets are logged or returned."""
from __future__ import annotations

import os
from typing import Optional

from dewako.auth.service import AuthService, RateLimiter
from dewako.exchanges.persistent_manager import PersistentExchangeManager
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, database_target_from_env
from dewako.safety.controller import SafetyController
from dewako.services.credentials import CredentialVault, VaultError


class Runtime:
    def __init__(self, db_path: Optional[str] = None):
        self.db = Database(db_path or database_target_from_env())
        self.audit = AuditLog(self.db)
        self.auth = AuthService(self.db, self.audit)
        self.safety = SafetyController(self.db, self.audit)
        self.limiter = RateLimiter(int(os.environ.get("DEWAKO_RATE_LIMIT", "120")), 60)
        self.login_limiter = RateLimiter(10, 60)
        self._manager: Optional[PersistentExchangeManager] = None
        self.engine = self.recon = self.supervisor = self.application = None

    def manager(self) -> PersistentExchangeManager:
        if self._manager is None:
            self._manager = PersistentExchangeManager(self.db, CredentialVault(), audit=self.audit)  # raises VaultError if no key
        return self._manager


_rt: Optional[Runtime] = None


def get_runtime() -> Runtime:
    global _rt
    if _rt is None:
        _rt = Runtime()
    return _rt


def set_runtime(rt: Optional[Runtime]) -> None:
    global _rt
    _rt = rt


def get_runtime_if_set() -> Optional[Runtime]:
    return _rt
