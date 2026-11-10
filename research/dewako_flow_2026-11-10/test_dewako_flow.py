"""Synthetic checks of Dewako Flow 1.0's rules (no market data)."""
import runpy
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/claude/dewako-ai-trading-engine2/backend")
from dewako.core.types import Candle, MarketState, Side
from dewako.strategies import base, library  # noqa: F401

if ("Dewako Flow", "1.0") not in base._REGISTRY:
    runpy.run_path(__file__.replace("test_dewako_flow.py", "dewako_flow_plugin.py"))
D = base.get("Dewako Flow", "1.0")
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
POS = {"side": "BUY", "quantity": 1, "entry_price": 110}


def bar(c, o=None, h=None, l=None, v=1.0):
    o = c if o is None else o
    return dict(o=o, h=max(o, c) + 0.5 if h is None else h, l=min(o, c) - 0.5 if l is None else l, c=c, v=v)


def state(bars):
    cs = [Candle(t=T0 + timedelta(hours=4 * i), **b) for i, b in enumerate(bars)]
    return MarketState(symbol="ETH/USDT", as_of=cs[-1].t + timedelta(hours=4), candles=cs)


def setup(last, n=1200, start=100.0, dip=0.2):
    """A slow uptrend, a gentle 6-bar dip under the 30-bar VWAP, then `last` as the signal bar."""
    up = [bar(start + 0.01 * i) for i in range(n - 7)]
    top = up[-1]["c"]
    return up + [bar(top - dip * (k + 1)) for k in range(6)] + [last], top


def good(top, **kw):
    return bar(top + 0.1, o=top - 0.4, v=3.0, **kw)


def test_buys_a_volume_backed_close_back_above_vwap_in_an_uptrend_with_a_2_atr_stop():
    bars, top = setup(None)
    bars[-1] = good(top)
    s = D().generate_signal(state(bars))
    assert s is not None and s.side is Side.BUY
    assert 1.5 < s.entry_price - s.stop_price < 3.0                # 2 x ATR, ATR is about 1.0-1.2 here
    assert D().generate_signal(state(bars[-1000:])) is None         # under 1,200 candles: no signal


def test_each_entry_condition_is_required():
    bars, top = setup(None)
    quiet = bars[:-1] + [good(top) | {"v": 1.0}]                    # volume not above its 20-bar average
    assert D().generate_signal(state(quiet)) is None
    below = bars[:-1] + [bar(top - 1.3, v=3.0)]                     # still under the VWAP
    assert D().generate_signal(state(below)) is None
    already = bars[:-1] + [good(top), bar(top + 0.2, v=3.0)]        # previous close was already above VWAP: not a fresh reclaim
    assert D().generate_signal(state(already[1:])) is None
    wick = bars[:-1] + [good(top, h=top + 1.2)]                      # upper wick 1.1 of a 2.1 range: rejection (and not a shock bar)
    assert D().generate_signal(state(wick)) is None


def test_no_entry_below_the_200_day_average():
    bars, top = setup(None)
    high_past = [bar(300.0) for _ in range(600)] + bars[600:-1] + [good(top)]   # same last 600 bars, but the 1,200-bar average is far above
    assert D().generate_signal(state(high_past)) is None


def test_no_entry_within_six_bars_of_a_shock_bar():
    bars, top = setup(None)
    bars[-1] = good(top)
    assert D().generate_signal(state(bars)) is not None
    shocked = list(bars)
    b = shocked[-5]
    shocked[-5] = b | {"h": b["c"] + 4.0, "l": b["c"] - 4.0}        # an 8-wide bar against an ATR near 1: a shock
    assert D().generate_signal(state(shocked)) is None
    old = list(bars)
    b = old[-9]
    old[-9] = b | {"h": b["c"] + 4.0, "l": b["c"] - 4.0}            # the same shock 9 bars back: outside the 6-bar cooldown
    assert D().generate_signal(state(old)) is not None


def test_exits_one_atr_below_vwap_or_on_a_lost_regime_and_otherwise_holds():
    up = [bar(100 + 0.01 * i) for i in range(1200)]
    assert D().manage_position(state(up), POS) is None
    drop = up + [bar(up[-1]["c"] - 3.0)]
    s = D().manage_position(state(drop), POS)
    assert s.side is Side.SELL and "VWAP" in s.reason[0]
    lost = [bar(200.0) for _ in range(1100)] + [bar(100 + 0.01 * i) for i in range(100)]   # above VWAP, far below the 1,200-bar average
    s = D().manage_position(state(lost), POS)
    assert s.side is Side.SELL and "uptrend lost" in s.reason[0]
