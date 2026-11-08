"""Full promotion gate, 2020-01-01..2026-11-01, fee 0.001, variants-tried 67, for the pre-selected winners only."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
B="/home/claude/dewako-ai-trading-engine2/backend"; T="/tmp/bt2020/tune"
gaps=json.load(open("/tmp/bt2020/gaps.json"))["4h"]; spikes=json.load(open("/tmp/bt2020/spikes.json"))
def one(job):
    key,sym=job; name,ver=key.split("|"); k=sym.replace("/USDT","")+"USDT_4h"
    cmd=[sys.executable,"-m","dewako.backtesting.gui","_cli","--plugin",f"{T}/variants.py","--candles","/tmp/bt2020/candles.db",
         "--symbol",sym,"--timeframe","4h","--source","binance_archive:resampled:5m","--start","2020-01-01","--end","2026-11-01",
         "--strategy",name,"--version",ver,"--cash","10000","--lookback","300","--fee-rate","0.001","--streak-reset","daily",
         "--dry-run","--json","--max-gap-intervals","3","--variants-tried","67"]
    for g in gaps: cmd+=["--allow-gap",g]
    for s in spikes[k]: cmd+=["--ack-spike",s]
    env={**os.environ,"PYTHONPATH":B,"DEWAKO_TRADING_DB":f"{T}/db/full_{ver}_{k}.db"}
    pr=subprocess.run(cmd,cwd=B,env=env,capture_output=True,text=True)
    open(f"{T}/full_{ver}_{k}.json","w").write(pr.stdout or pr.stderr)
    print(key,sym,pr.returncode,flush=True)
with ThreadPoolExecutor(2) as ex: list(ex.map(one,[(k,s) for k in sys.argv[1:] for s in ("BTC/USDT","ETH/USDT")]))
