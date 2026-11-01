"""Paper exchange. Implements the same adapter contract as a real exchange. No real money."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any, Optional

from dewako.core.types import Order, OrderRequest, OrderStatus, OrderType, Side
from dewako.exchanges.base import AccountBalance, ExchangeAdapter, InsufficientBalance, InvalidOrder, OrderNotFound


class PaperAdapter(ExchangeAdapter):
    name = "paper"
    environment = "PAPER"

    def __init__(self, balance: float = 10_000.0, fee_rate: float = 0.001, slippage: float = 0.0005,
                 min_notional: float = 5.0):
        self.cash = balance
        self.fee_rate, self.slippage, self.min_notional = fee_rate, slippage, min_notional
        self.prices: dict[str, float] = {}
        self.price_as_of: dict[str, datetime] = {}      # when each price was observed: a quote is only as fresh as its price
        self.holdings: dict[str, float] = {}
        self.orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self.connected = False

    def capabilities(self):
        from dewako.exchanges.base import AdapterCapabilities
        return AdapterCapabilities(market_data=True, account_snapshot=True, instrument_metadata=True, open_orders=True, order_submission=True,
                                   lookup_by_order_id=True, lookup_by_client_id=True, cancellation=True, spot_holdings=True)

    def set_price(self, symbol: str, price: float, as_of: Optional[datetime] = None) -> None:
        self.prices[symbol] = price
        self.price_as_of[symbol] = as_of or datetime.now(timezone.utc)

    async def connect(self): self.connected = True
    async def disconnect(self): self.connected = False
    async def get_balance(self): return {"USDT": self.cash}
    async def get_account_snapshot(self): return [AccountBalance("USDT", self.cash, self.cash, 0.0)]
    async def get_markets(self): return [{"symbol": s, "tick_size": 0.01, "qty_precision": 5,
                                          "min_notional": self.min_notional, "fee": self.fee_rate} for s in self.prices]
    async def get_ticker(self, symbol):
        price = self.prices[symbol]
        return {"last": price, "bid": price * (1 - self.slippage), "ask": price * (1 + self.slippage),
                "timestamp": self.price_as_of.get(symbol) or datetime.now(timezone.utc)}
    async def get_orderbook(self, symbol, depth=20):
        p = self.prices[symbol]
        return {"bids": [[p * (1 - 0.0004 * i), 1.0] for i in range(1, depth + 1)],
                "asks": [[p * (1 + 0.0004 * i), 1.0] for i in range(1, depth + 1)]}

    async def create_order(self, req: OrderRequest) -> Order:
        if req.client_order_id in self.orders:  # idempotent on client id
            return self.orders[req.client_order_id]
        if req.symbol not in self.prices:
            raise InvalidOrder(f"no market {req.symbol}")
        if req.type is not OrderType.MARKET:
            raise InvalidOrder("paper adapter supports MARKET orders only in Phase 1")
        px = self.prices[req.symbol] * (1 + self.slippage if req.side is Side.BUY else 1 - self.slippage)
        notional = px * req.quantity
        if notional < self.min_notional:
            raise InvalidOrder("below minimum notional")
        fee = notional * self.fee_rate
        if req.side is Side.BUY:
            if self.cash < notional + fee:
                raise InsufficientBalance("insufficient paper balance")
            self.cash -= notional + fee
            self.holdings[req.symbol] = self.holdings.get(req.symbol, 0) + req.quantity
        else:
            if self.holdings.get(req.symbol, 0.0) + 1e-12 < req.quantity:
                raise InvalidOrder("insufficient paper holdings: short selling is not supported")
            self.cash += notional - fee
            self.holdings[req.symbol] = self.holdings.get(req.symbol, 0) - req.quantity
        o = Order(exchange_order_id=f"P{next(self._ids)}", client_order_id=req.client_order_id, symbol=req.symbol,
                  side=req.side, type=req.type, quantity=req.quantity, filled=req.quantity, price=px,
                  status=OrderStatus.FILLED)
        self.orders[req.client_order_id] = o
        return o

    async def cancel_order(self, symbol, order_id):
        raise InvalidOrder("market orders fill immediately in paper mode")

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        for o in self.orders.values():
            if o.client_order_id == client_order_id or o.exchange_order_id == order_id:
                return o
        raise OrderNotFound("order not found")

    async def get_positions(self) -> list[dict[str, Any]]:
        return [{"symbol": s, "quantity": q} for s, q in self.holdings.items() if q]

    async def get_open_orders(self, symbol=None): return []
