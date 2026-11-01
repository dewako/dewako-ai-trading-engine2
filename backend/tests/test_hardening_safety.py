from datetime import datetime, timedelta, timezone

from dewako.core.types import Side
from dewako.execution.engine import ExecutionEngine
from dewako.paper_trading.paper_exchange import PaperAdapter
from dewako.persistence.audit import AuditLog, scrub
from dewako.persistence.db import Database
from dewako.risk.engine import RiskEngine, RiskLimits
from dewako.risk.state import RiskStateStore
from dewako.safety.controller import LiveSwitch, NotAuthorized, SafetyController, TradingState
from hardening_helpers import *
import pytest


def test_fresh_install_live_disabled_and_healthy():
    db, _ = tmp_db()
    s = SafetyController(db)
    assert s.live_switch() is LiveSwitch.DISABLED
    assert s.trading_state() is TradingState.SYSTEM_HEALTHY
    assert not s.kill_switch_active()


def test_kill_switch_survives_restart_and_needs_admin_to_release():
    db, path = tmp_db()
    s = SafetyController(db)
    s.activate_kill_switch({"username": "worker", "role": "SYSTEM"}, "drawdown")
    s2 = SafetyController(Database(path))  # Test D: restart
    assert s2.kill_switch_active() and s2.trading_state() is TradingState.KILL_SWITCH_ACTIVE
    for who in (TRADER, {"username": "ai", "role": "ADMIN"}, {"username": "x", "role": "VIEWER"}):
        with pytest.raises(NotAuthorized):
            s2.release_kill_switch(who)
    assert s2.kill_switch_active()
    s2.release_kill_switch(ADMIN)
    assert not SafetyController(Database(path)).kill_switch_active()
    h = s2.kill_switch_history()[0]
    assert h["triggered_by"] == "worker" and h["cleared_by"] == "alice" and h["reason"] == "drawdown"


def test_unknown_safety_state_blocks():
    db, path = tmp_db()
    s = SafetyController(db)
    with db.tx() as c:
        c.execute("DELETE FROM safety_state WHERE key='live_trading'")
    assert s.kill_switch_active() is True            # baseline damaged: fail closed
    assert s.trading_state() is TradingState.TRADING_BLOCKED
    assert s.live_switch() is LiveSwitch.BLOCKED


def test_live_requires_admin_and_healthy_state():
    db, _ = tmp_db()
    s = SafetyController(db)
    with pytest.raises(NotAuthorized):
        s.enable_live(TRADER)
    s.set_reconciliation_required(True, "test")
    with pytest.raises(NotAuthorized):
        s.enable_live(ADMIN)
    assert s.live_switch() is LiveSwitch.DISABLED


def test_live_comes_back_blocked_if_startup_verification_fails():
    db, path = tmp_db()
    s = SafetyController(db)
    make_live_ready(db, s)
    assert s.live_switch() is LiveSwitch.ENABLED
    s2 = SafetyController(Database(path))
    assert s2.on_startup(verification_ok=False) is LiveSwitch.BLOCKED
    assert SafetyController(Database(path)).live_switch() is LiveSwitch.BLOCKED


