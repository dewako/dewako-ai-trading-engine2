"""Stage 3B step 4: run records are append-only evidence, and the promotion gate judges them itself."""
import json
import os
import subprocess
import sys

import pytest

from dewako.backtesting import gate
from dewako.backtesting.records import RunRefused, RunStore
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database
from dewako.strategy_packages.manager import PackageManager, TransitionRefused
from hardening_helpers import *
from stage3b_evidence import GOOD, config, dataset, m, record_run, validation
from test_strategy_packages import ANALYST, TRADER, _unreg, build

FP = "f" * 64


def store():
    db, _ = tmp_db()
    return RunStore(db, AuditLog(db)), db


def audit_rows(db, action):
    with db.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,)).fetchall()]


# ------------------------------------------------------------------ records
def test_a_saved_run_round_trips_with_everything_needed_to_identify_it():
    s, db = store()
    rid, v = record_run(s, ANALYST, "S", "1.0", FP)
    r = s.get(rid)
    assert (r["strategy_name"], r["strategy_version"], r["strategy_fingerprint"], r["verdict"], r["simulated"]) == ("S", "1.0", FP, "PASSED", 1)
    assert r["dataset_hash"] == "a" * 64 and r["run_digest"] == "d" * 64 and r["created_by"] == "analyst"
    assert r["dataset"]["start"] < r["dataset"]["end"] and r["validation"]["thresholds"]["min_trades"] == 100
    assert r["validation"]["placeholder_thresholds"] is False
    from dewako.backtesting.metrics import Metrics
    assert Metrics.from_dict(r["metrics"]["full"]) == v.full
    assert s.latest("S", "1.0")["id"] == rid and s.latest("S", "2.0") is None and [x["id"] for x in s.list()] == [rid]
    (a,) = audit_rows(db, "BACKTEST_RUN_RECORDED")
    assert a["resource"] == f"backtest_run:{rid}" and a["result"] == "OK" and json.loads(a["metadata"])["verdict"] == "PASSED"


def test_a_failing_validation_is_recorded_as_failed_not_dropped():
    s, _ = store()
    rid, _ = record_run(s, ANALYST, "S", "1.0", FP, full=m(trades=5, net_pnl=-1.0, max_drawdown_pct=0.5))
    assert s.get(rid)["verdict"] == "FAILED"


def test_runs_are_append_only_at_the_database():
    s, db = store()
    rid, _ = record_run(s, ANALYST, "S", "1.0", FP)
    for sql in ("UPDATE backtest_runs SET verdict='PASSED'", "DELETE FROM backtest_runs"):
        with pytest.raises(Exception, match="append-only"):
            with db.tx() as c:
                c.execute(sql)
    assert s.get(rid)["verdict"] == "PASSED"


def test_saving_needs_the_backtests_permission_and_consistent_inputs():
    s, db = store()
    with pytest.raises(RunRefused, match="backtests"):
        record_run(s, {"user_id": 9, "username": "v", "role": "VIEWER"}, "S", "1.0", FP)
    assert audit_rows(db, "BACKTEST_RUN_RECORDED")[0]["result"] == "DENIED" and s.list() == []
    v = validation("S", "1.0")
    with pytest.raises(RunRefused, match="different things"):
        s.save(v, dataset(), config("OTHER", "1.0"), FP, ANALYST)
    with pytest.raises(RunRefused, match="different things"):
        s.save(v, dataset(symbol="BTC/USDT"), config("S", "1.0"), FP, ANALYST)
    with pytest.raises(RunRefused, match="fingerprint"):
        s.save(v, dataset(), config("S", "1.0"), "", ANALYST)
    v.results.clear()
    with pytest.raises(RunRefused, match="unstressed"):
        s.save(v, dataset(), config("S", "1.0"), FP, ANALYST)
    assert s.list() == []


# ------------------------------------------------------------------ gate
def test_no_evidence_means_no():
    s, _ = store()
    g = gate.check(s, "S", "1.0", FP)
    assert not g.ok and "no backtest evidence" in g.reasons[0] and g.run_id is None


def test_passing_evidence_for_the_current_definition_passes():
    s, _ = store()
    rid, _ = record_run(s, ANALYST, "S", "1.0", FP)
    g = gate.check(s, "S", "1.0", FP)
    assert g.ok and g.reasons == [] and g.run_id == rid


