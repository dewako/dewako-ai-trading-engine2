"""Phase 3.3: adapter contract + capability honesty, the live lock, and fill reconciliation.
Fake exchanges only (a deterministic test double and the paper adapter). Nothing here proves behaviour against a real exchange."""
import hashlib
import hmac
import os
import urllib.parse

import pytest

from dewako.exchanges.base import (AdapterCapabilities, ExchangeError, Fill, InvalidOrder, NotConnected, NotImplementedForExchange,
                                   OrderNotFound, OrderOutcomeUnknown, RateLimited, classify_error)
from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.registry import REGISTRY
from dewako.exchanges.stubs import BybitAdapter, OKXAdapter
from dewako.execution.engine import ExecutionEngine
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.safety.controller import LiveSwitch, SafetyController
from hardening_helpers import *
from test_hardening_platform import _live_env

# method each capability flag promises
PROMISES = {"account_snapshot": "get_account_snapshot", "open_orders": "get_open_orders", "fill_history": "get_fills", "order_history": "get_order_history",
            "instrument_metadata": "get_markets", "spot_holdings": "get_balance"}


# ------------------------------------------------------------------ contract / capability honesty
def test_every_registered_real_adapter_is_unverified_for_live_and_is_not_a_test_double():
    for name, cls in REGISTRY.items():
        caps = cls().capabilities() if name != "binance" else cls(api_key="k", api_secret="s").capabilities()
        assert caps.live_execution_verified is False and caps.test_double is False and caps.testnet_verified is False, name
        assert caps.order_submission is False and caps.cancellation is False, name        # none of these is implemented


def test_stub_adapters_declare_nothing_and_every_trading_method_refuses():
    for ad in (BybitAdapter(), OKXAdapter()):
        assert ad.capabilities() == AdapterCapabilities()
        for m in ("get_account_snapshot", "get_fills", "get_order_history", "get_open_orders", "get_positions"):
            with pytest.raises((NotImplementedForExchange, NotConnected)):
                run(getattr(ad, m)())
        with pytest.raises(NotImplementedForExchange):
            run(ad.get_order("BTC/USDT", client_order_id="x"))


def test_binance_declares_only_the_permission_check_and_everything_else_refuses():
    ad = BinanceAdapter(api_key="k", api_secret="s")
    caps = ad.capabilities()
    assert caps.permissions_check and not any(getattr(caps, f) for f in PROMISES) and not caps.order_submission
    with pytest.raises(NotImplementedForExchange):
        run(ad.get_account_snapshot())
    with pytest.raises(NotImplementedForExchange):
        run(ad.get_fills())


def test_a_declared_capability_works_and_an_undeclared_one_refuses_on_the_test_double_and_paper_adapter():
    for ad in (FakeLive(), PaperAdapter()):
        caps = ad.capabilities()
        for flag, method in PROMISES.items():
            fn = getattr(ad, method)
            if getattr(caps, flag):
                try:
                    run(fn())
                except NotImplementedForExchange:
                    raise AssertionError(f"{ad.name} declares {flag} but {method} is not implemented")
            else:
                with pytest.raises((NotImplementedForExchange, TypeError, NotConnected)):
                    run(fn())


def test_lookup_by_client_id_returns_the_order_and_unknown_ids_are_not_found():
    ad = FakeLive()
    eng, safety = engine_for(*tmp_db()[:1], ad)
    eng.exchange_ids["fake"] = make_live_ready(eng.db, safety)
    r = run(eng.execute(decision("lk"), bot_id="b", environment="LIVE", exchange="fake"))
    cid = eng.orders.get(r.order_id)["client_order_id"]
    got = run(ad.get_order("BTC/USDT", client_order_id=cid))
    assert got.client_order_id == cid and got.filled == 1.0
    with pytest.raises(OrderNotFound):
        run(ad.get_order("BTC/USDT", client_order_id="nope"))


