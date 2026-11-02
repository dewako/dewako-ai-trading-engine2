"""Phase 4.1: exchange-agnostic adapter contract. Deterministic fakes only: nothing here says anything about a real exchange."""
import asyncio
import unittest

import pytest

from dewako.core.types import Order, OrderRequest, OrderStatus, OrderType, Side
from dewako.exchanges import registry as reg
from dewako.exchanges.base import (AdapterCapabilities, AccountBalance, ExchangeAdapter, ExchangeError, InsufficientBalance, InvalidOrder,
                                   NotConnected, NotImplementedForExchange, OrderNotFound, OrderOutcomeUnknown, RateLimited)
from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.contract import (ADAPTER_ENVIRONMENTS, MUTATING_OPERATIONS, OPERATION_CAPABILITY, ORDER_ENVIRONMENTS_ENABLED, RetryClass,
                                       normalize_error, redact, refusals_for_submission, require, supports)
from dewako.exchanges.stubs import BybitAdapter, OKXAdapter
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.safety.controller import SafetyController
from hardening_helpers import *


def _req(cid="c1"):
    return OrderRequest(symbol="BTC/USDT", side=Side.BUY, type=OrderType.MARKET, quantity=1.0, client_order_id=cid)


# ------------------------------------------------------------------ the contract itself
def test_every_operation_maps_to_a_real_capability_flag_and_unknown_operations_are_unsupported():
    fields = set(AdapterCapabilities().as_dict())
    assert set(OPERATION_CAPABILITY.values()) <= fields
    assert MUTATING_OPERATIONS <= set(OPERATION_CAPABILITY)
    assert ORDER_ENVIRONMENTS_ENABLED == ("PAPER", "LIVE") and set(ORDER_ENVIRONMENTS_ENABLED) < set(ADAPTER_ENVIRONMENTS)
    assert "TESTNET" in ADAPTER_ENVIRONMENTS and "TESTNET" not in ORDER_ENVIRONMENTS_ENABLED
    assert not supports(FakeLive(), "transfer_funds") and not supports(FakeLive(), "withdraw")
    with pytest.raises(NotImplementedForExchange):
        require(FakeLive(), "withdraw")


def test_real_adapters_declare_nothing_they_do_not_implement_and_every_unsupported_operation_refuses():
    for cls in (BinanceAdapter, BybitAdapter, OKXAdapter):
        ad = cls()
        caps = ad.capabilities()
        assert not (caps.order_submission or caps.live_execution_verified or caps.test_double or caps.testnet_verified), cls.__name__
        assert not caps.lookup_by_client_id and not caps.fill_history and not caps.account_snapshot
        calls = [ad.get_balance(), ad.get_markets(), ad.get_ticker("BTC/USDT"), ad.get_orderbook("BTC/USDT"), ad.create_order(_req()),
                 ad.cancel_order("BTC/USDT", "1"), ad.get_order("BTC/USDT", client_order_id="c"), ad.get_positions(), ad.get_open_orders(),
                 ad.get_fills(), ad.get_order_history(), ad.get_account_snapshot()]
        for coro in calls:
            with pytest.raises(NotImplementedForExchange):          # never a fabricated success
                asyncio.run(coro)


def test_fake_and_paper_adapters_return_the_declared_types():
    for ad in (FakeLive(), PaperAdapter()):
        caps = ad.capabilities()
        assert caps.order_submission and caps.lookup_by_client_id and caps.instrument_metadata and caps.account_snapshot
        assert not caps.live_execution_verified
        if isinstance(ad, PaperAdapter):
            ad.set_price("BTC/USDT", 100.0)
            asyncio.run(ad.connect())
        markets = asyncio.run(ad.get_markets())
        assert isinstance(markets, list) and markets and "symbol" in markets[0]
        assert isinstance(asyncio.run(ad.get_balance()), dict)
        o = asyncio.run(ad.create_order(_req("same-id")))
        assert isinstance(o, Order) and o.client_order_id == "same-id" and o.symbol == "BTC/USDT"
        again = asyncio.run(ad.get_order("BTC/USDT", client_order_id="same-id"))
        assert again.client_order_id == "same-id"
        with pytest.raises(OrderNotFound):
            asyncio.run(ad.get_order("BTC/USDT", client_order_id="never-sent"))


