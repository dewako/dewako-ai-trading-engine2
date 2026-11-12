import json, os, runpy, sys
from datetime import datetime, timezone
os.environ["DEWAKO_CANDLE_MAX_GAP_INTERVALS"] = "3"
os.environ["DEWAKO_TRADING_DB"] = "/tmp/bt2020/dewako/inspect.db"
sys.path.insert(0, "/home/claude/dewako-ai-trading-engine2/backend")
from dewako.market_data.store import CandleStore
from dewako.backtesting.datasets import build_dataset
from dewako.backtesting.replay import ReplayConfig
from dewako.backtesting.sim_adapter import SimConfig
from dewako.backtesting.validation import validate_strategy
from dewako.backtesting.metrics import metrics_of
from dewako.strategies import base, library  # noqa
runpy.run_path("/home/claude/dewako-ai-trading-engine2/research/dewako_strategy_2026-11-08/dewako_plugin.py")
sym = sys.argv[1]; key = sym.replace("/USDT", "") + "USDT_4h"
gaps = [datetime.fromisoformat(g).replace(tzinfo=timezone.utc) for g in json.load(open("/tmp/bt2020/gaps.json"))["4h"]]
sp = [datetime.fromisoformat(t) for t in json.load(open("/tmp/bt2020/spikes.json"))[key]]
s, e = datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
ds = build_dataset(CandleStore("/tmp/bt2020/candles.db"), sym, "4h", "binance_archive:resampled:5m", s, e,
                   allowed_gaps=[g for g in gaps if s <= g < e], acknowledged_spikes=[t for t in sp if s <= t < e])
cfg = ReplayConfig(strategy="Dewako", version="1.0", symbol=sym, timeframe="4h", initial_cash=10000, lookback=1210,
                   sim=SimConfig(fee_rate=0.001), streak_reset="daily")
v = validate_strategy(ds, cfg)
R = v.results
tr = R["costs_1x"].trades
out = {"passed": v.passed, "net_by_cost": {k: metrics_of(R[f"costs_{k}x"]).net_pnl for k in ("1", "2", "3")},
       "oos_start": R["out_of_sample"].trades[0].entry_time.isoformat() if R["out_of_sample"].trades else None,
       "oos_trades": len(R["out_of_sample"].trades), "oos_net": sum(t.pnl for t in R["out_of_sample"].trades),
       "in_trades": len(R["in_sample"].trades), "in_net": sum(t.pnl for t in R["in_sample"].trades),
       "mc": v.monte_carlo, "periods": [round(sum(t.pnl for t in R[f"period_{i}"].trades)) for i in range(1, 5)],
       "trades": [[t.entry_time.isoformat()[:10], t.exit_time.isoformat()[:10], round(t.pnl, 2), t.exit_reason] for t in tr],
       "equity": [[t.isoformat()[:10], round(x, 2)] for t, x in R["costs_1x"].equity[::6]]}
json.dump(out, open(f"/tmp/bt2020/dewako/inspect_{key}.json", "w"))
print("done", sym, flush=True)