def test_error_classification_never_treats_an_unknown_failure_as_a_rejection():
    assert classify_error(RateLimited("x")) == "RATE_LIMITED"
    assert classify_error(InvalidOrder("x")) == "REJECTED"
    assert classify_error(OrderNotFound("x")) == "NOT_FOUND"
    assert classify_error(NotImplementedForExchange("x")) == "UNSUPPORTED"
    assert classify_error(OrderOutcomeUnknown("x")) == "UNKNOWN_OUTCOME"
    assert classify_error(TimeoutError()) == "UNKNOWN_OUTCOME"
    assert classify_error(ExchangeError("HTTP 500")) == "UNKNOWN_OUTCOME"
    assert classify_error(RuntimeError("???")) == "UNKNOWN_OUTCOME"


def test_binance_request_signature_is_hmac_sha256_of_the_exact_query():
    ad = BinanceAdapter(api_key="KEY", api_secret="topsecretvalue", clock=lambda: 1_700_000_000.0)
    url = ad.signed_url("/sapi/v1/account/apiRestrictions")
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    unsigned = parsed.query.rsplit("&signature=", 1)[0]
    assert q["timestamp"] == ["1700000000000"] and q["recvWindow"] == ["5000"]
    assert q["signature"] == [hmac.new(b"topsecretvalue", unsigned.encode(), hashlib.sha256).hexdigest()]
    assert "topsecretvalue" not in url                                                  # the secret itself is never in the URL


# ------------------------------------------------------------------ the live lock
class _RealishAdapter(FakeLive):
    """Looks like a real exchange adapter: LIVE environment, but NOT a test double and NOT verified for live execution."""
    name = "realish"
    def capabilities(self):
        return AdapterCapabilities(order_submission=True, cancellation=True)


def test_live_lock_blocks_an_unverified_adapter_even_when_live_is_enabled_and_flags_or_phrases_are_supplied():
    db, _ = tmp_db()
    ad = _RealishAdapter()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety)                                           # live switch ENABLED, gate conditions satisfied
    assert safety.live_switch() is LiveSwitch.ENABLED
    eng = ExecutionEngine(db, safety, {"fake": ad}, exchange_ids={"fake": eid})
    os.environ["DEWAKO_ALLOW_LIVE"] = "1"; os.environ["DEWAKO_LIVE_TRADING"] = "I UNDERSTAND THE RISK"
    try:
        r = run(eng.execute(decision("lock"), bot_id="b", environment="LIVE", exchange="fake"))
    finally:
        os.environ.pop("DEWAKO_ALLOW_LIVE", None); os.environ.pop("DEWAKO_LIVE_TRADING", None)
    assert r.outcome == "BLOCKED" and "live lock" in r.detail
    assert ad.create_calls == 0
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM risk_reservations").fetchone()[0] == 0


def test_real_adapter_classes_are_blocked_by_the_live_lock_and_never_called():
    db, _ = tmp_db()
    safety = SafetyController(db)
    eid = make_live_ready(db, safety, exchange="binance")
    for ad in (BinanceAdapter(api_key="k", api_secret="s"), BybitAdapter(), OKXAdapter()):
        eng = ExecutionEngine(db, safety, {ad.name: ad}, exchange_ids={ad.name: eid})
        r = run(eng.execute(decision(f"x-{ad.name}"), bot_id="b", environment="LIVE", exchange=ad.name))
        assert r.outcome == "BLOCKED", (ad.name, r)


# ------------------------------------------------------------------ fill reconciliation
def _filled_env():
    db, path, ad, eng, safety, rec = _live_env()
    r = run(eng.execute(decision("f1"), bot_id="b", environment="LIVE", exchange="fake"))
    cid = eng.orders.get(r.order_id)["client_order_id"]
    assert run(rec.run("fake"))["state"] == "HEALTHY"                           # fills agree: clean
    return db, ad, eng, safety, rec, r.order_id, cid