# ------------------------------------------------------------------ engine enforces the declared surface
class _Counting(FakeLive):
    """Counts EVERY adapter call so 'no external call' can be asserted."""
    def __init__(self, caps=None, env="LIVE", **kw):
        super().__init__(**kw)
        self.calls = []
        self._caps, self.environment = caps, env
        self.down = False

    def capabilities(self):
        return self._caps or super().capabilities()

    def _hit(self, name):
        self.calls.append(name)
        if self.down:
            raise NotConnected("adapter disconnected")


for _n in ("get_markets", "get_balance", "get_ticker", "get_orderbook", "create_order", "cancel_order", "get_order", "get_open_orders",
           "get_fills", "get_positions", "get_account_snapshot"):
    def _wrap(name):
        async def m(self, *a, **k):
            self._hit(name)
            return await getattr(FakeLive, name)(self, *a, **k)
        return m
    setattr(_Counting, _n, _wrap(_n))


def _setup(ad, ready=True):
    db, _ = tmp_db()
    eng, safety = engine_for(db, ad)
    eid = make_live_ready(db, safety) if ready else None
    if eid:
        eng.exchange_ids["fake"] = eid
    return db, eng, safety


def _order_rows(db):
    with db.read() as c:
        return c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_an_adapter_that_does_not_declare_order_submission_receives_nothing():
    ad = _Counting(caps=AdapterCapabilities(instrument_metadata=True, account_snapshot=True, lookup_by_client_id=True, test_double=True))
    db, eng, _ = _setup(ad)
    r = run(eng.execute(decision("s1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "order_submission" in r.detail
    assert ad.calls == [] and _order_rows(db) == 0


def test_live_orders_need_client_id_lookup_so_an_ambiguous_submission_can_always_be_resolved():
    ad = _Counting(caps=AdapterCapabilities(order_submission=True, instrument_metadata=True, account_snapshot=True, test_double=True))
    db, eng, _ = _setup(ad)
    r = run(eng.execute(decision("s2"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "client order id" in r.detail
    assert ad.calls == [] and _order_rows(db) == 0


def test_missing_metadata_or_snapshot_capability_blocks_the_order():
    for missing in ("instrument_metadata", "account_snapshot"):
        flags = dict(order_submission=True, instrument_metadata=True, account_snapshot=True, lookup_by_client_id=True, test_double=True)
        flags[missing] = False
        ad = _Counting(caps=AdapterCapabilities(**flags))
        db, eng, _ = _setup(ad)
        r = run(eng.execute(decision("s3"), bot_id="b", environment="LIVE", exchange="fake"))
        assert r.outcome == "BLOCKED" and missing in r.detail and ad.calls == [] and _order_rows(db) == 0


def test_testnet_execution_is_refused_and_the_adapter_is_never_called():
    ad = _Counting(env="TESTNET")
    db, eng, _ = _setup(ad, ready=False)
    r = run(eng.execute(decision("s4"), bot_id="b", environment="TESTNET", exchange="fake"))
    assert r.outcome == "BLOCKED" and "TESTNET execution is not enabled" in r.detail
    assert ad.calls == [] and _order_rows(db) == 0


def test_environment_mismatch_and_unknown_environment_never_reach_the_adapter():
    ad = _Counting()                                              # declares LIVE
    db, eng, _ = _setup(ad)
    r = run(eng.execute(decision("s5"), bot_id="b", environment="PAPER", exchange="fake"))
    assert r.outcome == "BLOCKED" and "does not match" in r.detail
    bad = _Counting(env="SANDBOX")
    db2, eng2, _ = _setup(bad, ready=False)
    r = run(eng2.execute(decision("s6"), bot_id="b", environment="SANDBOX", exchange="fake"))
    assert r.outcome == "BLOCKED" and bad.calls == [] and _order_rows(db2) == 0
    assert refusals_for_submission(bad, "LIVE") and "unknown environment" in refusals_for_submission(bad, "LIVE")[0]


def test_no_external_call_at_all_when_authorization_fails():
    # 1. kill switch  2. live disabled  3. unapproved decision  4. adapter not verified for live (live lock)
    ad = _Counting()
    db, eng, safety = _setup(ad)
    safety.activate_kill_switch(ADMIN, "stop")
    assert run(eng.execute(decision("k1"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"
    safety.release_kill_switch(ADMIN)
    safety.disable_live(ADMIN)
    assert run(eng.execute(decision("k2"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"
    assert run(eng.execute(decision("k3", approved=False), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"
    assert ad.calls == [], ad.calls
    unverified = _Counting(caps=AdapterCapabilities(order_submission=True, instrument_metadata=True, account_snapshot=True, lookup_by_client_id=True))
    # live cannot even be enabled with an unverified adapter (activation gate); and an engine handed one still refuses.
    db2, _ = tmp_db()
    eng2, safety2 = engine_for(db2, unverified)
    try:
        make_live_ready(db2, safety2)
        raise AssertionError("live was enabled with an unverified adapter")
    except AssertionError:
        raise
    except Exception as e:
        assert "live lock" in str(e), e
    r = run(eng2.execute(decision("k4"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and unverified.calls == [] and _order_rows(db2) == 0


def test_adapter_disconnected_before_the_order_blocks_without_creating_anything_then_recovers():
    ad = _Counting()
    db, eng, _ = _setup(ad)
    ad.down = True
    r = run(eng.execute(decision("d1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and _order_rows(db) == 0 and "create_order" not in ad.calls
    ad.down = False                                               # the adapter reconnects
    r = run(eng.execute(decision("d1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "EXECUTED" and ad.calls.count("create_order") == 1


class _DropsMidRequest(_Counting):
    """create_order raises a GENERIC NotConnected AFTER the exchange accepted the order (a connection reset after transmission)."""
    async def create_order(self, req):
        self.calls.append("create_order")
        self.create_calls += 1
        self.store[req.client_order_id] = self._order(req)
        raise NotConnected("connection reset")


def test_a_generic_not_connected_from_create_order_is_an_unknown_outcome_never_a_failure_or_a_resend():
    # Regression (Phase 4.1): the engine used to treat any NotConnected as "nothing was sent" and mark the order FAILED, which would
    # allow a duplicate if the request had in fact been transmitted. Now it reconciles by client order id.
    ad = _DropsMidRequest()
    db, eng, _ = _setup(ad)
    r = run(eng.execute(decision("m1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome != "FAILED", r
    assert ad.calls.count("create_order") == 1                    # not resent
    with db.read() as c:
        st = c.execute("SELECT state FROM orders").fetchone()["state"]
    assert st in ("FILLED", "SUBMITTED", "PARTIALLY_FILLED"), st   # the lookup found the accepted order: no duplicate, no false failure
    again = run(eng.execute(decision("m1"), bot_id="b", environment="LIVE", exchange="fake"))
    # trading stays in RECONCILIATION_REQUIRED until an operator/reconciler clears it, so a repeat is refused rather than resent
    assert again.outcome == "BLOCKED" and "RECONCILIATION_REQUIRED" in again.detail and ad.calls.count("create_order") == 1


def test_not_implemented_is_still_a_definitive_not_sent():
    ad = _Counting()

    async def nope(req):
        raise NotImplementedForExchange("NOT_IMPLEMENTED: x")
    ad.create_order = nope
    db, eng, _ = _setup(ad)
    r = run(eng.execute(decision("n1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "FAILED"


# ------------------------------------------------------------------ registration
class _ReadOnly(ExchangeAdapter):
    name, environment = "ro", "TESTNET"
    def __init__(self, **kw): pass
    def capabilities(self): return AdapterCapabilities(market_data=True, instrument_metadata=True)
    async def connect(self): ...
    async def disconnect(self): ...
    async def get_balance(self): raise NotImplementedForExchange("x")
    async def get_markets(self): return []
    async def get_ticker(self, s): return {}
    async def get_orderbook(self, s, depth=20): return {}
    async def create_order(self, r): raise NotImplementedForExchange("x")
    async def cancel_order(self, s, o): raise NotImplementedForExchange("x")
    async def get_order(self, s, order_id=None, client_order_id=None): raise NotImplementedForExchange("x")
    async def get_positions(self): raise NotImplementedForExchange("x")
    async def get_open_orders(self, symbol=None): raise NotImplementedForExchange("x")


def _variant(**caps):
    class V(_ReadOnly):
        environment = caps.pop("env", "TESTNET")
        def capabilities(self): return AdapterCapabilities(**caps)
    return V


def test_registration_accepts_an_honest_adapter_and_cannot_grant_live_or_testnet_verification():
    before = dict(reg.REGISTRY)
    try:
        reg.register_adapter("ro_market", _ReadOnly)
        assert reg.REGISTRY["ro_market"] is _ReadOnly
        with pytest.raises(reg.RegistrationRefused):
            reg.register_adapter("ro_market", _ReadOnly)                           # no silent replacement
        reg.register_adapter("ro_market", _ReadOnly, replace=True)
        refused = [
            ("BadName", _ReadOnly), ("x", _ReadOnly), ("not_adapter", object), ("binance", _ReadOnly),
            ("claims_live", _variant(live_execution_verified=True, order_submission=True, lookup_by_client_id=True, instrument_metadata=True, account_snapshot=True)),
            ("claims_testnet", _variant(testnet_verified=True)),
            ("a_double", _variant(test_double=True)),
            ("no_lookup", _variant(order_submission=True, instrument_metadata=True, account_snapshot=True)),
            ("no_meta", _variant(order_submission=True, lookup_by_client_id=True)),
            ("bad_env", _variant(env="SANDBOX")),
        ]
        for name, cls in refused:
            with pytest.raises(reg.RegistrationRefused):
                reg.register_adapter(name, cls)
            if name != "binance":
                assert name not in reg.REGISTRY, name
            else:
                assert reg.REGISTRY["binance"] is BinanceAdapter
        with pytest.raises(reg.RegistrationRefused):
            reg.register_adapter("binance", BinanceAdapter, replace=True)          # built-ins cannot be replaced
        with pytest.raises(reg.RegistrationRefused):
            reg.unregister_adapter("okx")
        assert reg.LIVE_VERIFIED_ADAPTERS == frozenset() and reg.TESTNET_VERIFIED_ADAPTERS == frozenset()
    finally:
        reg.REGISTRY.clear()
        reg.REGISTRY.update(before)
    assert set(reg.REGISTRY) == {"binance", "bybit", "okx"}


def test_registering_an_adapter_never_changes_live_activation():
    db, _ = tmp_db()
    safety = SafetyController(db)
    try:
        reg.register_adapter("ro_market", _ReadOnly)
        assert not safety.activation_readiness().allowed and safety.live_switch().value != "ENABLED"
    finally:
        reg.unregister_adapter("ro_market")


# ------------------------------------------------------------------ error normalisation
def test_error_normalisation_is_conservative_for_writes_and_redacts_secrets():
    cases = [(InvalidOrder("min notional"), "REJECTED"), (InsufficientBalance("x"), "REJECTED"), (NotImplementedForExchange("x"), "UNSUPPORTED"),
             (OrderNotFound("x"), "NOT_FOUND"), (RateLimited("x"), "RATE_LIMITED"), (OrderOutcomeUnknown("x"), "UNKNOWN_OUTCOME"),
             (TimeoutError("t"), "UNKNOWN_OUTCOME"), (NotConnected("x"), "UNAVAILABLE"), (ExchangeError("x"), "UNKNOWN_OUTCOME"), (ValueError("?"), "UNKNOWN_OUTCOME")]
    for exc, code in cases:
        w = normalize_error(exc, operation="create_order", adapter="fake", correlation_id="cid")
        r = normalize_error(exc, operation="get_order_by_client_order_id", adapter="fake")
        assert w.code == r.code == code and w.correlation_id == "cid"
        if code in ("REJECTED", "UNSUPPORTED", "NOT_FOUND"):
            assert w.retry is r.retry is RetryClass.NO_RETRY and w.outcome_known
        else:
            assert w.retry is RetryClass.RECONCILE_FIRST and not w.outcome_known                  # a write is never simply retried
            assert r.retry is RetryClass.BACKOFF_RETRY                                             # a read may be
    e = normalize_error(ExchangeError("failed: apiKey=ABCDEFGH1234 secret: s3cr3tvalue " + "x" * 1000), operation="get_balance", adapter="a",
                        creds={"api_key": "ABCDEFGH1234"})
    assert "ABCDEFGH1234" not in e.message and "s3cr3tvalue" not in e.message and len(e.message) <= 300
    assert "TOPSECRETVALUE" not in redact("token=TOPSECRETVALUE")


def test_adapter_contract_has_no_withdrawal_or_transfer_operation():
    assert not any("withdraw" in k or "transfer" in k for k in OPERATION_CAPABILITY)
    assert not any(hasattr(ExchangeAdapter, n) for n in ("withdraw", "transfer", "enable_live", "set_live"))