def test_only_the_newest_run_counts_a_later_failure_replaces_an_earlier_pass():
    s, _ = store()
    record_run(s, ANALYST, "S", "1.0", FP)
    rid2, _ = record_run(s, ANALYST, "S", "1.0", FP, full=m(trades=20, net_pnl=-3.0, max_drawdown_pct=0.2))
    g = gate.check(s, "S", "1.0", FP)
    assert not g.ok and g.run_id == rid2
    assert any("enough trades" in r for r in g.reasons)


def test_changed_rules_invalidate_the_evidence():
    s, _ = store()
    record_run(s, ANALYST, "S", "1.0", FP)
    g = gate.check(s, "S", "1.0", "0" * 64)
    assert not g.ok and "different definition" in g.reasons[0]


def test_the_gate_recomputes_and_ignores_a_verdict_recorded_under_lax_thresholds():
    s, _ = store()
    from dewako.backtesting.thresholds import Thresholds
    lax = Thresholds(min_trades=1, max_drawdown_pct=0.99, cost_factor_that_must_stay_positive=1.0, approved_by="someone")
    rid, v = record_run(s, ANALYST, "S", "1.0", FP, th=lax, full=m(trades=5, net_pnl=1.0, max_drawdown_pct=0.4), stress2=m(net_pnl=-9.0))
    assert s.get(rid)["verdict"] == "PASSED"                       # passed its own lax limits
    g = gate.check(s, "S", "1.0", FP)
    assert not g.ok
    names = " | ".join(g.reasons)
    assert "enough trades" in names and "positive at 2x costs" in names and "drawdown within limit" in names


@pytest.mark.parametrize("kw,needle", [
    (dict(days=364), "history too short"),
    (dict(holdout=0.24), "hold-out too small"),
    (dict(oos=m(expectancy=0.49)), "out-of-sample holds up"),
    (dict(periods=[m(net_pnl=1.0), m(net_pnl=-1.0), m(net_pnl=-1.0)]), "consistent across periods"),
])
def test_each_official_limit_blocks_on_its_own(kw, needle):
    s, _ = store()
    record_run(s, ANALYST, "S", "1.0", FP, **kw)
    g = gate.check(s, "S", "1.0", FP)
    assert not g.ok and any(needle in r for r in g.reasons), g.reasons


def test_boundary_values_pass():
    s, _ = store()
    record_run(s, ANALYST, "S", "1.0", FP, days=365, holdout=0.25, full=m(trades=100, net_pnl=1.0, expectancy=1.0, max_drawdown_pct=0.15),
               oos=m(expectancy=0.5))
    assert gate.check(s, "S", "1.0", FP).ok


def test_an_unreadable_record_is_not_evidence():
    s, db = store()
    with db.tx() as c:
        c.execute("INSERT INTO backtest_runs(created_at,created_by,strategy_name,strategy_version,strategy_fingerprint,symbol,timeframe,"
                  "dataset_hash,dataset,config,run_digest,code_version,metrics,validation,verdict,simulated) "
                  "VALUES ('2026-01-01T00:00:00+00:00','x','S','1.0',?,'XAUUSD','5m','h','{}','{}','d','c','{}','{}','PASSED',1)", (FP,))
    g = gate.check(s, "S", "1.0", FP)
    assert not g.ok and "unreadable" in g.reasons[0]


def test_variants_tried_are_shown_but_do_not_block():
    s, _ = store()
    v = validation("S", "1.0")
    v.variants_tried = 7
    s.save(v, dataset(), config("S", "1.0"), FP, ANALYST)
    g = gate.check(s, "S", "1.0", FP)
    assert g.ok and "7 variants" in g.notes[0]


def test_a_run_cannot_be_recorded_as_not_simulated():
    s, db = store()
    record_run(s, ANALYST, "S", "1.0", FP)
    with pytest.raises(Exception):
        with db.tx() as c:
            c.execute("INSERT INTO backtest_runs(created_at,created_by,strategy_name,strategy_version,strategy_fingerprint,symbol,timeframe,"
                      "dataset_hash,dataset,config,run_digest,code_version,metrics,validation,verdict,simulated) "
                      "VALUES ('t','x','S','1.0','f','X','5m','h','{}','{}','d','c','{}','{}','PASSED',0)")


def test_the_gate_and_record_store_do_not_load_the_replay_or_execution_stack():
    code = ("import sys; sys.path.insert(0, '.'); import dewako.backtesting.gate, dewako.backtesting.records, dewako.strategy_packages.manager;"
            "bad=[m for m in sys.modules if m.startswith(('dewako.execution','dewako.backtesting.replay','dewako.backtesting.sim_adapter','dewako.exchanges'))];"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.join(os.path.dirname(__file__), ".."), check=True)
    assert out.stdout.strip() == "[]", out.stdout