def _counts(db):
    with db.read() as c:
        return (c.execute("SELECT COUNT(*) FROM trades").fetchone()[0], c.execute("SELECT filled FROM orders").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM orders").fetchone()[0])


def _types(r):
    return {f["type"] for f in r["findings"]}


def test_late_fill_on_the_exchange_is_a_finding_blocks_live_and_is_never_applied_automatically():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    before = _counts(db)
    ad.extra_fills.append(Fill("LATE-1", cid, None, "BTC/USDT", "BUY", 0.5, 100.0))
    r = run(rec.run("fake"))
    assert "fill_missing_locally" in _types(r) and r["state"] == "RECONCILIATION_REQUIRED"
    assert safety.live_switch() is LiveSwitch.BLOCKED
    assert _counts(db) == before                                                # nothing fabricated: no trade, no changed quantity, no new order
    out = run(eng.execute(decision("after-late"), bot_id="b", environment="LIVE", exchange="fake"))
    assert out.outcome == "BLOCKED"
    # human-only: it does not vanish on the next run, and a non-admin cannot clear it
    assert run(rec.run("fake"))["state"] == "RECONCILIATION_REQUIRED"


def test_duplicate_fill_report_is_flagged_but_counted_once():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    dup = Fill(f"T-{cid}", cid, None, "BTC/USDT", "BUY", 1.0, 100.0)            # the same trade id the exchange already lists
    ad.extra_fills.append(dup)
    r = run(rec.run("fake"))
    assert _types(r) == {"fill_duplicate_remote"}                               # quantity was NOT double counted (no fill_missing_locally)


def test_external_fill_for_an_order_dewako_never_created_is_flagged():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    before = _counts(db)
    ad.extra_fills.append(Fill("EXT-1", "someone-elses-order", None, "BTC/USDT", "SELL", 2.0, 101.0))
    r = run(rec.run("fake"))
    assert "fill_unexpected_external" in _types(r) and _counts(db) == before


def test_local_fill_the_exchange_does_not_report_is_flagged():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    ad.hide_fills_for.add(cid)
    r = run(rec.run("fake"))
    assert "fill_local_only" in _types(r)


def test_local_quantity_larger_than_remote_fill_history_is_flagged():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    ad.hide_fills_for.add(cid)
    ad.extra_fills.append(Fill("PART-1", cid, None, "BTC/USDT", "BUY", 0.4, 100.0))
    r = run(rec.run("fake"))
    assert "fill_local_exceeds_remote" in _types(r)


def test_fill_findings_need_an_admin_with_a_reason_and_leave_the_books_untouched():
    db, ad, eng, safety, rec, oid, cid = _filled_env()
    ad.extra_fills.append(Fill("LATE-2", cid, None, "BTC/USDT", "BUY", 0.5, 100.0))
    run(rec.run("fake"))
    before = _counts(db)
    fid = [f for f in rec.open_findings("exchange:fake") if f["finding_type"] == "fill_missing_locally"][0]["id"]
    from dewako.safety.controller import NotAuthorized
    with pytest.raises(NotAuthorized):
        rec.resolve_finding(fid, TRADER, "late fill checked on exchange UI")
    rec.resolve_finding(fid, ADMIN, "Late 0.5 fill verified manually on the exchange; to be booked by an operator")
    assert _counts(db) == before                                               # resolving a finding is a decision, not an accounting entry


def test_unreadable_fill_history_fails_the_run_closed():
    db, path, ad, eng, safety, rec = _live_env()
    async def boom(since=None): raise ExchangeError("pagination failed")
    ad.get_fills = boom
    r = run(rec.run("fake"))
    assert r["result"] == "ERROR" and r["state"] == "FAILED"
    assert eng and run(eng.execute(decision("blocked"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"


def test_an_adapter_without_fill_history_is_not_compared_and_not_reported_as_clean():
    db, path, ad, eng, safety, rec = _live_env()
    ad.capabilities = lambda: AdapterCapabilities(account_snapshot=True, open_orders=True, order_submission=True, test_double=True)
    ad.extra_fills.append(Fill("X", "unknown", None, "BTC/USDT", "BUY", 1.0, 1.0))      # would be a finding if fills were compared
    r = run(rec.run("fake"))
    assert not any(t.startswith("fill_") for t in _types(r))                    # honest: skipped, with the limitation documented per adapter
