"""Stub adapters. Every trading method raises NotConnected until the real adapter is written (Phase 7).

Symbol mapping is already real so strategy code can be tested against the adapter surface.
"""
from __future__ import annotations

from dewako.core import symbols
from dewako.exchanges.base import ExchangeAdapter, NotConnected, NotImplementedForExchange


class _StubAdapter(ExchangeAdapter):
    def __init__(self, **_credentials):
        pass  # stubs never use credentials

    def _nope(self):
        raise NotImplementedForExchange(f"NOT_IMPLEMENTED: {self.name} adapter is a stub, this exchange is not implemented yet")

    def capabilities(self):
        from dewako.exchanges.base import AdapterCapabilities
        return AdapterCapabilities()          # a stub implements nothing

    def native(self, symbol: str) -> str:
        return symbols.to_native(symbol, self.name)

    async def connect(self): self._nope()
    async def disconnect(self): return None
    async def get_balance(self): self._nope()
    async def get_markets(self): self._nope()
    async def get_ticker(self, symbol): self._nope()
    async def get_orderbook(self, symbol, depth=20): self._nope()
    async def create_order(self, req): self._nope()
    async def cancel_order(self, symbol, order_id): self._nope()
    async def get_order(self, symbol, order_id=None, client_order_id=None): self._nope()
    async def get_positions(self): self._nope()
    async def get_open_orders(self, symbol=None): self._nope()


class BybitAdapter(_StubAdapter):
    name = "bybit"


class OKXAdapter(_StubAdapter):
    name = "okx"
