"""Phase 5 regressions: pre-trade risk accounting uses ONE conservative executable-price basis, and the success path records exactly one
ORDER_SUBMITTED audit event. Fake/paper adapters only (test doubles); no exchange is contacted, live is not enabled by the product.

Basis: BUY = max(reference, executable ask); SELL = min(reference, executable bid). It drives minimum notional, available balance,
position/exposure limits and the reservation. The quote is re-validated immediately before submission, and a BUY whose ask has risen above
the reserved unit price is not sent.
"""
import json
import math

from hardening_helpers import *
from dewako.core.types import Side
from dewako.execution import reservations as rsv
from dewako.execution.validation import (QuoteCheck, QuoteGuardError, conservative_risk_price)
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.risk.engine import RiskLimits
from dewako.risk.state import RiskStateStore
from datetime import datetime, timezone


def _live(price=100.0, balance=None, limits=None, adapter=None):
    db, _ = tmp_db()
    ad = adapter or FakeLive("ok", price=price)
    if balance is not None:
        ad.initial_usdt = balance
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    if limits is not None:
        eng.limits = limits
    return db, eng, ad


def _counts(db):
    with db.read() as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("orders", "execution_intents", "risk_reservations")}


def _check(price, **kw):
    return QuoteCheck(executable_price=price, spread_pct=0.01, deviation_pct=0.0, timestamp=datetime.now(timezone.utc))


# ------------------------------------------------------------------ the basis itself
def test_conservative_price_basis_is_max_for_buy_and_min_for_sell():
    assert conservative_risk_price("BUY", 100.0, _check(100.4)) == 100.4        # ask above reference: the higher cost
    assert conservative_risk_price("BUY", 100.0, _check(99.7)) == 100.0         # ask below reference: never cheaper than the approved reference
    assert conservative_risk_price("SELL", 100.0, _check(99.6)) == 99.6         # bid below reference: the lower value
    assert conservative_risk_price("SELL", 100.0, _check(100.3)) == 100.0


def test_conservative_price_rejects_missing_invalid_or_non_finite_inputs():
    good = _check(100.0)
    bad_refs = (None, 0, -1, math.nan, math.inf, True, "100")
    for ref in bad_refs:
        for side in ("BUY", "SELL"):
            try:
                conservative_risk_price(side, ref, good)
                raise AssertionError(f"accepted reference {ref!r}")
            except QuoteGuardError:
                pass
    for px in (None, 0, -5.0, math.nan, math.inf, True):
        try:
            conservative_risk_price("BUY", 100.0, QuoteCheck(px, 0.0, 0.0, datetime.now(timezone.utc)))
            raise AssertionError(f"accepted executable price {px!r}")
        except QuoteGuardError:
            pass
    for args in (("BUY", 100.0, None), ("HOLD", 100.0, good), ("buy", 100.0, good)):
        try:
            conservative_risk_price(*args)
            raise AssertionError(f"accepted {args!r}")
        except QuoteGuardError:
            pass


