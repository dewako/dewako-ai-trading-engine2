"""Small pure indicator helpers shared by rule-based strategies."""
from __future__ import annotations

from dewako.core.types import Candle


def ema(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: list[float], n: int = 14) -> float:
    """Wilder RSI of the last bar. Returns 50 when there is not enough data."""
    if len(closes) <= n:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[Candle], n: int = 14) -> float:
    window = candles[-(n + 1):]
    trs = []
    for i, c in enumerate(window):
        prev = window[i - 1].c if i else c.o
        trs.append(max(c.h - c.l, abs(c.h - prev), abs(c.l - prev)))
    return sum(trs[-n:]) / len(trs[-n:])


# ---- Phase 3 additions. All functions are pure, use only the candles passed in, and return values for the LAST bar.
import math
from typing import Optional


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / min(n, len(values))


def stdev(values: list[float], n: int) -> float:
    w = values[-n:]
    m = sum(w) / len(w)
    return math.sqrt(sum((x - m) ** 2 for x in w) / len(w))


def zscore(values: list[float], n: int) -> float:
    sd = stdev(values, n)
    return (values[-1] - sma(values, n)) / sd if sd else 0.0


def roc(closes: list[float], n: int) -> float:
    """Rate of change over n bars as a fraction (0.02 = +2%)."""
    return closes[-1] / closes[-1 - n] - 1 if len(closes) > n and closes[-1 - n] else 0.0


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def bollinger(closes: list[float], n: int = 20, k: float = 2.0):
    mid, sd = sma(closes, n), stdev(closes, n)
    return mid, mid + k * sd, mid - k * sd


def vwap(candles: list[Candle], n: int) -> float:
    """Volume-weighted typical price over the last n candles (a rolling VWAP)."""
    w = candles[-n:]
    vol = sum(c.v for c in w)
    return sum((c.h + c.l + c.c) / 3 * c.v for c in w) / vol if vol else w[-1].c


def donchian(candles: list[Candle], n: int):
    """Highest high and lowest low of the n candles BEFORE the last one (the last candle is excluded on purpose,
    so a breakout is measured against the previous range)."""
    w = candles[-n - 1:-1]
    return max(c.h for c in w), min(c.l for c in w)


def adx(candles: list[Candle], n: int = 14) -> Optional[tuple[float, float, float]]:
    """Wilder ADX. Returns (adx, +DI, -DI) for the last bar, or None with fewer than 2n+1 candles."""
    if len(candles) < 2 * n + 1:
        return None
    trs, pdm, mdm = [], [], []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        up, down = c.h - p.h, p.l - c.l
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c)))
    tr_s, p_s, m_s = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])

    def di():
        return (100 * p_s / tr_s, 100 * m_s / tr_s) if tr_s else (0.0, 0.0)

    def dx(pdi, mdi):
        return 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0

    pdi, mdi = di()
    dxs = [dx(pdi, mdi)]
    for i in range(n, len(trs)):
        tr_s += trs[i] - tr_s / n
        p_s += pdm[i] - p_s / n
        m_s += mdm[i] - m_s / n
        pdi, mdi = di()
        dxs.append(dx(pdi, mdi))
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a, pdi, mdi