def test_live_gate_each_condition_blocks():
    db, _ = tmp_db()
    s = SafetyController(db)
    eid = make_live_ready(db, s)
    ok = lambda: s.live_gate(exchange_id=eid, strategy_id="Trend AI", strategy_version="2.2")
    assert ok().allowed
    # withdrawal permission appears on the key
    with db.tx() as c:
        c.execute("UPDATE credentials SET withdrawal_permission=1")
    assert not ok().allowed
    with db.tx() as c:
        c.execute("UPDATE credentials SET withdrawal_permission=0")
        c.execute("UPDATE strategy_versions SET release_status='INVALID'")
    assert any("strategy" in r for r in ok().reasons)
    with db.tx() as c:
        c.execute("UPDATE strategy_versions SET release_status='RELEASED'")
        c.execute("UPDATE reconciliation_runs SET result='MISMATCH'")
    assert any("reconciliation" in r for r in ok().reasons)
    with db.tx() as c:
        c.execute("UPDATE reconciliation_runs SET result='OK', finished_at=?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    assert any("stale" in r for r in ok().reasons)
    with db.tx() as c:
        c.execute("UPDATE reconciliation_runs SET finished_at=?", (datetime.now(timezone.utc).isoformat(),))
    assert ok().allowed
    s.activate_kill_switch(ADMIN, "stop")
    assert not ok().allowed
    assert not s.live_gate(exchange_id=999, strategy_id="Trend AI", strategy_version="2.2").allowed


def test_live_order_blocked_when_live_disabled_and_nothing_sent():
    db, _ = tmp_db()
    ad = FakeLive()
    eng, s = engine_for(db, ad)
    r = run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and ad.create_calls == 0
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_kill_switch_blocks_live_and_cancels_unfilled_but_keeps_position():
    db, _ = tmp_db()
    ad = FakeLive("ok")
    eng, s = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, s)
    run(eng.execute(decision("filled"), bot_id="b", environment="LIVE", exchange="fake"))
    ad.mode = "open"
    r = run(eng.execute(decision("resting", symbol="BTC/USDT"), bot_id="b2", environment="LIVE", exchange="fake"))
    assert r.state == "OPEN"
    run(eng.on_kill_switch(ADMIN, "manual stop"))
    assert eng.orders.get(r.order_id)["state"] == "CANCELLED"
    assert len(eng.positions.active("LIVE", "b")) == 1       # position NOT closed
    assert run(eng.execute(decision("new"), bot_id="b", environment="LIVE", exchange="fake")).outcome == "BLOCKED"


def test_adapter_environment_mismatch_refused():
    db, _ = tmp_db()
    eng, s = engine_for(db, PaperAdapter(), exchange="x")
    assert run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="x")).outcome == "BLOCKED"
    eng2, _ = engine_for(db, FakeLive(), exchange="y")
    assert run(eng2.execute(decision(), bot_id="b", environment="PAPER", exchange="y")).outcome == "BLOCKED"


def test_futures_and_unapproved_and_unsigned_refused():
    db, _ = tmp_db()
    pa = PaperAdapter(); pa.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, pa, exchange="paper")
    assert "NOT_IMPLEMENTED" in run(eng.execute(decision(), bot_id="b", environment="PAPER", exchange="paper", market_type="PERPETUAL")).detail
    assert run(eng.execute(decision(approved=False), bot_id="b", environment="PAPER", exchange="paper")).outcome == "BLOCKED"
    assert run(eng.execute(decision(signal_id=""), bot_id="b", environment="PAPER", exchange="paper")).outcome == "BLOCKED"


def test_stub_exchange_returns_not_implemented_never_success():
    from dewako.exchanges.stubs import BybitAdapter
    db, _ = tmp_db()
    s = SafetyController(db)
    eng = ExecutionEngine(db, s, {"bybit": BybitAdapter()}, quote_guard=TEST_QUOTE_GUARD)
    assert run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="bybit")).outcome == "BLOCKED"
    class P(BybitAdapter):  # even if wrongly marked PAPER, the stub refuses and the order ends FAILED
        environment = "PAPER"
    eng = ExecutionEngine(db, s, {"bybit": P()}, quote_guard=TEST_QUOTE_GUARD)
    r = run(eng.execute(decision(), bot_id="b", environment="PAPER", exchange="bybit"))
    # Phase 4.1: the capability gate refuses it before any order row exists (stricter than the old late NOT_IMPLEMENTED failure).
    assert r.outcome == "BLOCKED" and "does not declare order_submission" in r.detail
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_paper_restart_keeps_cash_positions_and_orders():
    """Test E."""
    db, path = tmp_db()
    pa = PaperAdapter(10_000.0); pa.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, pa, exchange="paper")
    RiskStateStore(db).init_scope("PAPER", 10_000.0)
    # Phase 5: limits are evaluated at the executable ask (100.05), not the reference (100). 10 units at the reference sat exactly ON the 10% cap
    # (1000.00) and is now correctly refused at 1000.50; 9.9 units keeps this restart test about persistence, with the limit unchanged.
    r = run(eng.execute(decision(qty=9.9), bot_id="pb", environment="PAPER", exchange="paper"))
    assert r.state == "FILLED"
    cash = pa.cash
    db2 = Database(path)
    from dewako.execution.positions import PositionStore
    pos = PositionStore(db2).active("PAPER", "pb")
    assert len(pos) == 1 and pos[0]["quantity"] == 9.9
    with db2.read() as c:
        assert abs(c.execute("SELECT cash FROM paper_accounts WHERE bot_id='pb'").fetchone()[0] - cash) < 1e-9
    # paper activity never created LIVE rows
    assert PositionStore(db2).active("LIVE") == []


