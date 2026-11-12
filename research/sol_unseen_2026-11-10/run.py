"""Runs the two pre-registered SOL gates (see PROTOCOL.md). Skips a finished run."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B = "/home/claude/dewako-ai-trading-engine2/backend"; H = os.path.dirname(os.path.abspath(__file__))
gaps = json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes = json.load(open("/tmp/bt2020/spikes_sol.json"))["SOLUSDT_4h"]
def run(job):
    tag, st, ver, plugin = job; out = f"{H}/results/{tag}.json"
    if os.path.exists(out) and os.path.getsize(out) > 0: return
    cmd = [sys.executable, "-m", "dewako.backtesting.gui", "_cli"] + plugin + ["--candles", "/tmp/bt2020/candles_sol.db", "--symbol", "SOL/USDT",
           "--timeframe", "4h", "--source", "binance_archive:resampled:5m", "--start", "2020-08-12", "--end", "2026-11-01", "--strategy", st, "--version", ver,
           "--cash", "10000", "--lookback", "1210", "--fee-rate", "0.001", "--streak-reset", "daily", "--dry-run", "--json", "--max-gap-intervals", "3",
           "--variants-tried", "2"]
    for g in gaps: cmd += ["--allow-gap", g]
    for s in spikes: cmd += ["--ack-spike", s]
    pr = subprocess.run(cmd, cwd=B, env={**os.environ, "PYTHONPATH": B, "DEWAKO_TRADING_DB": f"/tmp/bt2020/flowtune/sol_{tag}.db"}, capture_output=True, text=True)
    try: d = json.loads(pr.stdout)
    except ValueError:
        open(out + ".err", "w").write(pr.stdout[-2000:] + pr.stderr[-3000:]); print("FAILED TO RUN", tag, pr.stderr[-400:], flush=True); return
    json.dump(d, open(out, "w"), indent=1); print(tag, d.get("verdict"), d.get("trades"), round(d.get("net_pnl", 0)), flush=True)
JOBS = [("dewako_eth_1.0_SOLUSDT_4h", "Dewako ETH", "1.0", []),
        ("dewako_flow_1.0.6_SOLUSDT_4h", "Dewako Flow", "1.0.6", ["--plugin", H + "/../dewako_flow_tuning_2026-11-10/variants.py"])]
with ThreadPoolExecutor(2) as ex: list(ex.map(run, JOBS))
print("all done", flush=True)
