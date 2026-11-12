import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B="/home/claude/dewako-ai-trading-engine2/backend"
gaps=json.load(open("/tmp/bt2020/gaps.json"))["1h"]; spikes=json.load(open("/tmp/bt2020/spikes.json"))
def one(job):
    name,ver,sym=job; k=sym.replace("/USDT","")+"USDT_1h"; out=f"/tmp/bt2020/h1/{name.replace(' ','_')}_{ver}_{k}.json"
    if os.path.exists(out) and os.path.getsize(out) > 0: return
    cmd=[sys.executable,"-m","dewako.backtesting.gui","_cli","--candles","/tmp/bt2020/candles.db","--symbol",sym,"--timeframe","1h",
         "--source","binance_archive:resampled:5m","--start","2020-01-01","--end","2026-11-01","--strategy",name,"--version",ver,
         "--cash","10000","--lookback","300","--fee-rate","0.001","--streak-reset","daily","--dry-run","--json","--max-gap-intervals","3",
         "--variants-tried","82"]
    for g in gaps: cmd+=["--allow-gap",g]
    for s in spikes[k]: cmd+=["--ack-spike",s]
    env={**os.environ,"PYTHONPATH":B,"DEWAKO_TRADING_DB":f"/tmp/bt2020/h1/{name.replace(' ','_')}_{ver}_{k}.db"}
    pr=subprocess.run(cmd,cwd=B,env=env,capture_output=True,text=True)
    open(out if pr.stdout else out+".err","w").write(pr.stdout or pr.stderr); print(job,pr.returncode,flush=True)
jobs=[(n,v,s) for n,v in (("Momentum AI","1.4"),("Momentum AI","1.5"),("Breakout AI","1.8")) for s in ("BTC/USDT","ETH/USDT")]
with ThreadPoolExecutor(2) as ex: list(ex.map(one,jobs))
print("all done")