def test_closing_fill_realises_pnl_and_closes_position_and_persists_risk():
    db, path = tmp_db()
    pa = PaperAdapter(10_000.0, slippage=0.0, fee_rate=0.0); pa.set_price("BTC/USDT", 100.0)
    eng, _ = engine_for(db, pa, exchange="paper")
    RiskStateStore(db).init_scope("PAPER", 10_000.0)
    run(eng.execute(decision("buy", qty=10.0), bot_id="pb", environment="PAPER", exchange="paper"))
    pa.set_price("BTC/USDT", 90.0)
    sell = decision("sell", side=Side.SELL, qty=10.0)
    sell = sell.model_copy(update={"signal": sell.signal.model_copy(update={"entry_price": 90.0})})
    r = run(eng.execute(sell, bot_id="pb", environment="PAPER", exchange="paper", intent="EXIT"))
    assert r.state == "FILLED" and eng.positions.active("PAPER") == []
    with db.read() as c:
        assert abs(c.execute("SELECT realized_pnl FROM positions").fetchone()[0] - (-100.0)) < 1e-9
    ctx = RiskStateStore(Database(path)).context("PAPER", "pb", "PAPER")   # fresh process
    assert ctx.consecutive_losses == 1 and ctx.daily_pnl_pct < 0 and ctx.last_loss_at is not None


def test_cannot_close_position_without_exchange_confirmation():
    from dewako.execution.positions import PositionError, PositionStore
    db, _ = tmp_db()
    ps = PositionStore(db)
    with db.tx() as c:
        pid = ps.open_or_add(c, bot_id="b", exchange="e", environment="LIVE", symbol="BTC/USDT", side="BUY", quantity=1, price=10, stop_loss=9)
    with pytest.raises(PositionError):
        ps.set_status(pid, "CLOSED")
    with pytest.raises(PositionError):
        with db.tx() as c:
            ps.reduce(c, bot_id="b", exchange="e", environment="LIVE", symbol="BTC/USDT", quantity=1, price=10, exchange_confirmed=False)
    with pytest.raises(PositionError):
        with db.tx() as c:
            ps.open_or_add(c, bot_id="b", exchange="e", environment="LIVE", symbol="ETH/USDT", side="BUY", quantity=1, price=1, market_type="PERPETUAL")


def test_risk_state_survives_restart_and_rolls_day():
    db, path = tmp_db()
    rs = RiskStateStore(db)
    rs.init_scope("g", 1000.0)
    t0 = datetime.now(timezone.utc)
    rs.record_trade_result("g", -50.0, 950.0, t0)
    rs.record_trade_result("g", -30.0, 920.0, t0)
    rs2 = RiskStateStore(Database(path)); rs2.init_scope("g", 99999.0)   # re-init must not reset
    ctx = rs2.context("g", "b", "PAPER", t0)
    assert ctx.consecutive_losses == 2 and round(ctx.daily_pnl_pct, 1) == -8.0 and round(ctx.drawdown_pct, 1) == 8.0
    # the risk engine rejects on those persisted numbers
    eng = RiskEngine(RiskLimits(max_daily_loss_pct=5))
    assert not eng.evaluate(decision().signal, ctx).approved
    nxt = rs2.context("g", "b", "PAPER", t0 + timedelta(days=1))
    assert nxt.daily_pnl_pct == 0 and nxt.consecutive_losses == 2 and nxt.drawdown_pct > 7


def test_risk_engine_halts_from_durable_kill_switch_and_fails_closed():
    db, _ = tmp_db()
    s = SafetyController(db)
    eng = RiskEngine(kill_switch_active=s.kill_switch_active)
    assert not eng.halted
    s.activate_kill_switch(ADMIN, "x")
    assert eng.halted
    def boom(): raise RuntimeError("db down")
    assert RiskEngine(kill_switch_active=boom).halted


def test_audit_scrubs_secrets_and_is_transactional():
    db, _ = tmp_db()
    a = AuditLog(db)
    a.record("CREDENTIAL_CREATED", "OK", "c1", metadata={"exchange": "binance", "api_secret": "TOPSECRET", "nested": {"password": "pw", "ok": 1}})
    row = a.recent(1)[0]
    assert "TOPSECRET" not in row["metadata"] and "pw" not in row["metadata"].replace("password", "") and '"ok": 1' in row["metadata"]
    assert scrub({"Authorization": "Bearer abc"}) == {"Authorization": "[redacted]"}


