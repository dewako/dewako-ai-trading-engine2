"""Deterministic synthetic markets for strategy tests. No randomness."""
import math
from datetime import datetime, timedelta, timezone

from dewako.core.types import Candle, MarketState

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=15)


def candles_from(closes, spread=0.004, vols=None):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(t=T0 + STEP * i, o=o, h=max(o, c) * (1 + spread / 2), l=min(o, c) * (1 - spread / 2), c=c,
                          v=(vols[i] if vols else 10.0)))
        prev = c
    return out


def state(cs, volume_ratio=2.0, **kw):
    return MarketState(symbol="BTC/USDT", as_of=cs[-1].t + STEP, candles=cs, volume_ratio=volume_ratio, **kw)


def steady_uptrend(n=120):
    """Up 0.25% then down 0.1% alternately: a clean trend with real pullbacks."""
    p, out = 100.0, []
    for i in range(n):
        p *= 1.0025 if i % 2 else 0.999
        out.append(p)
    return candles_from(out)


def steady_downtrend(n=120):
    p, out = 100.0, []
    for i in range(n):
        p *= 0.9975 if i % 2 else 1.001
        out.append(p)
    return candles_from(out)


def range_then_drop(n_range=90, drop_bars=2, drop=0.005):
    """Flat range (tiny sine) followed by a sharp sell-off: oversold inside a range."""
    out = [100 + 0.3 * math.sin(i / 3) for i in range(n_range)]
    for _ in range(drop_bars):
        out.append(out[-1] * (1 - drop))
    return candles_from(out)


def range_then_spike(n_range=90, spike_bars=2, spike=0.005):
    out = [100 + 0.3 * math.sin(i / 3) for i in range(n_range)]
    for _ in range(spike_bars):
        out.append(out[-1] * (1 + spike))
    return candles_from(out)


def quiet_range_then_breakout(n=100, up=True):
    """Tight range, then one wide-range bar that closes beyond it."""
    out = [100 + 0.15 * math.sin(i / 2) for i in range(n)]
    last = 101.2 if up else 98.8
    out.append(last)
    cs = candles_from(out, spread=0.002)
    c = cs[-1]
    cs[-1] = c.model_copy(update={"h": max(c.h, last * 1.001), "l": min(c.l, last * 0.999)})
    return cs


def momentum_uptrend(n=120, up=0.002, dn=0.0014, acc=0.0001):
    """Grind with pullbacks that speeds up over the last 12 bars: MACD histogram rising, RSI in the mid 60s."""
    p, out = 100.0, []
    for i in range(n):
        a = max(0, i - (n - 12)) * acc
        p *= 1 + (up + a if i % 2 else -dn)
        out.append(p)
    return candles_from(out)


def overstretched_uptrend(n=120):
    """Every bar up: RSI pinned near 100, which Momentum AI treats as too stretched to chase."""
    return candles_from([100 * 1.003 ** i for i in range(n)])


def sideways(n=120):
    # Trendless: two fast waves around 100 with no drift (a pure zigzag makes ADX degenerate).
    return candles_from([100 + 0.5 * math.sin(i * 1.7) + 0.3 * math.sin(i * 4.59) for i in range(n)])
