import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B="/home/claude/dewako-ai-trading-engine2/backend"; H=os.path.dirname(os.path.abspath(__file__)); PL=H+"/dewako_flow_plugin.py"
gaps=json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes=json.load(open("/tmp/bt2020/spikes.json"))
os.makedirs("/tmp/bt2020/flow",exist_ok=True); os.makedirs(H+"/results",exist_ok=True)
def one(sym):
    k=sym.replace("/USDT","")+"USDT_4h"; out=f"{H}/results/full_{k}.json"
    if os.path.exists(out) and os.path.getsize(out)>0: return
    cmd=[sys.executable,"-m","dewako.backtesting.gui","_cli","--plugin",PL,"--candles","/tmp/bt2020/candles.db","--symbol",sym,"--timeframe","4h",
         "--source","binance_archive:resampled:5m","--start","2020-01-01","--end","2026-11-01","--strategy","Dewako Flow","--version","1.0",
         "--cash","10000","--lookback","1210","--fee-rate","0.001","--streak-reset","daily","--dry-run","--json","--max-gap-intervals","3",
         "--variants-tried","104"]
    for g in gaps: cmd+=["--allow-gap",g]
    for s in spikes[k]: cmd+=["--ack-spike",s]
    env={**os.environ,"PYTHONPATH":B,"DEWAKO_TRADING_DB":f"/tmp/bt2020/flow/{k}.db"}
    pr=subprocess.run(cmd,cwd=B,env=env,capture_output=True,text=True)
    open(out if pr.stdout else out+".err","w").write(pr.stdout or pr.stderr); print(sym,pr.returncode,flush=True)
with ThreadPoolExecutor(2) as ex: list(ex.map(one,["BTC/USDT","ETH/USDT"]))
print("all done")
