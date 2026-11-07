"""Stage 3B step 6: the command that produces run records."""
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from dewako.backtesting import cli  # noqa: E402
from dewako.backtesting.records import RunStore  # noqa: E402
from dewako.core.types import Candle, DataQuality  # noqa: E402
from dewako.market_data.models import CandleRecord  # noqa: E402
from dewako.market_data.store import CandleStore  # noqa: E402
from dewako.persistence.audit import AuditLog  # noqa: E402
from dewako.strategies import base  # noqa: E402
from dewako.strategies.versions import fingerprint  # noqa: E402
from dewako.strategy_packages.manager import PackageManager  # noqa: E402
from hardening_helpers import ADMIN, tmp_db  # noqa: E402
from test_stage3b_validation import make_cycle  # noqa: E402
from test_strategy_packages import _unreg, build  # noqa: E402

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)


def candle_file(tmp_path, n=100, drop=()):
    rng, p, recs = random.Random(3), 100.0, []
    for i in range(n):
        o = p
        p = p * math.exp(0.003 + rng.uniform(-0.0005, 0.0005))
        if i in drop:
            continue
        c = Candle(t=T0 + M5 * i, o=o, h=max(o, p) * 1.0002, l=min(o, p) * 0.9998, c=p, v=1e9)
        recs.append(CandleRecord(symbol="BTC/USDT", timeframe="5m", candle=c, quality=DataQuality(
            source="src", data_timestamp=c.t, publication_timestamp=c.t + M5, ingested_at=c.t + M5)))
    path = str(tmp_path / "candles.db")
    CandleStore(path).put_candles(recs, now=T0 + timedelta(days=2))
    return path


def args(path, name, n=100, *extra):
    return ["--candles", path, "--symbol", "BTC/USDT", "--timeframe", "5m", "--source", "src", "--start", T0.isoformat(),
            "--end", (T0 + M5 * n).isoformat(), "--strategy", name, "--version", "1.0", "--lookback", "20", "--mc-runs", "20", *extra]


def test_a_run_is_recorded_and_printed_and_never_promotes_anything(tmp_path, capsys):
    make_cycle("Cli Cycle")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path), "Cli Cycle", 100, "--json"), db=db) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["simulated"] is True and out["run_id"] == 1 and out["verdict"] in ("PASSED", "FAILED")
    run = RunStore(db, AuditLog(db)).get(1)
    assert (run["strategy_name"], run["created_by"].startswith("cli:"), run["simulated"]) == ("Cli Cycle", True, 1)
    assert run["strategy_fingerprint"] == fingerprint(base.get("Cli Cycle", "1.0")) and run["dataset_hash"] == out["dataset_hash"]
    assert run["verdict"] == out["verdict"]
    assert "enough trades" in {c["name"] for c in out["checks"]}                       # the 100-trade limit is applied and shown
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM strategy_packages").fetchone()[0] == 0


def test_dry_run_saves_nothing(tmp_path, capsys):
    make_cycle("Cli Dry")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path), "Cli Dry", 100, "--dry-run"), db=db) == 0
    assert "not saved" in capsys.readouterr().out and RunStore(db, AuditLog(db)).list() == []


def test_bad_data_is_refused_before_any_replay_and_names_the_problem(tmp_path, capsys):
    make_cycle("Cli Gap")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path, drop=(40,)), "Cli Gap", 100), db=db) == 2
    err = capsys.readouterr().err
    assert "DATA REJECTED" in err and "GAP" in err and RunStore(db, AuditLog(db)).list() == []


def test_a_human_explained_gap_is_accepted_and_recorded_with_the_run(tmp_path):
    make_cycle("Cli Gap OK")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path, drop=(40,)), "Cli Gap OK", 100, "--allow-gap", (T0 + M5 * 40).isoformat(), "--dry-run"), db=db) == 0


def test_unknown_strategy_is_refused(tmp_path, capsys):
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path), "Nope", 100), db=db) == 2
    assert "unknown strategy" in capsys.readouterr().err


def test_variants_tried_travels_with_the_record(tmp_path, capsys):
    make_cycle("Cli Variants")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path), "Cli Variants", 100, "--variants-tried", "5"), db=db) == 0
    run = RunStore(db, AuditLog(db)).get(1)
    assert run["validation"]["variants_tried"] == 5 and any("5 variants" in w for w in run["validation"]["warnings"])


def test_a_recorded_package_run_matches_the_package_fingerprint_so_the_gate_can_use_it(tmp_path):
    db, _ = tmp_db()
    mm = PackageManager(db, AuditLog(db))
    pid = mm.import_package(build("Cli Pkg"), ADMIN)["id"]
    try:
        mm.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)
        assert fingerprint(base.get("Cli Pkg", "1.0")) == mm.get(pid)["fingerprint"]
    finally:
        _unreg(mm)


def test_screening_mode_is_labelled_saves_nothing_has_no_verdict_and_matches_the_full_runs_1x_numbers(tmp_path, capsys):
    make_cycle("Cli Screen")
    db, _ = tmp_db()
    path = candle_file(tmp_path, 300)
    more = ("--lookback", "70")                                                               # enough bars for the real market-state builder to trade
    assert cli.main(args(path, "Cli Screen", 300, "--json", "--screen", *more), db=db) == 0   # no --dry-run: it must still save nothing
    s = json.loads(capsys.readouterr().out)
    assert s["mode"] == "SCREENING_ONLY" and s["saved"] is False and s["simulated"] is True
    assert "verdict" not in s and "run_id" not in s and "checks" not in s
    assert any("SCREENING ONLY" in w for w in s["warnings"])
    assert RunStore(db, AuditLog(db)).list() == []
    assert cli.main(args(path, "Cli Screen", 300, "--json", "--dry-run", *more), db=db) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["trades"] > 5                                                                 # a comparison of two zeros would prove nothing
    assert (s["trades"], s["net_pnl"], s["max_drawdown_pct"], s["dataset_hash"]) == (full["trades"], full["net_pnl"], full["max_drawdown_pct"], full["dataset_hash"])


def test_screening_mode_cannot_produce_a_run_the_gate_could_use(tmp_path, capsys):
    make_cycle("Cli Screen2")
    db, _ = tmp_db()
    assert cli.main(args(candle_file(tmp_path), "Cli Screen2", 100, "--screen"), db=db) == 0
    out = capsys.readouterr().out
    assert out.startswith("SCREENING ONLY") and "not saved" in out and RunStore(db, AuditLog(db)).list() == []
