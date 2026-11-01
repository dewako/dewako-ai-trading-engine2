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
        self.login_limiter = RateLimiter(int(os.environ.get("DEWAKO_LOGIN_RATE_LIMIT", "10")), 60)   # default 10/min; the knob exists for the browser e2e run only
        self._manager: Optional[PersistentExchangeManager] = None
        self._packages = None
        self.engine = self.recon = self.supervisor = self.application = None

    @property
    def packages(self):
        if self._packages is None:
            from dewako.strategy_packages.manager import PackageManager
            self._packages = PackageManager(self.db, self.audit)
        return self._packages

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


# Test seam only (same pattern as set_runtime): lets a test process build the Application with a deterministic, test-supplied market-data
# provider and clock. It is a Python-level hook: there is no route, header or environment variable that can set it, so a remote caller or an
# operator's configuration cannot use it to feed prices.
_application_factory = None


def set_application_factory(factory) -> None:
    global _application_factory
    _application_factory = factory


def get_application_factory():
    return _application_factory
