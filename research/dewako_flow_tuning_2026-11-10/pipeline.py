"""Mechanical tuning pipeline (see PROTOCOL.md): training screens -> selection -> (only if the baseline is beaten) test screens + full gate."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B = "/home/claude/dewako-ai-trading-engine2/backend"; H = os.path.dirname(os.path.abspath(__file__)); W = "/tmp/bt2020/flowtune"
os.makedirs(W, exist_ok=True); os.makedirs(H + "/results", exist_ok=True)
gaps = json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes = json.load(open("/tmp/bt2020/spikes.json"))
info = json.loads(subprocess.run([sys.executable, H + "/variants.py"], env={**os.environ, "PYTHONPATH": B}, capture_output=True, text=True, check=True).stdout)
MAP, BASE = info["map"], info["baseline"]
COINS = ("BTC/USDT", "ETH/USDT")

def run(ver, sym, start, end, fee, screen, tag):
    k = sym.replace("/USDT", "") + "USDT_4h"; out = f"{H}/results/{tag}_{ver}_{k}.json"
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return json.load(open(out))
    cmd = [sys.executable, "-m", "dewako.backtesting.gui", "_cli", "--plugin", H + "/variants.py", "--candles", "/tmp/bt2020/candles.db",
           "--symbol", sym, "--timeframe", "4h", "--source", "binance_archive:resampled:5m", "--start", start, "--end", end,
           "--strategy", "Dewako Flow", "--version", ver, "--cash", "10000", "--lookback", "1210", "--fee-rate", fee,
           "--streak-reset", "daily", "--dry-run", "--json", "--max-gap-intervals", "3"] + (["--screen"] if screen else ["--variants-tried", "116"])
    for g in gaps: cmd += ["--allow-gap", g]
    for s in spikes[k]: cmd += ["--ack-spike", s]
    pr = subprocess.run(cmd, cwd=B, env={**os.environ, "PYTHONPATH": B, "DEWAKO_TRADING_DB": f"{W}/{tag}_{ver}_{k}.db"}, capture_output=True, text=True)
    try:
        d = json.loads(pr.stdout)
    except ValueError:
        open(out + ".err", "w").write(pr.stderr[-3000:]); raise SystemExit(f"run failed: {tag} {ver} {sym}: {pr.stderr[-300:]}")
    json.dump(d, open(out, "w"), indent=1); print(tag, ver, sym, d.get("trades"), round(d.get("net_pnl", 0)), flush=True)
    return d

with ThreadPoolExecutor(2) as ex:
    train = dict(zip([(v, s) for v in MAP for s in COINS], ex.map(lambda j: run(j[0], j[1], "2020-01-01", "2024-01-01", "0.002", True, "train"),
                                                                 [(v, s) for v in MAP for s in COINS])))
rows = []
for v in MAP:
    b, e = train[(v, "BTC/USDT")], train[(v, "ETH/USDT")]
    rows.append({"version": v, "params": MAP[v], "btc_trades": b["trades"], "btc_net": b["net_pnl"], "eth_trades": e["trades"], "eth_net": e["net_pnl"],
                 "eligible": b["trades"] >= 40 and e["trades"] >= 40, "score": min(b["net_pnl"], e["net_pnl"])})
elig = [r for r in rows if r["eligible"]]
best = max(elig, key=lambda r: r["score"]) if elig else None
base = next(r for r in rows if r["version"] == BASE)
beats = bool(best) and best["version"] != BASE and best["score"] > base["score"] and best["score"] > 0
sel = {"baseline": BASE, "rows": rows, "winner": best and best["version"], "beats_baseline": beats}
json.dump(sel, open(H + "/results/selection.json", "w"), indent=1)
print("winner", sel["winner"], "beats baseline", beats, flush=True)
if beats:
    w = best["version"]
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda s: run(w, s, "2024-01-01", "2026-11-01", "0.001", True, "test"), COINS))
        list(ex.map(lambda s: run(w, s, "2020-01-01", "2026-11-01", "0.001", False, "full"), COINS))
print("all done", flush=True)
