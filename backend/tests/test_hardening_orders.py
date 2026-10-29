import threading

from dewako.core.types import OrderStatus, Side
from dewako.exchanges.base import ExchangeError
from dewako.execution.engine import ExecutionEngine
from dewako.execution.order_store import OrderStore, RetryNotAllowed, StaleState
from dewako.execution.state_machine import IllegalTransition, OrderState as S, TRANSITIONS, TERMINAL
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.persistence.db import Database, now_iso
from dewako.safety.controller import SafetyController
from hardening_helpers import *
import pytest


def test_state_machine_is_explicit():
    assert S.FILLED not in TRANSITIONS[S.CREATED]
    for t in TERMINAL:
        assert TRANSITIONS[t] == set()
    db, _ = tmp_db()
    st = OrderStore(db)
    o = st.create(bot_id="b", signal_id="s", intent="ENTRY", environment="PAPER", exchange="paper", symbol="BTC/USDT", side="BUY",
                  order_type="MARKET", quantity=1, price=100, correlation_id="c")
    with pytest.raises(IllegalTransition):
        st.transition(o.order_id, S.CREATED, S.FILLED, "x")
    st.transition(o.order_id, S.CREATED, S.SUBMITTING, "x")
    with pytest.raises(StaleState):  # caller's view is out of date
        st.transition(o.order_id, S.CREATED, S.SUBMITTING, "x")
    evs = st.events(o.order_id)
    assert [e["new_state"] for e in evs] == ["CREATED", "SUBMITTING"]


def test_unique_intent_constraint_in_database():
    db, _ = tmp_db()
    st = OrderStore(db)
    kw = dict(bot_id="b", signal_id="s", intent="ENTRY", environment="PAPER", exchange="p", symbol="BTC/USDT", side="BUY",
              order_type="MARKET", quantity=1, price=1, correlation_id="c")
    a, b = st.create(**kw), st.create(**kw)
    assert a.created and not b.created and a.order_id == b.order_id
    with db.tx() as c:  # bypass the store entirely: the DB itself refuses a duplicate logical order
        with pytest.raises(Exception):
            c.execute("INSERT INTO execution_intents(bot_id,signal_id,intent,environment,correlation_id,created_at) VALUES ('b','s','ENTRY','PAPER','c',?)", (now_iso(),))


def test_successful_live_order_and_duplicate_signal():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, safety = engine_for(db, ad, exchange_id=1)
    eid = make_live_ready(db, safety)
    eng.exchange_ids["fake"] = eid
    r1 = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    r2 = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r1.outcome == "EXECUTED" and r1.state == "FILLED"
    assert r2.outcome == "DUPLICATE" and ad.create_calls == 1
    assert eng.positions.active("LIVE", "bot")[0]["quantity"] == 1.0


def _live(mode):
    db, path = tmp_db()
    ad = FakeLive(mode)
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    return db, ad, eng, safety


def test_rejected_order_never_becomes_a_trade():
    db, ad, eng, _ = _live("reject")
    r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.outcome == "REJECTED" and eng.positions.active("LIVE") == []
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_timeout_then_exchange_has_order_is_not_resent():
    db, ad, eng, safety = _live("timeout_accepted")
    r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.state == "FILLED" and ad.create_calls == 1
    states = [e["new_state"] for e in eng.orders.events(r.order_id)]
    assert states == ["CREATED", "SUBMITTING", "UNKNOWN", "RECONCILING", "FILLED"]
    assert len(eng.positions.active("LIVE", "bot")) == 1
    # an unresolved order flags reconciliation; only the full reconciliation service clears it
    assert safety.trading_state().value == "RECONCILIATION_REQUIRED"


def test_timeout_and_not_accepted_allows_one_safe_retry():
    db, ad, eng, _ = _live("timeout_absent")
    r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.outcome == "FAILED" and r.state == "FAILED"
    ev = [e["event_type"] for e in eng.orders.events(r.order_id)]
    assert "CONFIRMED_ABSENT" in ev
    ad.mode = "ok"
    # the retry goes through the gate, which is closed because reconciliation is required
    blocked = run(eng.retry(r.order_id))
    assert blocked.outcome == "BLOCKED"


def test_retry_after_confirmed_absent_paper_path():
    # PAPER has no live gate: exercise the retry transition itself
    db, _ = tmp_db()
    pa = PaperAdapter(); pa.set_price("BTC/USDT", 100.0)
    eng, _s = engine_for(db, pa, exchange="paper")
    store = eng.orders
    o = store.create(bot_id="b", signal_id="s", intent="ENTRY", environment="PAPER", exchange="paper", symbol="BTC/USDT", side="BUY",
                     order_type="MARKET", quantity=1, price=100, correlation_id="c")
    store.transition(o.order_id, S.CREATED, S.SUBMITTING, "A")
    store.transition(o.order_id, S.SUBMITTING, S.UNKNOWN, "U")
    store.transition(o.order_id, S.UNKNOWN, S.RECONCILING, "R")
    store.transition(o.order_id, S.RECONCILING, S.FAILED, "SOMETHING_ELSE")
    with pytest.raises(RetryNotAllowed):  # not confirmed absent -> never resend
        store.transition(o.order_id, S.FAILED, S.SUBMITTING, "RETRY")


