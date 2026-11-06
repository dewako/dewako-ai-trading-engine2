"""Stage 3B step 5: the report data is read-only, plain, and shows the limits."""
import ast
import pathlib

import pytest

from dewako.backtesting import gate, report
from dewako.persistence.audit import AuditLog
from dewako.strategy_packages.manager import PackageManager
from hardening_helpers import *
from stage3b_evidence import m, record_run
from test_stage3b_gate import FP, store
from test_strategy_packages import ANALYST, TRADER, _unreg, build

MAIN = pathlib.Path(__file__).resolve().parents[1].joinpath("dewako/api/main.py").read_text()


def test_summary_has_what_a_table_needs_and_says_simulated():
    s, _ = store()
    rid, _ = record_run(s, ANALYST, "S", "1.0", FP)
    x = report.summary(s.get(rid))
    assert (x["id"], x["strategy"], x["version"], x["verdict"], x["simulated"], x["trades"], x["net_pnl"]) == (rid, "S", "1.0", "PASSED", True, 150, 50.0)
    assert x["dataset_hash"] == "a" * 64 and x["digest"] == "d" * 64 and x["created_by"] == "analyst"


def test_detail_recomputes_the_official_gate_and_always_carries_the_limits():
    s, _ = store()
    from dewako.backtesting.thresholds import Thresholds
    lax = Thresholds(min_trades=1, max_drawdown_pct=0.99, cost_factor_that_must_stay_positive=1.0, approved_by="x")
    rid, _ = record_run(s, ANALYST, "S", "1.0", FP, th=lax, full=m(trades=5, net_pnl=1.0, max_drawdown_pct=0.4))
    d = report.detail(s.get(rid), current=True)
    assert d["verdict"] == "PASSED" and d["official_gate"]["ok"] is False          # the stored verdict is shown, but the gate's own answer too
    assert any("enough trades" in w for w in d["weaknesses"]) and d["counts_for_promotion"] is True
    assert d["thresholds"]["min_trades"] == 1                                       # and what the run was recorded under is visible
    assert len(d["limits"]) == 4 and "SIMULATED" in d["limits"][0] and "locked" in d["limits"][2]
    assert set(d["stress"]) == {"1", "2", "3"} and len(d["periods"]) == 4


def test_detail_marks_older_runs_as_not_counting():
    s, _ = store()
    a, _ = record_run(s, ANALYST, "S", "1.0", FP)
    b, _ = record_run(s, ANALYST, "S", "1.0", FP)
    assert report.detail(s.get(a), current=False)["counts_for_promotion"] is False
    assert report.detail(s.get(b), current=True)["counts_for_promotion"] is True


def test_package_evidence_shows_what_the_gate_sees_without_changing_anything():
    mm = PackageManager((db := tmp_db()[0]), AuditLog(db))
    pid = mm.import_package(build("Report Path"), ADMIN)["id"]
    try:
        e = mm.evidence(pid)
        assert e["gate"]["ok"] is False and e["latest_run"] is None and e["state"] == "VALIDATED" and "no backtest evidence" in e["gate"]["reasons"][0]
        record_run(mm.runs, ANALYST, "Report Path", "1.0", mm.get(pid)["fingerprint"])
        e = mm.evidence(pid)
        assert e["gate"]["ok"] is True and e["latest_run"]["verdict"] == "PASSED" and e["state"] == "VALIDATED"
        with pytest.raises(KeyError):
            mm.evidence(99999)
    finally:
        _unreg(mm)


def routes():
    out = {}
    for n in ast.walk(ast.parse(MAIN)):
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post", "put", "patch", "delete") and d.args:
                    out[n.name] = (d.func.attr, ast.literal_eval(d.args[0]), ast.unparse(n))
    return out


def test_the_backtest_routes_are_read_only_authenticated_and_never_take_metrics_from_a_client():
    r = {k: v for k, v in routes().items() if v[1].startswith("/backtests")}
    assert set(r) == {"list_backtest_runs", "get_backtest_run"}
    for name, (method, path, src) in r.items():
        assert method == "get", name
        assert "permission('backtests')" in src, name
        assert "RunStore" not in src and "runs.save" not in src and "BaseModel" not in src, name
    assert "runs.save" not in MAIN and "RunStore" not in MAIN and "validate_strategy" not in MAIN and "replay(" not in MAIN   # the API cannot start or write a run


def test_the_evidence_and_review_routes_need_a_logged_in_strategies_user():
    r = routes()
    assert r["strategy_package_evidence"][0] == "get" and r["review_existing_packages"][0] == "post"
    for k in ("strategy_package_evidence", "review_existing_packages"):
        assert "permission('strategies')" in r[k][2]


def test_review_existing_is_not_called_by_anything_but_its_route():
    import re
    hits = []
    for f in pathlib.Path(__file__).resolve().parents[1].joinpath("dewako").rglob("*.py"):
        if re.search(r"\.review_existing\(", f.read_text()):
            hits.append(f.name)
    assert sorted(hits) == ["main.py"]                                                          # no timer, worker or agent ever calls it
