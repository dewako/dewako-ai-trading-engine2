"""Order engine. Accepts only approved RiskDecisions and never resubmits blindly."""
from __future__ import annotations

import hashlib

from dewako.core.types import Order, OrderRequest, OrderStatus, OrderType, RiskDecision, Side
from dewako.exchanges.base import ExchangeAdapter, ExchangeError, OrderOutcomeUnknown


class OrderRejected(Exception):
    pass


class OrderEngine:
    def __init__(self, adapter: ExchangeAdapter, bot_id: str = ""):
        self.adapter = adapter
        self.bot_id = bot_id
        self._seen: dict[str, Order] = {}

    @staticmethod
    def client_id(decision: RiskDecision, bot_id: str) -> str:
        s = decision.signal
        raw = f"{bot_id}|{s.symbol}|{s.side.value}|{s.strategy}|{s.strategy_version}|{s.timestamp.isoformat()}"
        return "dw-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    async def submit(self, decision: RiskDecision) -> Order:
        if not decision.approved or decision.quantity <= 0:
            raise OrderRejected("order engine only accepts approved risk decisions")
        cid = self.client_id(decision, self.bot_id)
        if cid in self._seen:  # duplicate signal delivery: return the existing order
            return self._seen[cid]
        s = decision.signal
        req = OrderRequest(symbol=s.symbol, side=s.side, type=OrderType.MARKET, quantity=decision.quantity,
                           client_order_id=cid, strategy=s.strategy, bot_id=self.bot_id)
        try:
            order = await self.adapter.create_order(req)
        except OrderOutcomeUnknown:
            order = await self._reconcile(req)
        self._seen[cid] = order
        return order

    async def _reconcile(self, req: OrderRequest) -> Order:
        """Ask the exchange what happened. Do not resend."""
        try:
            return await self.adapter.get_order(req.symbol, client_order_id=req.client_order_id)
        except ExchangeError:
            # Still unknown. Surface it as UNKNOWN so a human or the reconciler job resolves it.
            return Order(exchange_order_id=None, client_order_id=req.client_order_id, symbol=req.symbol,
                         side=req.side, type=req.type, quantity=req.quantity, status=OrderStatus.UNKNOWN)
