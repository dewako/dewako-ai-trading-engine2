import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B="/home/claude/dewako-ai-trading-engine2/backend"; PL="/home/claude/dewako-ai-trading-engine2/research/dewako_strategy_2026-11-08/dewako_plugin.py"
gaps=json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes=json.load(open("/tmp/bt2020/spikes.json"))
def one(sym):
    k=sym.replace("/USDT","")+"USDT_4h"
    cmd=[sys.executable,"-m","dewako.backtesting.gui","_cli","--plugin",PL,"--candles","/tmp/bt2020/candles.db","--symbol",sym,"--timeframe","4h",
         "--source","binance_archive:resampled:5m","--start","2020-01-01","--end","2026-11-01","--strategy","Dewako","--version","1.0",
         "--cash","10000","--lookback","1210","--fee-rate","0.001","--streak-reset","daily","--dry-run","--json","--max-gap-intervals","3",
         "--variants-tried","76"]
    for g in gaps: cmd+=["--allow-gap",g]
    for s in spikes[k]: cmd+=["--ack-spike",s]
    env={**os.environ,"PYTHONPATH":B,"DEWAKO_TRADING_DB":f"/tmp/bt2020/dewako/{k}.db"}
    pr=subprocess.run(cmd,cwd=B,env=env,capture_output=True,text=True)
    open(f"/tmp/bt2020/dewako/full_{k}.json","w").write(pr.stdout or pr.stderr); print(sym,pr.returncode,flush=True)
with ThreadPoolExecutor(2) as ex: list(ex.map(one,["BTC/USDT","ETH/USDT"]))
