"""Shared fixtures for the hardening tests. Fake exchange only; nothing here touches a network."""
import asyncio
import atexit
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from dewako.core.types import Order, OrderRequest, OrderStatus, OrderType, RiskDecision, Side, Signal
from dewako.exchanges.base import (AccountBalance, AdapterCapabilities, Fill, ExchangeAdapter, ExchangeError, InvalidOrder, OrderNotFound, OrderOutcomeUnknown)
from dewako.execution.engine import ExecutionEngine
from dewako.execution.validation import QuoteGuardConfig
from dewako.market_data.timeframes import floor, seconds
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.persistence.db import Database, now_iso
from dewako.safety.controller import SafetyController

ADMIN = {"user_id": 1, "username": "alice", "role": "ADMIN"}
TRADER = {"user_id": 2, "username": "bob", "role": "TRADER"}
TEST_QUOTE_GUARD = QuoteGuardConfig(max_spread_pct=0.25, max_price_deviation_pct=0.5,
                                   max_quote_age_seconds=30, clock_skew_tolerance_seconds=2)


_SCHEMAS = []


def backend():
    return os.environ.get("DEWAKO_TEST_DB", "sqlite")


def _drop_schemas():
    for d in _SCHEMAS:
        try:
            d.drop_schema()
        except Exception:
            pass


atexit.register(_drop_schemas)


def tmp_db(path=None):
    """A fresh, empty database. DEWAKO_TEST_DB=postgres runs the same tests on the real PostgreSQL server
    (DEWAKO_TEST_PG_URL), each test in its own schema. Returns (db, target) where target can be passed to Database() to 'restart'."""
    if backend() == "postgres":
        base = os.environ["DEWAKO_TEST_PG_URL"]
        target = path or f"{base}?schema=t_{uuid.uuid4().hex[:14]}"
        db = Database(target)
        if not path:
            _SCHEMAS.append(db)
        return db, target
    path = path or os.path.join(tempfile.mkdtemp(), "t.db")
    return Database(path), path


def decision(signal_id="sig1", side=Side.BUY, qty=1.0, symbol="BTC/USDT", approved=True, strategy="Trend AI", version="2.2"):
    s = Signal(symbol=symbol, side=side, confidence=0.8, strategy=strategy, strategy_version=version, signal_id=signal_id,
               entry_price=100.0, stop_price=95.0 if side is Side.BUY else 105.0, reason=["test"],
               timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return RiskDecision(approved=approved, signal=s, quantity=qty if approved else 0.0, reasons=["ok"])


class FakeLive(ExchangeAdapter):
    """Scriptable LIVE-environment exchange."""
    name = "fake"
    environment = "LIVE"

    def __init__(self, mode="ok", price=100.0):
        self.extra_fills = []        # tests append Fill objects here to simulate duplicate / late / external fills
        self.hide_fills_for = set()
        self.mode, self.price = mode, price
        self.store: dict[str, Order] = {}
        self.create_calls = 0
        self.get_fails = False
        self.cancel_calls = 0
        self.initial_usdt = 100000.0

    def _order(self, req, status=OrderStatus.FILLED, filled=None):
        return Order(exchange_order_id=f"X{len(self.store)+1}", client_order_id=req.client_order_id, symbol=req.symbol, side=req.side,
                     type=req.type, quantity=req.quantity, filled=req.quantity if filled is None else filled, price=self.price, status=status)

    async def create_order(self, req):
        self.create_calls += 1
        m = self.mode
        if m == "reject":
            raise InvalidOrder("below min notional")
        if m == "timeout_absent":
            raise OrderOutcomeUnknown("timeout")
        if m == "timeout_accepted":
            self.store[req.client_order_id] = self._order(req)
            raise OrderOutcomeUnknown("timeout after accept")
        if m == "http500":
            raise ExchangeError("HTTP 500")
        if m == "malformed":
            return {"junk": True}
        if m == "wrong_id":
            o = self._order(req); return o.model_copy(update={"client_order_id": "other"})
        if m == "partial":
            o = self._order(req, OrderStatus.PARTIAL, filled=req.quantity / 2); self.store[req.client_order_id] = o; return o
        if m == "open":
            o = self._order(req, OrderStatus.OPEN, filled=0); self.store[req.client_order_id] = o; return o
        o = self._order(req); self.store[req.client_order_id] = o; return o

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        if self.get_fails:
            raise ExchangeError("exchange unreachable")
        if client_order_id in self.store:
            return self.store[client_order_id]
        raise OrderNotFound("no such order")

    async def cancel_order(self, symbol, order_id):
        self.cancel_calls += 1
        for k, o in self.store.items():
            if o.exchange_order_id == order_id or k == order_id:
                o2 = o.model_copy(update={"status": OrderStatus.CANCELLED}); self.store[k] = o2; return o2
        raise OrderNotFound("x")

    async def connect(self): ...
    async def disconnect(self): ...
    def _net(self):
        qty = sum((o.filled if o.side is Side.BUY else -o.filled) for o in self.store.values())
        usdt = self.initial_usdt - sum((o.filled * (o.price or self.price)) * (1 if o.side is Side.BUY else -1) for o in self.store.values())
        return qty, usdt

    @property
    def balance(self):
        return self._net()[1]

    @balance.setter
    def balance(self, v):
        self.initial_usdt += v - self.balance

    async def get_balance(self): return {"USDT": self.balance}

    async def get_account_snapshot(self):
        if self.get_fails:
            raise ExchangeError("exchange unreachable")
        qty, usdt = self._net()
        locked = 0.0
        out = [AccountBalance("USDT", usdt, usdt - locked, locked)]
        if abs(qty) > 1e-12:
            out.append(AccountBalance("BTC", qty, qty, 0.0))
        return out

    async def get_positions(self):
        if self.get_fails:
            raise ExchangeError("exchange unreachable")
        qty, _ = self._net()
        return [{"symbol": "BTC/USDT", "quantity": qty}] if abs(qty) > 1e-12 else []
    def capabilities(self):
        return AdapterCapabilities(market_data=True, account_snapshot=True, instrument_metadata=True, open_orders=True, order_submission=True,
                                   lookup_by_order_id=True, lookup_by_client_id=True, cancellation=True, fill_history=True, spot_holdings=True,
                                   test_double=True)


    async def get_fills(self, since=None):
        if self.get_fails:
            raise ExchangeError("exchange unreachable")
        out = [Fill(f"T-{o.client_order_id}", o.client_order_id, o.exchange_order_id, o.symbol, o.side.value, o.filled, o.price or self.price)
               for o in self.store.values() if o.filled > 0 and o.client_order_id not in self.hide_fills_for]
        return out + list(self.extra_fills)

    async def get_markets(self):
        return [{"symbol": "BTC/USDT", "tick_size": 0.01, "qty_precision": 5, "min_notional": 5.0, "min_qty": 0.0001, "max_qty": 1000, "fee": 0.001}]
    async def get_ticker(self, s):
        from datetime import datetime, timezone
        return {"last": self.price, "bid": self.price * 0.9996, "ask": self.price * 1.0004,
                "timestamp": datetime.now(timezone.utc)}
    async def get_orderbook(self, s, depth=20): return {"bids": [], "asks": []}

    async def get_open_orders(self, symbol=None): return [o for o in self.store.values() if o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)]


