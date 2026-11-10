"""Pre-registered Dewako Flow grid (see PROTOCOL.md). Each variant is a candidate version 1.0.<n> of the plugin class."""
import importlib.util, json, os
from dewako.strategies import base, library  # noqa: F401
H = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("dewako_flow_plugin", os.path.join(H, "..", "dewako_flow_2026-11-10", "dewako_flow_plugin.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
GRID = [dict(vwap_bars=v, exit_atr=e, atr_stop=s) for v in (18, 30, 60) for e in (1.0, 2.0) for s in (2.0, 3.0)]
MAP = {}
for i, params in enumerate(GRID, 1):
    ver = f"1.0.{i}"
    sub = type(f"DewakoFlow_t{i}", (mod.DewakoFlow,), {"version": ver, "candidate": True,
                                                        "default_parameters": {**mod.DewakoFlow.default_parameters, **params}})
    sub.name = "Dewako Flow"
    base.register(sub)
    MAP[ver] = params
BASELINE = next(v for v, p in MAP.items() if p == dict(vwap_bars=30, exit_atr=1.0, atr_stop=2.0))
if __name__ == "__main__":
    print(json.dumps({"map": MAP, "baseline": BASELINE}))