# ------------------------------------------------------------------ the regressions the review asked for
def test_buy_passes_at_the_reference_price_but_is_rejected_at_the_executable_ask_for_the_position_limit():
    db, eng, ad = _live(price=100.4)                       # ask 100.44004 (within the quote guard); strategy reference stays 100.0
    qty = 99.9                                             # 10% of 100000 equity = 10000
    dec = decision("cap1", qty=qty)
    assert qty * dec.signal.entry_price < 10000 < qty * ad.price * 1.0004           # reference arithmetic would pass; executable would not
    assert rsv.check_limits(eng.limits, equity=100000.0, quote_balance=100000.0, notional=qty * dec.signal.entry_price,
                            st={"position_cost": 0.0, "reserved_notional": 0.0, "open_positions": 0, "reserved_new_positions": 0},
                            new_position=True, exposure_increasing=True) == []   # proves the OLD (reference) basis would have approved it
    r = run(eng.execute(dec, bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "exceeds 10.0% of equity" in r.detail, r
    assert ad.create_calls == 0 and _counts(db) == {"orders": 0, "execution_intents": 0, "risk_reservations": 0}
    with db.read() as c:
        assert c.execute("SELECT status FROM signals WHERE signal_id='cap1'").fetchone()["status"] == "REJECTED"
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='ORDER_BLOCKED'").fetchone()[0] >= 1


def test_buy_passes_at_the_reference_price_but_is_rejected_at_the_executable_ask_for_available_balance():
    qty, fee = 99.0, 0.001
    ref_cost = qty * 100.0 * (1 + fee)                     # 9909.9
    exec_cost = qty * 100.44004 * (1 + fee)                # about 9953.9
    balance = 9910.0
    assert ref_cost <= balance < exec_cost
    db, eng, ad = _live(price=100.4, balance=balance, limits=RiskLimits(max_position_pct=100, max_exposure_pct=100))
    r = run(eng.execute(decision("bal1", qty=qty), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "available USDT" in r.detail, r
    assert ad.create_calls == 0 and _counts(db)["orders"] == 0


def test_buy_that_fits_at_the_executable_ask_is_reserved_at_that_price_not_the_reference():
    db, eng, ad = _live(price=100.4, adapter=FakeLive("open", price=100.4))      # stays OPEN, so the reservation stays ACTIVE
    r = run(eng.execute(decision("res1", qty=10.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome != "BLOCKED", r
    with db.read() as c:
        row = c.execute("SELECT notional, quantity, status FROM risk_reservations").fetchone()
        st = rsv.committed_state(c, "LIVE", "fake", rsv.scope_of("LIVE", "fake"))
        order = c.execute("SELECT price, quantity FROM orders").fetchone()
    ask = 100.4 * 1.0004
    assert row["status"] == "ACTIVE" and abs(row["notional"] - 10.0 * ask) < 1e-6 and row["notional"] > 10.0 * 100.0
    assert abs(st["reserved_notional"] - 10.0 * ask) < 1e-6                                  # exposure accounting sees the same figure
    assert order["price"] == 100.0                                                           # the order keeps the APPROVED reference (the deviation guard anchors on it)


def test_sell_below_minimum_notional_at_the_executable_bid_is_rejected_though_the_reference_passes():
    db, eng, ad = _live(price=100.0)                       # bid 99.96, reference 100
    qty = 0.05                                             # reference notional 5.00 (minimum 5.0) passes; bid notional 4.998 does not
    assert qty * 100.0 >= 5.0 > qty * 99.96
    r = run(eng.execute(decision("sell1", side=Side.SELL, qty=qty), bot_id="b", environment="LIVE", exchange="fake", intent="EXIT"))
    assert r.outcome == "BLOCKED" and "below the exchange minimum" in r.detail, r
    assert ad.create_calls == 0 and _counts(db)["orders"] == 0


def test_sell_with_enough_notional_at_the_bid_is_not_blocked_by_the_notional_check():
    db, eng, ad = _live(price=100.0)
    assert run(eng.execute(decision("sell2-buy", qty=1.0), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"   # a position to reduce
    r = run(eng.execute(decision("sell2", side=Side.SELL, qty=0.06), bot_id="b", environment="LIVE", exchange="fake", intent="EXIT"))
    assert r.outcome == "EXECUTED", r                                                           # 0.06 x bid 99.96 = 5.9976 >= the 5.0 minimum


# ------------------------------------------------------------------ quote movement, missing and invalid values
class MovingQuote(FakeLive):
    """Returns the scripted asks in order, one per get_ticker call (call 1 = pre-trade, call 2 = the pre-submission revalidation)."""
    def __init__(self, asks):
        super().__init__("ok", price=100.0)
        self.asks, self.calls = list(asks), 0

    async def get_ticker(self, s):
        ask = self.asks[min(self.calls, len(self.asks) - 1)]
        self.calls += 1
        return {"last": ask, "bid": ask * 0.9996, "ask": ask, "timestamp": datetime.now(timezone.utc)}


def test_ask_rising_above_the_reserved_price_before_submission_is_not_sent_and_releases_the_reservation():
    ad = MovingQuote([100.04, 100.30])                      # both inside the spread/deviation guard; the second costs more than was reserved
    db, eng, _ = _live(adapter=ad)
    r = run(eng.execute(decision("mv1", qty=10.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "rose above the price reserved" in r.detail, r
    assert ad.create_calls == 0 and ad.calls == 2
    with db.read() as c:
        o = c.execute("SELECT state FROM orders").fetchone()
        assert o["state"] == "FAILED"
        assert c.execute("SELECT status FROM risk_reservations").fetchone()["status"] == "RELEASED"


def test_ask_falling_before_submission_is_still_sent_and_revalidation_still_happens():
    ad = MovingQuote([100.04, 99.98])
    db, eng, _ = _live(adapter=ad)
    r = run(eng.execute(decision("mv2", qty=10.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "EXECUTED" and ad.create_calls == 1 and ad.calls == 2


def test_quote_that_becomes_invalid_before_submission_blocks_without_a_submission():
    class Breaks(MovingQuote):
        async def get_ticker(self, s):
            self.calls += 1
            if self.calls == 1:
                return {"last": 100.0, "bid": 99.96, "ask": 100.04, "timestamp": datetime.now(timezone.utc)}
            return {"last": 100.0, "bid": 99.96, "ask": math.nan, "timestamp": datetime.now(timezone.utc)}
    ad = Breaks([])
    db, eng, _ = _live(adapter=ad)
    r = run(eng.execute(decision("mv3", qty=10.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and ad.create_calls == 0
    with db.read() as c:
        assert c.execute("SELECT status FROM risk_reservations").fetchone()["status"] == "RELEASED"


def test_missing_ask_at_the_start_creates_nothing():
    class NoAsk(FakeLive):
        async def get_ticker(self, s):
            return {"last": 100.0, "bid": 99.96, "timestamp": datetime.now(timezone.utc)}
    db, eng, ad = _live(adapter=NoAsk("ok"))
    r = run(eng.execute(decision("na1", qty=1.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and ad.create_calls == 0 and _counts(db) == {"orders": 0, "execution_intents": 0, "risk_reservations": 0}


# ------------------------------------------------------------------ paper adapter consistency
def test_paper_accounting_matches_the_reservation_the_position_and_the_modelled_fill():
    db, _ = tmp_db()
    pa = PaperAdapter(10_000.0, slippage=0.0005)
    pa.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, pa, exchange="paper")
    RiskStateStore(db).init_scope("PAPER", 10_000.0)
    r = run(eng.execute(decision("pp1", qty=9.0), bot_id="pb", environment="PAPER", exchange="paper"))
    assert r.state == "FILLED", r
    ask = 100.0 * 1.0005
    with db.read() as c:
        res = c.execute("SELECT notional, status FROM risk_reservations").fetchone()
        pos = c.execute("SELECT quantity, COALESCE(average_price, entry_price) AS px FROM positions").fetchone()
        order = c.execute("SELECT price FROM orders").fetchone()
    assert res["status"] == "CONSUMED" and abs(res["notional"] - 9.0 * ask) < 1e-6          # reserved at the modelled fill price
    assert abs(pos["px"] - ask) < 1e-9 and abs(pos["quantity"] - 9.0) < 1e-12                # the position is booked at the same price
    spent = 10_000.0 - pa.cash
    assert abs(spent - (9.0 * ask) * (1 + pa.fee_rate)) < 1e-6                               # cash moved by exactly notional + fee
    assert spent >= res["notional"]                                                          # the reservation never understates the cash used
    assert order["price"] == 100.0


# ------------------------------------------------------------------ Task 2: audit-event count vs. submission count (kept separate)
def _audit_count(db, action):
    with db.read() as c:
        return c.execute("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()[0]


def test_a_successful_submission_records_exactly_one_order_submitted_audit_event():
    for mode in ("ok", "partial", "open"):
        db, eng, ad = _live(adapter=FakeLive(mode))
        r = run(eng.execute(decision("au-" + mode), bot_id="b", environment="LIVE", exchange="fake"))
        assert r.outcome == "EXECUTED", (mode, r)
        assert _audit_count(db, "ORDER_SUBMITTED") == 1, mode
        assert _audit_count(db, "ORDER_CREATED") == 1, mode
        with db.read() as c:
            ev = [x[0] for x in c.execute("SELECT event_type FROM order_events ORDER BY id")]
        assert ev.count("SUBMIT_ATTEMPT") == 1 and ev.count("EXCHANGE_ACK") == 1, (mode, ev)       # the order lifecycle also records one each


def test_paper_submission_also_records_exactly_one_order_submitted_audit_event():
    db, _ = tmp_db()
    pa = PaperAdapter(10_000.0)
    pa.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, pa, exchange="paper")
    RiskStateStore(db).init_scope("PAPER", 10_000.0)
    assert run(eng.execute(decision("pau", qty=1.0), bot_id="pb", environment="PAPER", exchange="paper")).state == "FILLED"
    assert _audit_count(db, "ORDER_SUBMITTED") == 1


def test_a_replayed_signal_is_one_order_one_submission_and_adds_no_audit_event():
    db, eng, ad = _live(adapter=FakeLive("ok"))
    first = run(eng.execute(decision("rp1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert first.outcome == "EXECUTED"
    again = run(eng.execute(decision("rp1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert again.outcome == "DUPLICATE"
    assert ad.create_calls == 1 and _counts(db)["orders"] == 1                                      # submissions: one (this is NOT the audit count)
    assert _audit_count(db, "ORDER_SUBMITTED") == 1 and _audit_count(db, "ORDER_CREATED") == 1      # audit events: unchanged by the replay


def test_ambiguous_submission_is_sent_once_and_records_no_submitted_event_until_reconciled():
    db, eng, ad = _live(adapter=FakeLive("timeout_accepted"))
    run(eng.execute(decision("amb1"), bot_id="b", environment="LIVE", exchange="fake"))
    assert ad.create_calls == 1                                                                      # never blindly resent
    assert _audit_count(db, "ORDER_SUBMITTED") == 0 and _audit_count(db, "ORDER_UNKNOWN") == 1


def test_activity_feed_shows_one_submission_event_not_two():
    from dewako.activity.service import ActivityService, Filters
    db, eng, ad = _live(adapter=FakeLive("ok"))
    run(eng.execute(decision("feed1"), bot_id="b", environment="LIVE", exchange="fake"))
    out, cur = [], None
    for _ in range(20):
        r = ActivityService(db).query(Filters(category="order"), limit=200, cursor=cur)
        out += r["events"]; cur = r["next_cursor"]
        if not r["has_more"]:
            break
    assert [e["type"] for e in out].count("ORDER_EXCHANGE_ACK") == 1
    assert not [e for e in out if e["source"] == "audit" and e["type"] == "ORDER_SUBMITTED"]


# ------------------------------------------------------------------ Phase 5.2: SELL send-time rule revalidation
class BidScript(FakeLive):
    """get_ticker returns the scripted bids in order once `script` is set (call 1 = pre-trade, call 2 = the pre-submission revalidation)."""
    script = None

    async def get_ticker(self, s):
        if self.script:
            bid = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            return {"last": bid, "bid": bid, "ask": bid * 1.0004, "timestamp": datetime.now(timezone.utc)}
        return await super().get_ticker(s)


def _with_position():
    ad = BidScript("ok")
    db, eng, _ = _live(adapter=ad)
    assert run(eng.execute(decision("pos-buy", qty=1.0), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"
    return db, eng, ad


def _sell(eng, sid, qty):
    return run(eng.execute(decision(sid, side=Side.SELL, qty=qty), bot_id="b", environment="LIVE", exchange="fake", intent="EXIT"))


def test_sell_that_passes_initial_validation_but_falls_below_min_notional_before_submission_is_not_sent():
    db, eng, ad = _with_position()
    calls_before = ad.create_calls
    ad.script = [100.0, 99.8]                        # initial: 0.0501 x 100 = 5.01 >= 5.0; at send: 0.0501 x 99.8 = 4.99998 < 5.0 (inside the 0.5% deviation guard)
    r = _sell(eng, "sell-send-1", 0.0501)
    assert r.outcome == "BLOCKED" and "no longer met" in r.detail and "below the exchange minimum" in r.detail, r
    assert ad.create_calls == calls_before                                       # nothing was submitted
    with db.read() as c:
        o = c.execute("SELECT state FROM orders WHERE side='SELL'").fetchone()
        ev = [x[0] for x in c.execute("SELECT event_type FROM order_events WHERE order_id=(SELECT id FROM orders WHERE side='SELL')")]
        pos = c.execute("SELECT quantity FROM positions WHERE status<>'CLOSED'").fetchone()
        blocked = c.execute("SELECT result,metadata FROM audit_log WHERE action='ORDER_BLOCKED' ORDER BY id DESC LIMIT 1").fetchone()
    assert o["state"] == "FAILED" and "RULES_REVALIDATION_FAILED" in ev and "SUBMIT_ATTEMPT" not in ev and "EXCHANGE_ACK" not in ev
    assert abs(pos["quantity"] - 1.0) < 1e-12                                    # the position is untouched
    assert blocked["result"] == "BLOCKED"
    assert json.loads(blocked["metadata"])["stage"] == "pre-send"
    assert _audit_count(db, "ORDER_SUBMITTED") == 1                              # only the earlier BUY


def test_a_blocked_sell_creates_no_retry_or_duplicate_submission():
    db, eng, ad = _with_position()
    ad.script = [100.0, 99.8]
    assert _sell(eng, "sell-send-2", 0.0501).outcome == "BLOCKED"
    calls, orders = ad.create_calls, _counts(db)["orders"]
    ad.script = None                                                             # the market is fine again
    again = _sell(eng, "sell-send-2", 0.0501)                                    # the same signal replayed
    assert again.outcome == "DUPLICATE"
    assert ad.create_calls == calls and _counts(db)["orders"] == orders          # no automatic retry, no second order for the signal


def test_sell_whose_notional_still_meets_the_minimum_at_the_send_time_bid_is_sent():
    db, eng, ad = _with_position()
    ad.script = [100.0, 99.8]
    r = _sell(eng, "sell-send-3", 0.06)                                          # 0.06 x 99.8 = 5.988 >= 5.0
    assert r.outcome == "EXECUTED", r


def test_sell_is_not_sent_if_the_exchange_rules_cannot_be_read_at_send_time():
    class RulesDie(BidScript):
        markets_calls = 0
        async def get_markets(self):
            self.markets_calls += 1
            if self.markets_calls > 2:                                           # buy: 1, sell pre-trade: 2, sell send-time: 3 -> fails
                raise ExchangeError("rules endpoint down")
            return await super().get_markets()
    ad = RulesDie("ok")
    db, eng, _ = _live(adapter=ad)
    assert run(eng.execute(decision("pos-buy2", qty=1.0), bot_id="b", environment="LIVE", exchange="fake")).outcome == "EXECUTED"
    calls = ad.create_calls
    r = _sell(eng, "sell-send-4", 0.06)
    assert r.outcome == "BLOCKED" and "could not be revalidated" in r.detail and ad.create_calls == calls


def test_buy_protection_and_reservation_checks_are_unchanged_by_the_sell_revalidation():
    ad = MovingQuote([100.04, 100.30])
    db, eng, _ = _live(adapter=ad)
    r = run(eng.execute(decision("buy-intact", qty=10.0), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and "rose above the price reserved" in r.detail
    with db.read() as c:
        assert c.execute("SELECT status FROM risk_reservations").fetchone()["status"] == "RELEASED"
        assert "RULES_REVALIDATION_FAILED" not in [x[0] for x in c.execute("SELECT event_type FROM order_events")]