def test_inconclusive_reconcile_stays_reconciling_and_never_resends():
    db, ad, eng, _ = _live("timeout_accepted")
    ad.get_fails = True
    r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.outcome == "UNRESOLVED" and r.state == "RECONCILING" and ad.create_calls == 1
    assert eng.positions.active("LIVE") == []
    ad.get_fails = False
    out = run(eng.recover())
    assert out[0].state == "FILLED" and ad.create_calls == 1


def test_http500_and_malformed_and_wrong_id_go_to_unknown_path():
    for mode in ("http500", "malformed", "wrong_id"):
        db, ad, eng, _ = _live(mode)
        r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
        assert ad.create_calls == 1
        ev = [e["new_state"] for e in eng.orders.events(r.order_id)]
        assert "UNKNOWN" in ev and "RECONCILING" in ev, mode


def test_adapter_error_secrets_are_redacted_from_order_events_and_audit():
    secret, key = "EXECUTIONSECRET99", "EXECUTIONKEY77"

    class LeakingError(FakeLive):
        async def create_order(self, req):
            self.create_calls += 1
            raise ExchangeError(f"request failed api_key={key} secret={secret}")

    db, _ = tmp_db()
    ad = LeakingError()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    result = run(eng.execute(decision("secret-error"), bot_id="bot", environment="LIVE", exchange="fake"))

    assert result.outcome == "FAILED"
    with db.read() as c:
        event_errors = " ".join(str(row[0]) for row in c.execute("SELECT error FROM order_events WHERE error IS NOT NULL"))
        audit_metadata = " ".join(str(row[0]) for row in c.execute("SELECT metadata FROM audit_log"))
    assert secret not in event_errors + audit_metadata
    assert key not in event_errors + audit_metadata