def test_trade_events_never_contain_secrets_and_audit_has_trail():
    db, _ = tmp_db()
    ad = FakeLive(); eng, s = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, s)
    run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake", correlation_id="corr-1"))
    acts = [r["action"] for r in AuditLog(db).recent(50)]
    assert {"ORDER_CREATED", "ORDER_SUBMITTED", "ORDER_FILLED"} <= set(acts)
    with db.read() as c:
        assert {r[0] for r in c.execute("SELECT correlation_id FROM order_events")} == {"corr-1"}


def test_exchange_filters_block_or_round_down_and_never_send():
    from dewako.execution.validation import validate_order
    mk = [{"symbol": "BTC/USDT", "tick_size": 0.01, "qty_precision": 3, "min_notional": 10.0, "min_qty": 0.001, "max_qty": 5, "fee": 0.001}]
    bal = {"USDT": 1000.0}
    ok = lambda **k: validate_order(mk, bal, symbol="BTC/USDT", side="BUY", quantity=k.get("q", 1.23456), price=k.get("p", 100.0), order_type=k.get("t", "MARKET"))
    assert ok().problems == [] and ok().quantity == 1.234                 # rounded DOWN, never up
    assert validate_order(mk, bal, symbol="ETH/USDT", side="BUY", quantity=1, price=1).problems     # unknown symbol
    assert ok(q=0.0004).problems                                         # rounds to zero
    assert ok(q=0.05, p=100).problems                                    # notional 5 < 10
    assert ok(q=9).problems                                              # above max qty
    assert validate_order(mk, {"USDT": 50.0}, symbol="BTC/USDT", side="BUY", quantity=1, price=100.0).problems   # balance
    assert validate_order(mk, {}, symbol="BTC/USDT", side="BUY", quantity=1, price=100.0).problems              # unknown balance
    assert ok(t="LIMIT", p=100.005).problems                             # off tick
    assert validate_order([{"symbol": "BTC/USDT"}], bal, symbol="BTC/USDT", side="BUY", quantity=1, price=1).problems  # no step: refuse to guess
    db, _ = tmp_db()
    ad = FakeLive(); ad.balance = 5.0
    eng, s = engine_for(db, ad)
    eng.exchange_ids["fake"] = make_live_ready(db, s)
    r = run(eng.execute(decision(), bot_id="b", environment="LIVE", exchange="fake"))
    assert r.outcome == "BLOCKED" and ad.create_calls == 0
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert c.execute("SELECT status FROM signals").fetchone()[0] == "REJECTED"


@pytest.mark.parametrize(
    ("market_updates", "balances"),
    [
        ({"step_size": -0.3}, {"USDT": 1000.0}),
        ({"step_size": float("inf")}, {"USDT": 1000.0}),
        ({"step_size": float("nan")}, {"USDT": 1000.0}),
        ({"step_size": 0.001, "fee": -0.1}, {"USDT": 1000.0}),
        ({"step_size": 0.001, "min_notional": float("inf")}, {"USDT": 1000.0}),
        ({"step_size": 0.001}, {"USDT": float("nan")}),
        ({"step_size": 0.001}, {"USDT": float("inf")}),
        ({"step_size": 0.001}, {"USDT": -1.0}),
    ],
)
def test_invalid_exchange_filters_and_balances_fail_closed(market_updates, balances):
    from dewako.execution.validation import validate_order

    market = {"symbol": "BTC/USDT", "qty_precision": 3, "min_notional": 1.0}
    market.update(market_updates)
    result = validate_order([market], balances, symbol="BTC/USDT", side="BUY", quantity=1.0, price=100.0)
    assert result.problems
    assert result.quantity == 0.0


def test_quantity_rounding_never_exceeds_the_requested_size():
    from dewako.execution.validation import validate_order

    markets = [{"symbol": "BTC/USDT", "step_size": 1.0, "min_qty": 0.1, "min_notional": 1.0}]
    result = validate_order(markets, {"USDT": 1000.0}, symbol="BTC/USDT", side="BUY",
                            quantity=0.9999999999, price=100.0)
    assert result.problems
    assert result.quantity == 0.0
