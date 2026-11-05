"""Simulated exchange for replay and paper qualification (Stage 3A, step 2).

Implements the normal ExchangeAdapter contract, so the real ExecutionEngine, Risk Engine and ExitMonitor run unchanged against it. It is
pessimistic on purpose: when a number is unknown, it errs against the strategy.

Model (all values deterministic; no wall clock, no `random`):
* QUOTE   mid is supplied by the harness together with the bar it belongs to. half-spread = max(min_half_spread, spread_range_factor x bar range).
          bid = mid x (1 - half-spread), ask = mid x (1 + half-spread). The quote timestamp is the harness's virtual time.
* FILL    market BUY fills at ask x (1 + slippage), SELL at bid x (1 - slippage), where
          slippage = slippage_base + slippage_range_factor x bar range + impact_k x (order quantity / bar volume).
          A gap through a stop is therefore filled at the gapped quote, never at the stop price.
* FEE     fee_rate x notional of every fill.
* SIZE    at most max_participation x bar volume can fill per bar (all orders together); the rest stays open as a PARTIAL order and fills,
          oldest order first, on later price points. Zero bar volume fills nothing.
* LATENCY an order is accepted at once, but cannot fill before accept time + latency_seconds.
* RULES   quantity is rounded DOWN to the step; below min quantity or min notional, or above max quantity, is rejected.
* REJECTS maintenance windows reject every order; reject_rate rejects a deterministic, seeded share of client order ids.
* OUTAGE  inside an outage window every data and order call raises ConnectionError (nothing is accepted).
* LOST RESPONSE inside such a window an order IS accepted (and may fill) but the caller gets a TimeoutError: the outcome is unknown to the
          caller and must be reconciled, never resent.
* IDEMPOTENT a repeated client order id returns the original order.

Every fill is kept in `self.fills` with its mid, spread and slippage so execution cost can be reported. Nothing here contacts an exchange.
"""
from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Iterable, Optional

from dewako.core.types import Candle, Order, OrderRequest, OrderStatus, OrderType, Side
from dewako.exchanges.base import (AccountBalance, AdapterCapabilities, ExchangeAdapter, InsufficientBalance, InvalidOrder, OrderNotFound)


def _nonneg(name: str, v: float, hi: float = 1.0) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= hi:
        raise ValueError(f"{name} must be a finite number in [0, {hi}]")


@dataclass(frozen=True)
class SimConfig:
    fee_rate: float = 0.001
    min_half_spread: float = 0.0001
    spread_range_factor: float = 0.05
    slippage_base: float = 0.0001
    slippage_range_factor: float = 0.05
    impact_k: float = 0.5
    max_participation: float = 0.10
    latency_seconds: float = 0.0
    reject_rate: float = 0.0
    seed: int = 0
    step_size: float = 0.00001
    min_qty: float = 0.00001
    max_qty: float = 1_000_000.0
    min_notional: float = 5.0
    outage_windows: tuple[tuple[datetime, datetime], ...] = ()
    reject_windows: tuple[tuple[datetime, datetime], ...] = ()
    lost_response_windows: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        for n in ("fee_rate", "min_half_spread", "slippage_base", "reject_rate"):
            _nonneg(n, getattr(self, n), 1.0)
        for n in ("spread_range_factor", "slippage_range_factor", "impact_k"):
            _nonneg(n, getattr(self, n), 100.0)
        if isinstance(self.max_participation, bool) or not (0 < self.max_participation <= 1):
            raise ValueError("max_participation must be in (0, 1]")
        _nonneg("latency_seconds", self.latency_seconds, 3600.0)
        for n in ("step_size", "min_qty", "min_notional"):
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{n} must be positive and finite")
        if self.max_qty < self.min_qty:
            raise ValueError("max_qty must not be below min_qty")
        for label, ws in (("outage_windows", self.outage_windows), ("reject_windows", self.reject_windows),
                          ("lost_response_windows", self.lost_response_windows)):
            for a, b in ws:
                if a.tzinfo is None or b.tzinfo is None or b <= a:
                    raise ValueError(f"{label}: each window needs timezone-aware start < end")


@dataclass(frozen=True)
class SimFill:
    client_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    at: datetime
    mid: float
    half_spread: float
    slippage: float
    bar_time: datetime


@dataclass
class _Open:
    req: OrderRequest
    exchange_order_id: str
    accepted_at: datetime
    ready_at: datetime
    seq: int
    filled: float = 0.0
    cost: float = 0.0           # sum of price x quantity of fills (for the average price)
    status: OrderStatus = OrderStatus.NEW
    note: str = ""


@dataclass
class _Point:
    mid: float
    as_of: datetime
    bar: Candle