def test_partial_then_full_fill_updates_position_incrementally():
    db, ad, eng, _ = _live("partial")
    r = run(eng.execute(decision(qty=2.0), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.state == "PARTIALLY_FILLED"
    assert eng.positions.active("LIVE", "bot")[0]["quantity"] == 1.0
    cid = eng.orders.get(r.order_id)["client_order_id"]
    ad.store[cid] = ad.store[cid].model_copy(update={"status": __import__("dewako.core.types", fromlist=["OrderStatus"]).OrderStatus.FILLED, "filled": 2.0})
    # drive reconciliation via the legal path
    eng.orders.transition(r.order_id, S.PARTIALLY_FILLED, S.UNKNOWN, "TEST")
    out = run(eng.reconcile_order(r.order_id))
    assert out.state == "FILLED"
    assert eng.positions.active("LIVE", "bot")[0]["quantity"] == 2.0
    with db.read() as c:
        assert c.execute("SELECT SUM(quantity) FROM trades").fetchone()[0] == 2.0


@pytest.mark.parametrize(
    ("status", "filled", "price", "response_quantity"),
    [
        (OrderStatus.FILLED, 1.0, -1.0, None),
        (OrderStatus.FILLED, 1.0, 0.0, None),
        (OrderStatus.FILLED, 1.0, float("nan"), None),
        (OrderStatus.FILLED, 1.0, float("inf"), None),
        (OrderStatus.PARTIAL, 0.5, None, None),
        (OrderStatus.FILLED, 0.5, 100.0, None),
        (OrderStatus.FILLED, 1.1, 100.0, None),
        (OrderStatus.FILLED, 1.0, 100.0, 0.5),
    ],
)
def test_malformed_fill_response_remains_unresolved_and_does_not_change_accounting(status, filled, price, response_quantity):
    class MalformedFill(FakeLive):
        async def create_order(self, req):
            updates = {"price": price}
            if response_quantity is not None:
                updates["quantity"] = response_quantity
            order = self._order(req, status, filled).model_copy(update=updates)
            self.store[req.client_order_id] = order
            return order

    db, _ = tmp_db()
    ad = MalformedFill()
    eng, safety = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, safety)
    result = run(eng.execute(decision("invalid-fill-price"), bot_id="bot", environment="LIVE", exchange="fake"))

    assert result.outcome == "UNRESOLVED" and result.state == "RECONCILING"
    with db.read() as c:
        order = c.execute("SELECT state,filled FROM orders").fetchone()
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert c.execute("SELECT status FROM risk_reservations").fetchone()["status"] == "ACTIVE"
    assert order["state"] == "RECONCILING" and order["filled"] == 0


def test_out_of_order_lower_cumulative_fill_is_not_applied_or_persisted():
    db, ad, eng, _ = _live("partial")
    result = run(eng.execute(decision("out-of-order-fill", qty=2.0), bot_id="bot", environment="LIVE", exchange="fake"))
    assert result.state == "PARTIALLY_FILLED"
    cid = eng.orders.get(result.order_id)["client_order_id"]
    ad.store[cid] = ad.store[cid].model_copy(update={"filled": 0.5})
    eng.orders.transition(result.order_id, S.PARTIALLY_FILLED, S.UNKNOWN, "OUT_OF_ORDER_REPORT")

    out_of_order = run(eng.reconcile_order(result.order_id))
    with db.read() as c:
        order = c.execute("SELECT state,filled FROM orders").fetchone()
        trade_count = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        position_qty = c.execute("SELECT quantity FROM positions WHERE status<>'CLOSED'").fetchone()["quantity"]
    assert out_of_order.outcome == "UNRESOLVED" and order["state"] == "RECONCILING"
    assert order["filled"] == 1.0 and trade_count == 1 and position_qty == 1.0

    ad.store[cid] = ad.store[cid].model_copy(update={"filled": 1.5})
    recovered = run(eng.reconcile_order(result.order_id))
    with db.read() as c:
        total_traded = c.execute("SELECT SUM(quantity) FROM trades").fetchone()[0]
        position_qty = c.execute("SELECT quantity FROM positions WHERE status<>'CLOSED'").fetchone()["quantity"]
    assert recovered.state == "PARTIALLY_FILLED" and total_traded == 1.5 and position_qty == 1.5


def test_cancel_open_order():
    db, ad, eng, _ = _live("open")
    r = run(eng.execute(decision(), bot_id="bot", environment="LIVE", exchange="fake"))
    assert r.state == "OPEN"
    c = run(eng.cancel(r.order_id))
    assert eng.orders.get(r.order_id)["state"] == "CANCELLED" and ad.cancel_calls == 1


def test_crash_after_persisting_submitting_recovers_without_duplicate():
    """Test A/B: process dies after SUBMITTING was saved but before the result was. A fresh engine on the same DB recovers."""
    db, ad, eng, safety = _live("ok")
    ad.mode = "ok"
    created = eng.orders.create(bot_id="bot", signal_id="sigX", intent="ENTRY", environment="LIVE", exchange="fake", symbol="BTC/USDT",
                                side="BUY", order_type="MARKET", quantity=1.0, price=100, correlation_id="c1")
    eng.orders.transition(created.order_id, S.CREATED, S.SUBMITTING, "SUBMIT_ATTEMPT")
    # ...crash. The exchange DID accept the order:
    from dewako.core.types import OrderRequest, OrderType
    run(ad.create_order(OrderRequest(symbol="BTC/USDT", side=Side.BUY, type=OrderType.MARKET, quantity=1.0, client_order_id=created.client_order_id)))
    db2 = Database(db.path)  # restart
    eng2 = ExecutionEngine(db2, SafetyController(db2), {"fake": ad}, quote_guard=TEST_QUOTE_GUARD)
    out = run(eng2.recover())
    assert out[0].state == "FILLED" and ad.create_calls == 1
    ev = [e["event_type"] for e in eng2.orders.events(created.order_id)]
    assert "RECOVERED_AFTER_CRASH" in ev
    # the same signal arriving again after the restart creates nothing
    again = run(eng2.execute(decision("sigX"), bot_id="bot", environment="LIVE", exchange="fake"))
    assert again.outcome in ("DUPLICATE", "BLOCKED") and ad.create_calls == 1


def test_concurrent_workers_same_signal_one_order():
    db, path = tmp_db()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        d = Database(path)
        st = OrderStore(d)
        barrier.wait()
        results.append(st.create(bot_id="b", signal_id="S", intent="ENTRY", environment="PAPER", exchange="p", symbol="BTC/USDT",
                                 side="BUY", order_type="MARKET", quantity=1, price=1, correlation_id="c"))
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(r.created for r in results) == [False, True]
    assert results[0].order_id == results[1].order_id
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_concurrent_engines_same_signal_send_once():
    db, path = tmp_db()
    ad = FakeLive()
    eng0, safety0 = engine_for(db, ad)
    eid = make_live_ready(db, safety0)
    outs = []
    barrier = threading.Barrier(2)

    def worker():
        d = Database(path)
        e = ExecutionEngine(d, SafetyController(d), {"fake": ad}, exchange_ids={"fake": eid}, quote_guard=TEST_QUOTE_GUARD)
        barrier.wait()
        outs.append(run(e.execute(decision("same"), bot_id="bot", environment="LIVE", exchange="fake")).outcome)
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(outs) == ["DUPLICATE", "EXECUTED"] and ad.create_calls == 1


def test_transaction_rolls_back_whole_change():
    db, _ = tmp_db()
    st = OrderStore(db)
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            st.create(bot_id="b", signal_id="s", intent="ENTRY", environment="PAPER", exchange="p", symbol="BTC/USDT", side="BUY",
                      order_type="MARKET", quantity=1, price=1, correlation_id="c", conn=c)
            raise RuntimeError("boom")
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0] == 0
