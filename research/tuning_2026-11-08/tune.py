"""Tuning on the TRAINING window only (2020-01-01 .. 2024-01-01), 4h, fee 0.2% per side (2x the real fee) as the objective's cost.
Every variant in variants.py on BTC and ETH. One screening replay each. Results appended to results.jsonl. 2024+ is never touched here."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

B = "/home/claude/dewako-ai-trading-engine2/backend"
T = "/tmp/bt2020/tune"
gaps = json.load(open("/tmp/bt2020/gaps.json"))["4h"]
spikes = json.load(open("/tmp/bt2020/spikes.json"))
variants = json.load(open(f"{T}/variants.json")) if os.path.exists(f"{T}/variants.json") else None
if variants is None:
    subprocess.run([sys.executable, "-c", f"import runpy,sys; sys.path.insert(0,'{B}'); runpy.run_path('{T}/variants.py')"], check=True)
    variants = json.load(open(f"{T}/variants.json"))
START, END = sys.argv[1] if len(sys.argv) > 1 else "2020-01-01", sys.argv[2] if len(sys.argv) > 2 else "2024-01-01"
FEE = sys.argv[3] if len(sys.argv) > 3 else "0.002"
only = sys.argv[4:]
done = set()
out = f"{T}/results_{START}_{END}_{FEE}.jsonl"
if os.path.exists(out):
    done = {(r["key"], r["symbol"]) for r in map(json.loads, open(out))}


def one(job):
    key, sym = job
    name, ver = key.split("|")
    k = sym.replace("/USDT", "") + "USDT_4h"
    tag = f"{name.replace(' ', '_')}_v{ver}_{k}_{START}_{FEE}"
    cmd = [sys.executable, "-m", "dewako.backtesting.gui", "_cli", "--plugin", f"{T}/variants.py",
           "--candles", "/tmp/bt2020/candles.db", "--symbol", sym, "--timeframe", "4h", "--source", "binance_archive:resampled:5m",
           "--start", START, "--end", END, "--strategy", name, "--version", ver, "--cash", "10000", "--lookback", "300",
           "--fee-rate", FEE, "--streak-reset", "daily", "--dry-run", "--json", "--screen", "--max-gap-intervals", "3"]
    for g in gaps:
        cmd += ["--allow-gap", g]
    for s in spikes[k]:
        cmd += ["--ack-spike", s]
    env = {**os.environ, "PYTHONPATH": B, "DEWAKO_TRADING_DB": f"{T}/db/{tag}.db"}
    pr = subprocess.run(cmd, cwd=B, env=env, capture_output=True, text=True)
    row = {"key": key, "symbol": sym, "params": variants[key], "code": pr.returncode, "start": START, "end": END, "fee": FEE}
    try:
        row.update(json.loads(pr.stdout))
    except ValueError:
        row["stderr"] = pr.stderr[-600:]
    with open(out, "a") as f:
        f.write(json.dumps(row) + "\n")


os.makedirs(f"{T}/db", exist_ok=True)
jobs = [(k, s) for k in variants for s in ("BTC/USDT", "ETH/USDT") if (k, s) not in done and (not only or k in only)]
print(len(jobs), "runs", flush=True)
with ThreadPoolExecutor(2) as ex:
    list(ex.map(one, jobs))
print("all done", flush=True)
