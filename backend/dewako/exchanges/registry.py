from __future__ import annotations

import re

from dewako.exchanges.base import ExchangeAdapter
from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.contract import ADAPTER_ENVIRONMENTS
from dewako.exchanges.stubs import BybitAdapter, OKXAdapter

REGISTRY: dict[str, type[ExchangeAdapter]] = {
    "binance": BinanceAdapter,
    "bybit": BybitAdapter,
    "okx": OKXAdapter,
}
_BUILTIN = frozenset(REGISTRY)

# Code-reviewed allowlists. Registering an adapter can never grant these: a class that CLAIMS live or testnet verification is refused
# unless its name is listed here, and changing these tuples is a source change that goes through review. Both are empty.
LIVE_VERIFIED_ADAPTERS: frozenset[str] = frozenset()
TESTNET_VERIFIED_ADAPTERS: frozenset[str] = frozenset()

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class RegistrationRefused(Exception):
    pass


def register_adapter(name: str, cls: type, *, replace: bool = False, allow_test_double: bool = False) -> None:
    """Add an adapter class under `name`. Every check fails closed; registration never changes safety policy."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise RegistrationRefused("adapter name must match [a-z][a-z0-9_]{1,31}")
    if not (isinstance(cls, type) and issubclass(cls, ExchangeAdapter)):
        raise RegistrationRefused("adapter must be a subclass of ExchangeAdapter")
    if name in REGISTRY and (not replace or name in _BUILTIN):
        raise RegistrationRefused(f"adapter {name!r} is already registered" + (" (built-in adapters cannot be replaced)" if name in _BUILTIN else ""))
    if getattr(cls, "environment", None) not in ADAPTER_ENVIRONMENTS:
        raise RegistrationRefused(f"adapter environment must be one of {ADAPTER_ENVIRONMENTS}")
    try:
        caps = cls().capabilities()                       # reading the declaration must not need credentials or the network
    except Exception as e:
        raise RegistrationRefused(f"cannot read the adapter's declared capabilities without arguments ({type(e).__name__})") from None
    if caps.live_execution_verified and name not in LIVE_VERIFIED_ADAPTERS:
        raise RegistrationRefused("adapter claims live_execution_verified but is not on the reviewed LIVE_VERIFIED_ADAPTERS allowlist")
    if caps.testnet_verified and name not in TESTNET_VERIFIED_ADAPTERS:
        raise RegistrationRefused("adapter claims testnet_verified but is not on the reviewed TESTNET_VERIFIED_ADAPTERS allowlist")
    if caps.test_double and not allow_test_double:
        raise RegistrationRefused("test doubles bypass the live lock and can only be registered by tests (allow_test_double=True)")
    if caps.order_submission and not caps.lookup_by_client_id:
        raise RegistrationRefused("an adapter that submits orders must also look them up by client order id (ambiguous outcomes need it)")
    if caps.order_submission and not (caps.instrument_metadata and caps.account_snapshot):
        raise RegistrationRefused("an adapter that submits orders must declare instrument_metadata and account_snapshot")
    REGISTRY[name] = cls


def unregister_adapter(name: str) -> None:
    if name in _BUILTIN:
        raise RegistrationRefused("built-in adapters cannot be removed")
    REGISTRY.pop(name, None)