def make_live_ready(db, safety, exchange="fake", strategy=("Trend AI", "2.2"), recon_ok=True):
    """Insert everything the live gate checks, then enable live. Returns exchange id."""
    import json
    from dewako.strategies import library  # noqa: F401
    from dewako.strategies import base as sbase
    from dewako.strategies.versions import fingerprint
    fp = fingerprint(sbase.get(*strategy))  # the real fingerprint, so startup verification agrees
    ts = now_iso()
    with db.tx() as c:
        eid = c.execute("INSERT INTO exchanges(exchange,label,environment,enabled,permissions,verified_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (exchange, exchange, "LIVE", 1, json.dumps({"read": True, "trade": True, "withdraw": False}), ts, ts, ts)).lastrowid
        c.execute("INSERT INTO credentials(exchange_id,encrypted_blob,encryption_key_version,last4,withdrawal_permission,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                  (eid, b"x", 1, "abcd", 0, ts, ts))
        c.execute("INSERT INTO strategy_versions(strategy_id,strategy_version,fingerprint,release_status,enabled,created_at,released_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT (strategy_id, strategy_version) DO UPDATE SET fingerprint=excluded.fingerprint, release_status=excluded.release_status, enabled=excluded.enabled",
                  (*strategy, fp, "RELEASED", 1, ts, ts))
        if recon_ok:
            c.execute("INSERT INTO reconciliation_runs(exchange,started_at,finished_at,result,findings) VALUES (?,?,?,?,?)", (exchange, ts, ts, "OK", "[]"))
            for scope in (f"exchange:{exchange}", "orders"):
                c.execute("INSERT INTO reconciliation_state(scope,state,reason,updated_at) VALUES (?,?,?,?) ON CONFLICT (scope) DO UPDATE SET state=excluded.state", (scope, "HEALTHY", "test setup", ts))
    # Fixture stand-ins for the things activation asks about. The activation policy itself is tested in test_live_activation.py with these
    # absent or failing; here they only let engine tests start from "live is ON". Neither replaces the engine's own live lock.
    if safety.workers_healthy is None:
        safety.workers_healthy = lambda: (True, "")
    if safety.live_adapters_ok is None:
        safety.live_adapters_ok = lambda: (True, "")
    safety.verify_audit("test_setup")
    safety.record_evidence("TESTNET_VERIFIED", ADMIN, "fixture-only attestation used by tests; no real testnet was involved")
    tf = "15m"
    candle_open = floor(datetime.now(timezone.utc), tf) - timedelta(seconds=seconds(tf))
    safety.mark_market_data_fresh("BTC/USDT", candle_open, tf)
    safety.enable_live(ADMIN)
    return eid


def run(coro):
    return asyncio.run(coro)


def engine_for(db, adapter, exchange="fake", exchange_id=None):
    safety = SafetyController(db)
    safety.workers_healthy = lambda: (True, "")      # fixture stand-in (also used by subprocess workers): the live gate fails closed without a supervisor
    return ExecutionEngine(db, safety, {exchange: adapter}, exchange_ids={exchange: exchange_id} if exchange_id else {},
                           quote_guard=TEST_QUOTE_GUARD), safety
