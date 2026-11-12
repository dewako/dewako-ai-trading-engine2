"""Runs the 6 pre-registered charts (see PROTOCOL.md), two at a time. Skips a finished chart."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
H = os.path.dirname(os.path.abspath(__file__))
JOBS = [(s, tf) for tf in ("15m", "30m", "5m") for s in ("BTC/USDT", "ETH/USDT")]
def run(job):
    s, tf = job; out = f"{H}/results/{s.replace('/', '')}_{tf}.json"
    if os.path.exists(out) and os.path.getsize(out) > 0: return
    pr = subprocess.run([sys.executable, H + "/harness.py", "/tmp/bt2020/candles.db", s, tf], capture_output=True, text=True)
    if pr.returncode: print("FAILED", s, tf, pr.stderr[-500:], flush=True); return
    open(out, "w").write(pr.stdout); d = json.loads(pr.stdout)["gate_mode"]["taker_0.05+0.01"]
    print(s, tf, d["verdict"], d["trades"], round(d["net_pnl"]), flush=True)
with ThreadPoolExecutor(2) as ex: list(ex.map(run, JOBS))
print("all done", flush=True)
