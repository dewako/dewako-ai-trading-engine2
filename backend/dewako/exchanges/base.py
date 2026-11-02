"""Exchange adapter contract. Exchange-specific logic lives only behind this interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from dewako.core.types import Order, OrderRequest


class ExchangeError(Exception):
    pass


class NotConnected(ExchangeError):
    pass


class RateLimited(ExchangeError):
    pass


class InsufficientBalance(ExchangeError):
    pass


class InvalidOrder(ExchangeError):
    pass


class OrderNotFound(ExchangeError):
    """The exchange answered and positively says it has no such order. Only this (never a timeout) proves absence."""


class NotImplementedForExchange(NotConnected):
    """Feature is not implemented for this exchange. Never pretend it worked."""


class OrderOutcomeUnknown(ExchangeError):
    """Raised on timeouts after submission. The order may exist on the exchange.

    Callers must reconcile (get_order by client_order_id / get_open_orders) and must never
    resubmit blindly.
    """


@dataclass
class AccountBalance:
    """One asset in an account. Margin fields are None unless the account type has them (spot has none)."""
    asset: str
    total: float
    available: float
    locked: float
    account_type: str = "SPOT"           # SPOT | MARGIN | PERPETUAL | FUTURES
    margin_balance: Optional[float] = None
    available_margin: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    position_margin: Optional[float] = None


@dataclass
class Fill:
    """One execution (trade) reported by an exchange. Never constructed by DEWAKO: only adapters build it from exchange data."""
    exchange_trade_id: str
    client_order_id: Optional[str]
    exchange_order_id: Optional[str]
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float = 0.0
    fee_asset: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass(frozen=True)
class AdapterCapabilities:
    """What an adapter ACTUALLY implements and has been verified for. Every flag defaults to False: an adapter must opt in.
    A flag is a claim that is backed by tests; `live_execution_verified` stays False until a real-exchange review says otherwise."""
    market_data: bool = False
    permissions_check: bool = False
    account_snapshot: bool = False
    instrument_metadata: bool = False
    open_orders: bool = False
    order_history: bool = False
    order_submission: bool = False
    lookup_by_order_id: bool = False
    lookup_by_client_id: bool = False
    cancellation: bool = False
    fill_history: bool = False
    spot_holdings: bool = False
    derivatives_positions: bool = False
    rate_limit_classification: bool = False
    testnet_available: bool = False          # the exchange offers one (a fact about the exchange)
    testnet_verified: bool = False           # this adapter has been run against it
    live_execution_verified: bool = False    # NEVER true in this build
    test_double: bool = False                # an in-process fake used only by tests; the live lock allows these (they cannot reach a real exchange)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def classify_error(exc: BaseException) -> str:
    """Map an exception to a coarse class so callers treat unknown outcomes conservatively."""
    if isinstance(exc, RateLimited):
        return "RATE_LIMITED"
    if isinstance(exc, OrderNotFound):
        return "NOT_FOUND"
    if isinstance(exc, InvalidOrder):
        return "REJECTED"
    if isinstance(exc, InsufficientBalance):
        return "REJECTED"
    if isinstance(exc, NotImplementedForExchange):
        return "UNSUPPORTED"
    if isinstance(exc, (OrderOutcomeUnknown, TimeoutError)):
        return "UNKNOWN_OUTCOME"
    if isinstance(exc, NotConnected):
        return "UNAVAILABLE"
    return "UNKNOWN_OUTCOME"        # anything unrecognised is NOT assumed to be a rejection


class ExchangeAdapter(ABC):
    name: str = "base"
    environment: str = "LIVE"  # PAPER adapters override. The execution layer refuses a mismatch.

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    async def get_api_permissions(self) -> dict[str, bool]:
        """Read what the connected API key is allowed to do, from the exchange itself.

        Returns {"read": bool, "trade": bool, "withdraw": bool}. Adapters must override this.
        The default refuses, so a key can never be accepted without its permissions being known.
        """
        raise NotConnected(f"{self.name} cannot report API key permissions")

    def capabilities(self) -> AdapterCapabilities:
        """Default: nothing is claimed. Adapters override and declare exactly what they implement."""
        return AdapterCapabilities()

    async def get_fills(self, since: Optional[str] = None) -> list[Fill]:
        """Trade/fill history from the exchange. Default refuses so reconciliation never pretends it compared fills."""
        raise NotImplementedForExchange(f"NOT_IMPLEMENTED: {self.name} cannot report fill history")

    async def get_order_history(self, symbol: Optional[str] = None) -> list[Order]:
        raise NotImplementedForExchange(f"NOT_IMPLEMENTED: {self.name} cannot report order history")

    async def get_account_snapshot(self) -> list[AccountBalance]:
        """Total / available / locked per asset as the exchange reports them. Adapters must implement this honestly; the default
        refuses, so an exchange that cannot report locked funds can never pass balance reconciliation."""
        raise NotImplementedForExchange(f"NOT_IMPLEMENTED: {self.name} cannot report an account snapshot")

    @abstractmethod
    async def get_balance(self) -> dict[str, float]: ...

    @abstractmethod
    async def get_markets(self) -> list[dict[str, Any]]:
        """Return normalized market metadata: symbol, tick_size, qty_precision, min_notional, fees."""

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Return normalized last, bid, ask and an aware ISO-8601/datetime timestamp."""

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict[str, list[list[float]]]: ...

    @abstractmethod
    async def create_order(self, req: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> Order: ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> Order: ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...
