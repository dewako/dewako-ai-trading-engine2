"""Exchange-agnostic adapter contract (Phase 4.1).

This module adds NO new way to reach an exchange. It only makes the existing adapter surface explicit and checkable:
  * which operation needs which declared capability (an adapter may not be asked to do what it did not declare),
  * which environments exist and which of them the execution engine may execute in,
  * one normalised error object with a conservative retry class and credential redaction.
The execution engine remains the only caller of create_order/cancel_order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from dewako.exchanges.base import (AdapterCapabilities, ExchangeAdapter, NotImplementedForExchange, classify_error)

# An adapter may DECLARE any of these. The engine EXECUTES only in ORDER_ENVIRONMENTS_ENABLED. TESTNET is deliberately absent: the
# database restricts order/position/trade environments to PAPER and LIVE (8 CHECK constraints) and no testnet adapter has been verified.
ADAPTER_ENVIRONMENTS = ("PAPER", "TESTNET", "LIVE")
ORDER_ENVIRONMENTS_ENABLED = ("PAPER", "LIVE")

# operation -> the capability flag that must be True before the operation may be requested from an adapter
OPERATION_CAPABILITY = {
    "get_ticker": "market_data",
    "get_orderbook": "market_data",
    "get_markets": "instrument_metadata",
    "get_balance": "account_snapshot",
    "get_account_snapshot": "account_snapshot",
    "get_open_orders": "open_orders",
    "get_order_history": "order_history",
    "create_order": "order_submission",
    "cancel_order": "cancellation",
    "get_order_by_order_id": "lookup_by_order_id",
    "get_order_by_client_order_id": "lookup_by_client_id",
    "get_fills": "fill_history",
    "get_positions": "spot_holdings",
    "get_api_permissions": "permissions_check",
}
MUTATING_OPERATIONS = frozenset({"create_order", "cancel_order"})


def supports(adapter: ExchangeAdapter, operation: str) -> bool:
    """True only if the adapter DECLARES the capability. Unknown operations are unsupported (fail closed)."""
    flag = OPERATION_CAPABILITY.get(operation)
    return bool(flag) and bool(getattr(adapter.capabilities(), flag, False))


def require(adapter: ExchangeAdapter, operation: str) -> None:
    if not supports(adapter, operation):
        raise NotImplementedForExchange(f"NOT_IMPLEMENTED: {adapter.name} does not declare the capability for {operation}")


def refusals_for_submission(adapter: ExchangeAdapter, environment: str) -> list[str]:
    """Everything about an adapter's declared surface that must hold before an ORDER may be sent through it (all environments).
    The live lock (verified / test double) is separate and unchanged; this is in addition to it."""
    why: list[str] = []
    if adapter.environment not in ADAPTER_ENVIRONMENTS:
        why.append(f"adapter {adapter.name} declares unknown environment {adapter.environment!r}")
        return why
    caps = adapter.capabilities()
    if not caps.order_submission:
        why.append(f"adapter {adapter.name} does not declare order_submission")
    if not caps.instrument_metadata:
        why.append(f"adapter {adapter.name} does not declare instrument_metadata: exchange rules cannot be validated")
    if not caps.account_snapshot:
        why.append(f"adapter {adapter.name} does not declare account_snapshot: balances cannot be checked")
    if environment != "PAPER" and not caps.lookup_by_client_id:
        why.append(f"adapter {adapter.name} cannot look an order up by client order id, so an ambiguous submission could never be resolved")
    return why


class RetryClass(str, Enum):
    NO_RETRY = "NO_RETRY"                 # permanent or definitive (rejected, unsupported, positively absent)
    BACKOFF_RETRY = "BACKOFF_RETRY"       # a READ may simply be repeated after a delay
    RECONCILE_FIRST = "RECONCILE_FIRST"   # a WRITE with an unknown outcome: look it up; never resend blindly


_SECRETISH = re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passphrase|signature|key)\b(\s*[=:]\s*)\S+")


def redact(text: str, creds: Optional[dict] = None) -> str:
    """Mask supplied credential values and anything that looks like `key=...` in exchange error text."""
    for v in (creds or {}).values():
        if isinstance(v, str) and len(v) >= 4:
            text = text.replace(v, "***")
    return _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


@dataclass(frozen=True)
class NormalizedError:
    code: str                    # RATE_LIMITED | NOT_FOUND | REJECTED | UNSUPPORTED | UNKNOWN_OUTCOME | UNAVAILABLE
    retry: RetryClass
    outcome_known: bool          # True only when the exchange definitively did / did not act
    message: str                 # sanitised, bounded
    operation: str
    adapter: str
    correlation_id: str = ""


def normalize_error(exc: BaseException, *, operation: str, adapter: str, correlation_id: str = "", creds: Optional[dict] = None) -> NormalizedError:
    """Conservative classification. For WRITES only definitive answers (rejected / unsupported / positively not found) are final;
    everything else, including rate limits and unrecognised errors, means 'the order may exist': reconcile before any retry."""
    code = classify_error(exc)
    mutating = operation in MUTATING_OPERATIONS
    if code in ("REJECTED", "UNSUPPORTED", "NOT_FOUND"):
        retry, known = RetryClass.NO_RETRY, True
    elif mutating:
        retry, known = RetryClass.RECONCILE_FIRST, False
    else:
        retry, known = RetryClass.BACKOFF_RETRY, False
    return NormalizedError(code, retry, known, redact(str(exc), creds)[:300], operation, adapter, correlation_id)