# ------------------------------------------------------------------ the promotion path
def pkg(name):
    mm, db = (lambda d: (PackageManager(d, AuditLog(d)), d))(tmp_db()[0])
    pid = mm.import_package(build(name), ADMIN)["id"]
    mm.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)
    return mm, db, pid


def test_promotion_is_refused_without_evidence_audited_with_reasons_and_allowed_with_it():
    mm, db, pid = pkg("Gate Path A")
    try:
        with pytest.raises(TransitionRefused, match="backtest evidence"):
            mm.transition(pid, "PAPER_ELIGIBLE", TRADER)
        assert mm.get(pid)["state"] == "BACKTEST_ELIGIBLE"
        denied = [a for a in audit_rows(db, "STRATEGY_PACKAGE_STATE") if a["result"] == "DENIED"]
        assert denied and "no passing backtest evidence" in denied[-1]["metadata"] and "no backtest evidence" in denied[-1]["metadata"]
        fp = mm.get(pid)["fingerprint"]
        record_run(mm.runs, ANALYST, "Gate Path A", "1.0", fp, full=m(trades=3, net_pnl=-1.0, max_drawdown_pct=0.3))
        with pytest.raises(TransitionRefused, match="enough trades"):
            mm.transition(pid, "PAPER_ELIGIBLE", TRADER)
        record_run(mm.runs, ANALYST, "Gate Path A", "1.0", fp)
        assert mm.transition(pid, "PAPER_ELIGIBLE", TRADER)["state"] == "PAPER_ELIGIBLE"
        assert mm.transition(pid, "TESTNET_ELIGIBLE", TRADER)["state"] == "TESTNET_ELIGIBLE"
        assert mm.transition(pid, "PAPER_ELIGIBLE", TRADER)["state"] == "PAPER_ELIGIBLE"      # a demotion is never gated
    finally:
        _unreg(mm)


def test_stale_evidence_after_new_failing_run_blocks_testnet_label_too():
    mm, db, pid = pkg("Gate Path B")
    try:
        fp = mm.get(pid)["fingerprint"]
        record_run(mm.runs, ANALYST, "Gate Path B", "1.0", fp)
        mm.transition(pid, "PAPER_ELIGIBLE", TRADER)
        record_run(mm.runs, ANALYST, "Gate Path B", "1.0", fp, full=m(trades=150, net_pnl=-4.0, max_drawdown_pct=0.4))
        with pytest.raises(TransitionRefused, match="positive at 2x|drawdown"):
            mm.transition(pid, "TESTNET_ELIGIBLE", TRADER)
    finally:
        _unreg(mm)


def test_existing_promoted_packages_without_evidence_are_demoted_only_by_a_human_and_only_one_step_to_backtest():
    mm, db, pid = pkg("Gate Path C")
    mm2pid = mm.import_package(build("Gate Path D"), ADMIN)["id"]
    try:
        mm.transition(mm2pid, "BACKTEST_ELIGIBLE", ADMIN)
        record_run(mm.runs, ANALYST, "Gate Path D", "1.0", mm.get(mm2pid)["fingerprint"])
        mm3pid = mm.import_package(build("Gate Path E"), ADMIN)["id"]
        mm.transition(mm3pid, "BACKTEST_ELIGIBLE", ADMIN)
        for p, st in ((pid, "PAPER_ELIGIBLE"), (mm2pid, "PAPER_ELIGIBLE"), (mm3pid, "TESTNET_ELIGIBLE")):   # legacy: promoted before the gate
            with db.tx() as c:
                c.execute("UPDATE strategy_packages SET state=? WHERE id=?", (st, p))
        with pytest.raises(TransitionRefused, match="permission"):
            mm.review_existing(ANALYST)
        assert mm.get(pid)["state"] == "PAPER_ELIGIBLE"                                        # nothing happens by itself
        out = mm.review_existing(TRADER)
        assert sorted((o["id"], o["from"]) for o in out) == sorted([(pid, "PAPER_ELIGIBLE"), (mm3pid, "TESTNET_ELIGIBLE")])
        assert mm.get(pid)["state"] == "BACKTEST_ELIGIBLE" and mm.get(mm3pid)["state"] == "BACKTEST_ELIGIBLE"
        assert mm.get(mm2pid)["state"] == "PAPER_ELIGIBLE"
        assert mm.review_existing(TRADER) == []                                                # idempotent
        assert any("no passing backtest evidence" in a["metadata"] for a in audit_rows(db, "STRATEGY_PACKAGE_STATE"))
    finally:
        _unreg(mm)
