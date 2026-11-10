"""Examination of the 1.0.6 result (after the gate; changes nothing): re-plays the same full-window run once per coin and saves the
trade list, to measure how much of the profit rests on the few largest trades. Usage: python examine.py"""
import contextlib, io, json, os, subprocess, sys
B = "/home/claude/dewako-ai-trading-engine2/backend"; H = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 1:                                             # child: one replay, trades dumped
    sys.path.insert(0, B)
    import dewako.backtesting.replay as R
    from dewako.backtesting.gui import _cli_with_plugin
    real, out = R.replay, sys.argv[1]
    def spy(ds, cfg, **kw):
        r = real(ds, cfg, **kw)
        json.dump([dict(entry=t.entry_time.isoformat(), exit=t.exit_time.isoformat(), pnl=t.pnl, reason=t.exit_reason) for t in r.trades], open(out, "w"))
        return r
    R.replay = spy
    with contextlib.redirect_stdout(io.StringIO()):
        _cli_with_plugin(sys.argv[2:])
    raise SystemExit(0)
gaps = json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes = json.load(open("/tmp/bt2020/spikes.json"))
procs = []
for sym in ("BTC/USDT", "ETH/USDT"):
    k = sym.replace("/USDT", "") + "USDT_4h"; out = f"{H}/results/trades_1.0.6_{k}.json"
    cmd = [sys.executable, __file__, out, "--plugin", H + "/variants.py", "--candles", "/tmp/bt2020/candles.db", "--symbol", sym, "--timeframe", "4h",
           "--source", "binance_archive:resampled:5m", "--start", "2020-01-01", "--end", "2026-11-01", "--strategy", "Dewako Flow", "--version", "1.0.6",
           "--cash", "10000", "--lookback", "1210", "--fee-rate", "0.001", "--streak-reset", "daily", "--dry-run", "--json", "--max-gap-intervals", "3", "--screen"]
    for g in gaps: cmd += ["--allow-gap", g]
    for s in spikes[k]: cmd += ["--ack-spike", s]
    procs.append((k, out, subprocess.Popen(cmd, cwd=B, env={**os.environ, "PYTHONPATH": B, "DEWAKO_TRADING_DB": f"/tmp/bt2020/flowtune/examine_{k}.db"})))
for k, out, p in procs:
    assert p.wait() == 0, k
    t = json.load(open(out)); pn = sorted((x["pnl"] for x in t), reverse=True); net = sum(pn)
    yrs = {}
    for x in t: yrs[x["exit"][:4]] = yrs.get(x["exit"][:4], 0) + x["pnl"]
    print(k, "trades", len(t), "net", round(net), "wins", sum(p > 0 for p in pn), "top3", [round(p) for p in pn[:3]],
          "net without top1/3/5", round(net - pn[0]), round(net - sum(pn[:3])), round(net - sum(pn[:5])),
          "by exit year", {y: round(v) for y, v in sorted(yrs.items())}, flush=True)
