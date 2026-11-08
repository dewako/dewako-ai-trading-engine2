"""Pre-registered parameter grid (written before any tuning run). Each variant = an existing strategy version with other default parameters,
registered as a candidate version <base>.<n> so it can never become a default. Grid in GRID, mapping written to variants.json."""
import itertools, json
from dewako.strategies import base, library  # noqa: F401
from dewako.strategies.exits_v1 import TrendAIv23, AIStrategyv04
from dewako.strategies.mean_reversion import MeanReversion

GRID = {
    ("Trend AI", "2.3", TrendAIv23): [dict(ema_fast=f, ema_slow=s, adx_min=a, atr_stop=k)
                                      for (f, s) in [(10, 50), (20, 50), (20, 100), (50, 200)] for a in (15, 25) for k in (2.0, 3.0)],
    ("AI Strategy", "0.4", AIStrategyv04): [dict(min_volume_ratio=v, atr_stop=k) for v in (0.8, 1.0, 1.2, 1.5) for k in (1.5, 2.0, 3.0)],
    ("Mean Reversion", "1.2", MeanReversion): [dict(bb_dev=d, rsi_low=r, atr_stop=k, adx_max=x)
                                               for d in (2.0, 2.5) for r in (25, 30, 35) for k in (2.5, 3.5) for x in (25, 30)],
}
MAP = {}
for (name, ver, cls), combos in GRID.items():
    for i, params in enumerate(combos, 1):
        v = f"{ver}.{i}"
        sub = type(f"{cls.__name__}_t{i}", (cls,), {"version": v, "candidate": True,
                                                    "default_parameters": {**cls.default_parameters, **params}})
        sub.name = name
        base.register(sub)
        MAP[f"{name}|{v}"] = params
json.dump(MAP, open("/tmp/bt2020/tune/variants.json", "w"), indent=1)
