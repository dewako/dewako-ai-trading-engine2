"""The local backtest GUI: request validation (no shell injection, fee floor), the local-only guards, and one real end-to-end screening job."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from dewako.backtesting import gui  # noqa: E402
from test_stage3b_cli import candle_file  # noqa: E402


def run_body(store, **kw):
    b = {"store": store, "strategy": "Trend AI", "version": "2.1", "symbol": "BTC/USDT", "timeframe": "5m", "source": "src",
         "start": "2026-01-05", "end": "2026-01-06", "mode": "screen"}
    b.update(kw)
    return b


def test_a_valid_run_becomes_an_argument_list_with_the_safety_flags(tmp_path):
    store = candle_file(tmp_path, 20)
    cmd, env = gui.build_command("run", run_body(store), work_dir=tmp_path)
    assert isinstance(cmd, list) and "--dry-run" in cmd and "--screen" in cmd and "--streak-reset" in cmd
    assert cmd[cmd.index("--fee-rate") + 1] == "0.001" and env["DEWAKO_TRADING_DB"] == str(tmp_path / "gui_runs.db")
    full, _ = gui.build_command("run", run_body(store, mode="full"), work_dir=tmp_path)
    assert "--screen" not in full and "--dry-run" in full                                   # a full run is still never saved


@pytest.mark.parametrize("fee", [0, 0.0005, -1, "abc", None, float("nan")])
def test_a_fee_below_one_tenth_of_a_percent_or_not_a_number_is_refused(tmp_path, fee):
    with pytest.raises(gui.BadRequest):
        gui.build_command("run", run_body(candle_file(tmp_path, 20), fee_rate=fee), work_dir=tmp_path)


@pytest.mark.parametrize("field,val", [("symbol", "BTC/USDT; rm -rf /"), ("strategy", "x$(id)"), ("source", "a|b"), ("start", "2020-01-01; ls"),
                                       ("timeframe", "7m"), ("mode", "both"), ("lookback", 5), ("cash", -5)])
def test_shell_characters_and_out_of_range_values_are_refused(tmp_path, field, val):
    with pytest.raises(gui.BadRequest):
        gui.build_command("run", run_body(candle_file(tmp_path, 20), **{field: val}), work_dir=tmp_path)


def test_unknown_actions_and_missing_files_are_refused(tmp_path):
    with pytest.raises(gui.BadRequest):
        gui.build_command("shell", {}, work_dir=tmp_path)
    with pytest.raises(gui.BadRequest):
        gui.build_command("import", {"zip": str(tmp_path / "nope.zip"), "store": "x.db"}, work_dir=tmp_path)
    with pytest.raises(gui.BadRequest):
        gui.build_command("run", run_body(str(tmp_path / "missing.db")), work_dir=tmp_path)


def test_spike_times_are_read_whole_from_the_data_check_message():
    msg = ("DATA REJECTED: SUSPECT_SPIKE: 2025-02-03T02:05:00+00:00: range 17.8% inside one candle; "
           "SUSPECT_SPIKE: 2025-10-10T21:55:00+00:00: range 13.1% inside one candle")
    assert gui._SPIKE.findall(msg) == ["2025-02-03T02:05:00+00:00", "2025-10-10T21:55:00+00:00"]


def test_coverage_lists_what_the_store_holds_read_only(tmp_path):
    rows = gui.coverage(candle_file(tmp_path, 50))
    assert rows == [{"symbol": "BTC/USDT", "timeframe": "5m", "source": "src", "first": "2026-01-05", "last": "2026-01-05", "candles": 50}]
    with pytest.raises(gui.BadRequest):
        gui.coverage(str(tmp_path / "none.db"))


@pytest.fixture()
def server(tmp_path):
    gui.WORK = tmp_path
    jobs = gui.Jobs(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = gui.make_handler("tok", jobs, tmp_path, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, jobs
    srv.shutdown()


def call(port, path, body=None, token="tok", host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Token": token, "Content-Type": "application/json", **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_server_refuses_a_missing_token_and_a_foreign_host_header(server):
    port, _ = server
    assert call(port, "/api/job", token="wrong")[0] == 403
    assert call(port, "/api/job", host="evil.example")[0] == 403
    assert call(port, "/api/job", body={}, token="")[0] == 403
    assert call(port, "/api/job")[0] == 200
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:                # the page itself carries the token
        assert b"Dewako backtest" in r.read()


PLUGIN = """
from dewako.core.types import Side, Signal
from dewako.strategies import base


@base.register
class GuiPlugin(base.Strategy):
    name, version, supported_timeframes, default_parameters = "Gui Plugin", "1.0", ["5m"], {}

    def generate_signal(self, state):
        n = int(state.candles[-1].t.timestamp() // 300)
        if n % 10:
            return None
        c = state.candles[-1].c
        return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name, strategy_version=self.version,
                      reason=["x"], entry_price=c, stop_price=c * 0.97)

    def manage_position(self, state, position):
        if int(state.candles[-1].t.timestamp() // 300) % 10 == 5:
            return Signal(symbol=state.symbol, side=Side.SELL, confidence=0.9, strategy=self.name, strategy_version=self.version,
                          reason=["x"], entry_price=state.candles[-1].c)
"""


def wait(port):
    for _ in range(400):
        _c, j = call(port, "/api/job")
        if j["status"] == "done":
            return j
        time.sleep(1)
    raise AssertionError("job did not finish")


def test_a_screening_job_with_the_persons_own_strategy_file_runs_end_to_end_and_saves_nothing(server, tmp_path):
    port, _ = server
    (tmp_path / "plug.py").write_text(PLUGIN)
    store = candle_file(tmp_path, 300)
    body = run_body(store, strategy="Gui Plugin", version="1.0", end="2026-01-06T01:00", lookback=70, plugin=str(tmp_path / "plug.py"))
    assert call(port, "/api/run", body)[0] == 200
    j = wait(port)
    assert j["code"] == 0, j["stderr"]
    r = j["result"]
    assert r["mode"] == "SCREENING_ONLY" and r["saved"] is False and r["trades"] > 5 and "verdict" not in r
    assert call(port, "/api/run", body, token="wrong")[0] == 403


def test_a_full_job_shows_checks_and_a_verdict_and_still_saves_no_record(server, tmp_path):
    port, _ = server
    (tmp_path / "plug.py").write_text(PLUGIN)
    store = candle_file(tmp_path, 300)
    body = run_body(store, strategy="Gui Plugin", version="1.0", end="2026-01-06T01:00", lookback=70, mode="full", plugin=str(tmp_path / "plug.py"))
    assert call(port, "/api/run", body)[0] == 200
    j = wait(port)
    assert j["code"] == 0, j["stderr"]
    assert j["result"]["verdict"] in ("PASSED", "FAILED") and j["result"]["checks"] and j["result"]["run_id"] is None


def test_only_one_job_runs_at_a_time(server, tmp_path):
    port, jobs = server
    jobs.job = {"id": "x", "label": "run", "status": "running", "stdout": "", "stderr": "", "code": None, "started": time.time()}
    store = candle_file(tmp_path, 300)
    code, out = call(port, "/api/run", run_body(store, end="2026-01-06T01:00"))
    assert code == 400 and "already running" in out["error"]
