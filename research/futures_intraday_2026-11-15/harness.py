"""Intraday futures rules (BRK, MR, PB, SQZ) on 5m / 15m. See PROTOCOL.md. Research only; no engine code is used or changed."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

MIN = 60_000
START_MS, END_MS = 1577836800000, 1793491200000
OOS_MS = 1704067200000
TF_MS = {"5m": 5 * MIN, "15m": 15 * MIN}
FUND_8H = 0.0001


def load(db: str, symbol: str, tf: str):
    q = ("SELECT open_ms, o, h, l, c FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    a = np.array(sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(q, (symbol, START_MS, END_MS)).fetchall(), dtype=float)
    t, o, h, l, c = a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    if tf == "5m":
        return t, o, h, l, c
    step = TF_MS[tf]
    k = t // step * step
    first = np.r_[True, k[1:] != k[:-1]]
    idx = np.flatnonzero(first)
    return (k[idx], o[idx], np.maximum.reduceat(h, idx), np.minimum.reduceat(l, idx), c[np.r_[idx[1:] - 1, len(c) - 1]])


# ------------------------------------------------------------------------------------------------ indicators (value known at each close)
def rma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr(h, l, c, n):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return rma(tr, n)


def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up, dn = rma(np.where(d > 0, d, 0.0)[1:], n), rma(np.where(d < 0, -d, 0.0)[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + up / dn)
    r = np.where(dn == 0, 100.0, r)
    r[np.isnan(up)] = np.nan
    return np.r_[np.nan, r]


def ema(x, n):
    out = np.empty(len(x))
    a = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    out[:n - 1] = np.nan
    return out


def roll(x, n, fn):
    """fn over the n values ending at each index (nan until n values exist)."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = fn(w, axis=1)
    return out


def prev(x):
    return np.r_[np.nan, x[:-1]]


# ------------------------------------------------------------------------------------------------ the four rules
@dataclass
class Rule:
    long: np.ndarray            # bool: enter long at the next open
    short: np.ndarray
    atr: np.ndarray             # ATR(14) for stops
    stop_mult: float
    trail: bool = False
    target_r: float = 0.0       # 0 = no target
    exit_long: np.ndarray = None    # bool at close: leave a long at the next open
    exit_short: np.ndarray = None
    time_stop: int = 0          # bars; 0 = none


def make_rule(code: str, tf: str, o, h, l, c) -> Rule:
    a14 = atr(h, l, c, 14)
    f = lambda x: np.nan_to_num(x, nan=0.0).astype(bool)       # noqa: E731  (a comparison with nan is already False)
    with np.errstate(invalid="ignore"):
        if code == "BRK":
            n = 86_400_000 // TF_MS[tf]
            hh, ll = prev(roll(h, n, np.max)), prev(roll(l, n, np.min))
            return Rule(f(c > hh), f(c < ll), a14, 2.0, trail=True)
        sma = roll(c, 20, np.mean)
        sd = roll(c, 20, np.std)
        up, lo = sma + 2 * sd, sma - 2 * sd
        if code == "MR":
            r = rsi(c, 14)
            return Rule(f((c < lo) & (r < 30)), f((c > up) & (r > 70)), a14, 2.0, exit_long=f(c >= sma), exit_short=f(c <= sma),
                        time_stop=48)
        if code == "PB":
            e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
            return Rule(f((e50 > e200) & (c > e200) & (l <= e20) & (c > e20) & (c > o)),
                        f((e50 < e200) & (c < e200) & (h >= e20) & (c < e20) & (c < o)), a14, 1.5, target_r=2.0)
        if code == "SQZ":
            a20 = atr(h, l, c, 20)
            squeeze = f((up < sma + 1.5 * a20) & (lo > sma - 1.5 * a20))
            was = np.r_[False, squeeze[:-1]]
            return Rule(was & f(c > up), was & f(c < lo), a14, 2.0, exit_long=f(c < sma), exit_short=f(c > sma))
    raise ValueError(code)


# ------------------------------------------------------------------------------------------------ the simulation (one coin, one sub-account)
@dataclass
class Trade:
    side: int               # +1 long, -1 short
    entry_ms: int
    exit_ms: int
    pnl: float
    risked: float

    @property
    def r(self):
        return self.pnl / self.risked


def simulate(t, o, h, l, c, rule: Rule, tf: str, fee: float, slip: float, cash: float, risk: float = 0.01):
    """Returns (trades, [(day_ms, realized equity at the end of that day)])."""
    fund_bar = FUND_8H * TF_MS[tf] / (8 * 3_600_000)
    n = len(t)
    eq = cash
    trades, curve = [], []
    side = 0
    qty = entry = stop = target = best = stop_dist = fees = funding = 0.0
    entry_i = 0
    want = 0                     # entry decided at the previous close
    leave = False                # exit decided at the previous close
    L, S, A = rule.long, rule.short, rule.atr
    XL = rule.exit_long if rule.exit_long is not None else np.zeros(n, bool)
    XS = rule.exit_short if rule.exit_short is not None else np.zeros(n, bool)
    day = t[0] // 86_400_000

    def close_at(px, i):
        nonlocal eq, side
        fill = px * (1 - side * slip)
        f = qty * fill * fee
        pnl = side * qty * (fill - entry) - fees - f - funding
        eq += pnl
        trades.append(Trade(side, int(t[entry_i]), int(t[i]), pnl, qty * stop_dist))
        side = 0

    for i in range(n):
        d = t[i] // 86_400_000
        if d != day:
            curve.append((int(day * 86_400_000), eq))
            day = d
        oi, hi, li = o[i], h[i], l[i]
        if side and leave:
            close_at(oi, i)
        leave = False
        if side == 0 and want:
            a = A[i - 1]
            dist = rule.stop_mult * a
            if a == a and dist > 0 and eq > 0:
                side = want
                entry = oi * (1 + side * slip)
                qty = min(eq * risk / dist, eq / entry)
                stop_dist, stop = dist, entry - side * dist
                target = entry + side * rule.target_r * dist if rule.target_r else 0.0
                best, entry_i, funding = entry, i, 0.0
                fees = qty * entry * fee
        want = 0
        if side:
            if (oi <= stop) if side > 0 else (oi >= stop):
                if i != entry_i:
                    close_at(oi, i)
                else:
                    close_at(stop, i)       # entered at this open: the stop is below it by construction
            elif (li <= stop) if side > 0 else (hi >= stop):
                close_at(stop, i)
            elif target and ((hi >= target) if side > 0 else (li <= target)):
                close_at(oi if i != entry_i and ((oi >= target) if side > 0 else (oi <= target)) else target, i)
        if side:
            ci = c[i]
            if side > 0:
                funding += qty * ci * fund_bar
            if rule.trail:
                if side > 0:
                    best = max(best, ci)
                    stop = max(stop, best - rule.stop_mult * A[i])
                else:
                    best = min(best, ci)
                    stop = min(stop, best + rule.stop_mult * A[i])
            if (XL[i] if side > 0 else XS[i]) or (rule.time_stop and i - entry_i + 1 >= rule.time_stop):
                leave = True
        else:
            want = 1 if L[i] else -1 if S[i] else 0
    curve.append((int(day * 86_400_000), eq))
    return trades, curve


def max_drawdown(values) -> float:
    peak, dd = values[0], 0.0
    for e in values:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return dd
