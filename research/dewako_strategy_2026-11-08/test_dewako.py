"""Synthetic checks of Dewako 1.0's rules (no market data)."""
import runpy, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "/home/claude/dewako-ai-trading-engine2/backend")
from dewako.core.types import Candle, MarketState, Side
from dewako.strategies import base, library  # noqa: F401
if ("Dewako", "1.0") not in base._REGISTRY:
    runpy.run_path(__file__.replace("test_dewako.py", "dewako_plugin.py"))
D = base.get("Dewako", "1.0")
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)

def state(closes):
    cs = [Candle(t=T0 + timedelta(hours=4 * i), o=c, h=c * 1.001, l=c * 0.999, c=c, v=1.0) for i, c in enumerate(closes)]
    return MarketState(symbol="BTC/USDT", as_of=cs[-1].t + timedelta(hours=4), candles=cs)

def test_buys_a_breakout_only_above_the_200_day_average():
    up = [100 + i * 0.01 for i in range(1200)] + [140]
    s = D().generate_signal(state(up))
    assert s is not None and s.side is Side.BUY and s.stop_price < 140
    down = [200.0] * 1180 + [100.0] * 20 + [105.0]               # breaks the 20-bar high (100.1), but below the 1,200-bar average (~197)
    assert D().generate_signal(state(down)) is None
    assert D().generate_signal(state(up[-1000:])) is None        # not enough history: no signal

def test_no_entry_without_a_breakout():
    flat = [100 + i * 0.01 for i in range(1200)] + [100 + 1199 * 0.01]
    assert D().generate_signal(state(flat)) is None

def test_exits_on_a_10_bar_low_or_a_lost_regime_and_holds_otherwise():
    up = [100 + i * 0.01 for i in range(1200)]
    pos = {"side": "BUY", "quantity": 1, "entry_price": 110}
    assert D().manage_position(state(up + [up[-1] + 0.01]), pos) is None
    s = D().manage_position(state(up + [up[-1] - 0.5]), pos)
    assert s.side is Side.SELL and "10-bar low" in s.reason[0]
    lost = [200.0] * 1190 + [100.0] * 10 + [101.0]               # above the 10-bar low (99.9), far below the 1,200-bar average
    s = D().manage_position(state(lost), pos)
    assert s.side is Side.SELL and "uptrend lost" in s.reason[0]