class SimulatedExchange(ExchangeAdapter):
    name = "sim"
    environment = "PAPER"
    SIMULATED = True

    def __init__(self, clock: Callable[[], datetime], balance: float = 10_000.0, config: Optional[SimConfig] = None, quote_asset: str = "USDT"):
        self.clock = clock
        self.cfg = config or SimConfig()
        self.cash = float(balance)
        self.quote_asset = quote_asset
        self.holdings: dict[str, float] = {}
        self.fills: list[SimFill] = []
        self.orders: dict[str, _Open] = {}
        self._points: dict[str, _Point] = {}
        self._bar_used: dict[tuple[str, datetime], float] = {}
        self._ids, self._seq = itertools.count(1), itertools.count(1)
        self.connected = False

    # ---------------------------------------------------------------- harness side
    def set_market(self, symbol: str, mid: float, as_of: datetime, bar: Candle) -> list[SimFill]:
        """Publish a price point (mid) taken from `bar` at virtual time `as_of`, then let waiting orders fill. Returns the new fills."""
        if isinstance(mid, bool) or not isinstance(mid, (int, float)) or not math.isfinite(mid) or mid <= 0:
            raise ValueError("mid must be finite and positive")
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        self._points[symbol] = _Point(float(mid), as_of, bar)
        return self._advance(symbol)

    def _range_pct(self, bar: Candle) -> float:
        return max(0.0, (bar.h - bar.l) / bar.c)

    def _half_spread(self, bar: Candle) -> float:
        return max(self.cfg.min_half_spread, self.cfg.spread_range_factor * self._range_pct(bar))

    def half_spread(self, bar: Candle) -> float:
        """The half-spread this venue would quote for `bar` (the replay uses it to place price points where a bid-based trigger fires)."""
        return self._half_spread(bar)

    def _in(self, windows: Iterable[tuple[datetime, datetime]]) -> bool:
        now = self.clock()
        return any(a <= now < b for a, b in windows)

    def _check_up(self) -> None:
        if self._in(self.cfg.outage_windows):
            raise ConnectionError("simulated exchange outage")

    # ---------------------------------------------------------------- fills
    def _advance(self, symbol: str) -> list[SimFill]:
        p = self._points.get(symbol)
        out: list[SimFill] = []
        if p is None:
            return out
        for o in sorted((x for x in self.orders.values() if x.req.symbol == symbol and x.status in (OrderStatus.NEW, OrderStatus.PARTIAL)),
                        key=lambda x: x.seq):
            if p.as_of < o.ready_at:
                continue
            f = self._fill(o, p)
            if f is not None:
                out.append(f)
        return out

    def _fill(self, o: _Open, p: _Point) -> Optional[SimFill]:
        remaining = o.req.quantity - o.filled
        bar = p.bar
        used_key = (o.req.symbol, bar.t)
        cap_left = self.cfg.max_participation * max(bar.v, 0.0) - self._bar_used.get(used_key, 0.0)
        qty = self._round_down(min(remaining, cap_left))
        if qty <= 0:
            return None
        half = self._half_spread(bar)
        slip = (self.cfg.slippage_base + self.cfg.slippage_range_factor * self._range_pct(bar)
                + self.cfg.impact_k * (qty / bar.v if bar.v > 0 else 0.0))
        buy = o.req.side is Side.BUY
        price = p.mid * (1 + half) * (1 + slip) if buy else p.mid * (1 - half) * (1 - slip)
        notional = price * qty
        fee = notional * self.cfg.fee_rate
        if buy:
            if self.cash + 1e-9 < notional + fee:
                self._end(o, OrderStatus.CANCELLED, "insufficient cash for the next fill")
                return None
            self.cash -= notional + fee
            self.holdings[o.req.symbol] = self.holdings.get(o.req.symbol, 0.0) + qty
        else:
            if self.holdings.get(o.req.symbol, 0.0) + 1e-9 < qty:
                self._end(o, OrderStatus.CANCELLED, "insufficient holdings for the next fill")
                return None
            self.cash += notional - fee
            self.holdings[o.req.symbol] = self.holdings.get(o.req.symbol, 0.0) - qty
        o.filled += qty
        o.cost += price * qty
        self._bar_used[used_key] = self._bar_used.get(used_key, 0.0) + qty
        o.status = OrderStatus.FILLED if o.req.quantity - o.filled <= self.cfg.step_size / 2 else OrderStatus.PARTIAL
        if o.status is OrderStatus.FILLED:
            o.filled = o.req.quantity
        fl = SimFill(o.req.client_order_id, o.req.symbol, o.req.side.value, qty, price, fee, p.as_of, p.mid, half, slip, bar.t)
        self.fills.append(fl)
        return fl

    def _end(self, o: _Open, status: OrderStatus, note: str) -> None:
        o.status, o.note = status, note

    def _round_down(self, q: float) -> float:
        if q <= 0:
            return 0.0
        step = Decimal(str(self.cfg.step_size))
        return float((Decimal(str(q)) / step).to_integral_value(rounding=ROUND_DOWN) * step)

    # ---------------------------------------------------------------- ExchangeAdapter
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(market_data=True, account_snapshot=True, instrument_metadata=True, open_orders=True, order_submission=True,
                                   lookup_by_order_id=True, lookup_by_client_id=True, cancellation=True, spot_holdings=True)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_balance(self) -> dict[str, float]:
        self._check_up()
        return {self.quote_asset: self.cash}

    async def get_account_snapshot(self) -> list[AccountBalance]:
        self._check_up()
        return [AccountBalance(self.quote_asset, self.cash, self.cash, 0.0)]

    async def get_markets(self) -> list[dict[str, Any]]:
        self._check_up()
        return [{"symbol": s, "tick_size": 0.01, "step_size": self.cfg.step_size, "min_qty": self.cfg.min_qty, "max_qty": self.cfg.max_qty,
                 "min_notional": self.cfg.min_notional, "fee": self.cfg.fee_rate, "enabled": True} for s in self._points]

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        self._check_up()
        p = self._points.get(symbol)
        if p is None:
            raise InvalidOrder(f"no market {symbol}")
        half = self._half_spread(p.bar)
        return {"last": p.mid, "bid": p.mid * (1 - half), "ask": p.mid * (1 + half), "timestamp": p.as_of}

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict[str, Any]:
        self._check_up()
        p = self._points.get(symbol)
        if p is None:
            raise InvalidOrder(f"no market {symbol}")
        half = self._half_spread(p.bar)
        return {"bids": [[p.mid * (1 - half * i), 1.0] for i in range(1, depth + 1)], "asks": [[p.mid * (1 + half * i), 1.0] for i in range(1, depth + 1)]}

    def _rejected_by_seed(self, cid: str) -> bool:
        if self.cfg.reject_rate <= 0:
            return False
        h = hashlib.sha256(f"dewako-sim|{self.cfg.seed}|{cid}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64 < self.cfg.reject_rate

    def _as_order(self, o: _Open) -> Order:
        avg = o.cost / o.filled if o.filled > 0 else None
        return Order(exchange_order_id=o.exchange_order_id, client_order_id=o.req.client_order_id, symbol=o.req.symbol, side=o.req.side,
                     type=o.req.type, quantity=o.req.quantity, filled=o.filled, price=avg, status=o.status, timestamp=o.accepted_at)

    async def create_order(self, req: OrderRequest) -> Order:
        self._check_up()
        if req.client_order_id in self.orders:
            return self._as_order(self.orders[req.client_order_id])
        if req.symbol not in self._points:
            raise InvalidOrder(f"no market {req.symbol}")
        if req.type is not OrderType.MARKET:
            raise InvalidOrder("the simulated exchange supports MARKET orders only")
        if self._in(self.cfg.reject_windows):
            raise InvalidOrder("simulated exchange rejection: trading is suspended")
        if self._rejected_by_seed(req.client_order_id):
            raise InvalidOrder("simulated exchange rejection")
        q = self._round_down(req.quantity)
        if q < self.cfg.min_qty:
            raise InvalidOrder("quantity is below the exchange minimum after rounding down to the step")
        if q > self.cfg.max_qty:
            raise InvalidOrder("quantity is above the exchange maximum")
        p = self._points[req.symbol]
        est = p.mid * (1 + self._half_spread(p.bar))
        if est * q < self.cfg.min_notional:
            raise InvalidOrder("below minimum notional")
        if req.side is Side.BUY:
            if self.cash < est * q * (1 + self.cfg.fee_rate):
                raise InsufficientBalance("insufficient simulated balance")
        elif self.holdings.get(req.symbol, 0.0) + 1e-12 < q:
            raise InvalidOrder("insufficient simulated holdings: short selling is not supported")
        now = self.clock()
        o = _Open(req.model_copy(update={"quantity": q}), f"S{next(self._ids)}", now, now + timedelta(seconds=self.cfg.latency_seconds), next(self._seq))
        self.orders[req.client_order_id] = o
        self._advance(req.symbol)
        if self._in(self.cfg.lost_response_windows):
            raise TimeoutError("simulated lost response: the exchange accepted the order but the answer never arrived")
        return self._as_order(o)

    async def cancel_order(self, symbol: str, order_id: str) -> Order:
        self._check_up()
        for o in self.orders.values():
            if o.exchange_order_id == order_id or o.req.client_order_id == order_id:
                if o.status not in (OrderStatus.NEW, OrderStatus.PARTIAL):
                    raise InvalidOrder(f"order is already {o.status.value}")
                self._end(o, OrderStatus.CANCELLED, "cancelled by request")
                return self._as_order(o)
        raise OrderNotFound("order not found")

    async def get_order(self, symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> Order:
        self._check_up()
        for o in self.orders.values():
            if (client_order_id is not None and o.req.client_order_id == client_order_id) or (order_id is not None and o.exchange_order_id == order_id):
                return self._as_order(o)
        raise OrderNotFound("order not found")

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        self._check_up()
        return [self._as_order(o) for o in self.orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIAL) and (symbol is None or o.req.symbol == symbol)]

    async def get_positions(self) -> list[dict[str, Any]]:
        self._check_up()
        return [{"symbol": s, "quantity": q} for s, q in self.holdings.items() if q]
