"""Stage 3B step 5 (UI): the Backtesting page shows only stored server records; the standalone bundle matches its sources."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
UI = ROOT / "frontend" / "prototype"
NODE = shutil.which("node")
REQUIRE_NODE = os.environ.get("DEWAKO_REQUIRE_NODE") == "1"     # CI sets it: a missing Node must fail there, not skip
NO_NODE = NODE is None and not REQUIRE_NODE


def test_node_is_available_where_it_is_required():
    assert NODE is not None or not REQUIRE_NODE, "DEWAKO_REQUIRE_NODE=1 but node is not installed: the UI rendering tests did not run"


def test_the_standalone_bundle_is_rebuilt_from_its_sources():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_standalone
    assert build_standalone.build() == (UI / "index.standalone.html").read_text(), "run: python scripts/build_standalone.py"


def test_the_old_random_backtest_generator_is_gone_and_the_page_only_reads():
    js = (UI / "app.js").read_text()
    for gone in ("runBt", "btHtml", "btForm", "bStrat", "genCandles({...m,seed:m.seed+Math.round(days)}"):
        assert gone not in js, gone
    page = js[js.index("const BT="):js.index("function btDetail")]
    assert "method" not in page and "POST" not in page and "json:" not in page
    assert "bk('/backtests/runs')" in page
    assert "backtests.js" in (UI / "index.html").read_text()


@pytest.mark.skipif(NO_NODE, reason="node is not installed")
def test_hostile_text_in_a_record_is_rendered_inert():
    evil = '<img src=x onerror=alert(1)>"\'<script>alert(2)</script>'
    run = {"id": 1, "strategy": evil, "version": evil, "symbol": evil, "timeframe": "5m", "verdict": "PASSED", "trades": 1, "net_pnl": 1,
           "max_drawdown_pct": 0.1, "dataset_start": "2025-01-01T00:00:00+00:00", "dataset_end": "2026-01-01T00:00:00+00:00"}
    detail = dict(run, official_gate={"ok": False, "reasons": [evil], "notes": [evil]}, metrics={"trades": 1}, stress={"1": {"net_pnl": 1, "trades": 1}},
                  in_sample={}, out_of_sample={}, periods=[{"net_pnl": 1, "trades": 1}], weaknesses=[evil], limits=[evil], created_by=evil, created_at=evil,
                  dataset_hash=evil, digest=evil, code_version=evil, thresholds={"approved_by": evil}, counts_for_promotion=True,
                  monte_carlo={"runs": evil, "p50": 0.1, "p95": 0.2, "worst": 0.3})
    code = ("const B=require(process.argv[1]);const d=JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify([B.renderRows([d.run]),B.renderDetail(d.detail),B.renderRows([]),B.describeError(403)]))")
    out = subprocess.run([NODE, "-e", code, str(UI / "backtests.js"), json.dumps({"run": run, "detail": detail})], capture_output=True, text=True, check=True)
    for html in json.loads(out.stdout)[:2]:
        assert "<script" not in html and "<img" not in html and not re.search(r"<[^>]*\sonerror=", html)
        assert "&lt;img" in html
    assert "cannot be started" in json.loads(out.stdout)[2] or "cannot start" in json.loads(out.stdout)[2]


@pytest.mark.skipif(NO_NODE, reason="node is not installed")
def test_missing_numbers_show_as_not_available_never_as_zero():
    code = ("const B=require(process.argv[1]);console.log(B.renderDetail({verdict:'FAILED',metrics:{trades:0,net_pnl:null,win_rate:null,profit_factor:null},"
            "official_gate:{ok:false,reasons:[]},stress:{},periods:[],weaknesses:[],limits:[]}))")
    out = subprocess.run([NODE, "-e", code, str(UI / "backtests.js")], capture_output=True, text=True, check=True).stdout
    assert out.count("n/a") >= 3 and "NaN" not in out and "undefined" not in out
