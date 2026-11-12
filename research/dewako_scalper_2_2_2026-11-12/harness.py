"""Dewako Scalper 2.2 (owner's Pine strategy), re-implemented bar by bar. See PROTOCOL.md. Research only.

usage: python harness.py <candles.db> <BTC/USDT|ETH/USDT> <15m|30m|5m> -> JSON with every cost scenario and both fill modes
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START_MS, END_MS = 1577836800000, 1793491200000
SPLIT_MS = 1698796800000                   # 2023-11-01: the script's author used only data after this
CLAIM_MS = 1761955200000                   # 2025-11-01: start of the script's 12-month test
CASH, RISK, MAX_LEV, FUND = 10000.0, 0.01, 10.0, 0.0001
FEE_FILTER_PCT = 0.08                      # the script's feePct input (a rule, not the cost)
MIN = 60_000
TF_MS = {"5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "4h": 240 * MIN}

PRESETS = {
    "15m / 30m": dict(ema_f=9, ema_s=21, st_len=10, st_mult=4.0, max_fee_r=0.25, sess=(12, 21), swing=5, min_atr=1.0, max_atr=4.0,
                      tps=(2.0, 3.0, 4.0, 6.0), break_even=True, htf=False),
    "3m / 5m": dict(ema_f=34, ema_s=89, st_len=20, st_mult=5.0, max_fee_r=0.10, sess=(13, 17), swing=5, min_atr=2.5, max_atr=4.0,
                    tps=(2.0, 4.0, 6.0, 10.0), break_even=False, htf=True),
}
ARSI = dict(mn=8, mx=28, er=10, bull=50.0, bear=50.0, ob=80.0, os=20.0)
PB_TOUCH, RS_DIP = 0.5, 8.0


def load(db, symbol, tf):
    q = ("SELECT open_ms, o, h, l, c, v FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    df = pd.read_sql_query(q, sqlite3.connect(db), params=(symbol, START_MS, END_MS))
    return df if tf == "5m" else resample(df, TF_MS[tf])


def resample(df, step):
    k = df.open_ms // step * step
    g = df.groupby(k)
    return pd.DataFrame({"open_ms": g.open_ms.first().index.to_numpy(), "o": g.o.first().to_numpy(), "h": g.h.max().to_numpy(),
                         "l": g.l.min().to_numpy(), "c": g.c.last().to_numpy(), "v": g.v.sum().to_numpy()})


# ---------------------------------------------------------------- Pine built-ins
def ema(x, n):                              # ta.ema: seeded with the first value
    return pd.Series(x).ewm(alpha=2 / (n + 1), adjust=False).mean().to_numpy()


def true_range(h, l, c):
    pc = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def rma(x, n):                              # ta.rma: SMA seed, then Wilder smoothing
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def supertrend(h, l, c, factor, n):
    """ta.supertrend: returns (line, direction) with direction -1 = up, 1 = down (Pine's convention)."""
    a = atr(h, l, c, n)
    src = (h + l) / 2
    m = len(c)
    up, lo, line, d = np.full(m, np.nan), np.full(m, np.nan), np.full(m, np.nan), np.ones(m)
    for i in range(m):
        ub, lb = src[i] + factor * a[i], src[i] - factor * a[i]
        pl = 0.0 if i == 0 or np.isnan(lo[i - 1]) else lo[i - 1]
        pu = 0.0 if i == 0 or np.isnan(up[i - 1]) else up[i - 1]
        pc = c[i - 1] if i else np.nan
        lb = lb if (lb > pl or pc < pl) else pl
        ub = ub if (ub < pu or pc > pu) else pu
        lo[i], up[i] = lb, ub
        if i == 0 or np.isnan(a[i - 1]):
            d[i] = 1
        elif line[i - 1] == pu:
            d[i] = -1 if c[i] > ub else 1
        else:
            d[i] = 1 if c[i] < lb else -1
        line[i] = lb if d[i] == -1 else ub
    return line, d


def adaptive_rsi(c):
    p = ARSI
    m = len(c)
    out = np.empty(m)
    up_avg = dn_avg = 0.0
    absd = np.r_[np.nan, np.abs(np.diff(c))]
    for i in range(m):
        if i >= p["er"]:
            den = absd[i - p["er"] + 1:i + 1].sum()
            er = abs(c[i] - c[i - p["er"]]) / den if den > 0 else 0.0
        else:
            er = 0.0
        alpha = 1.0 / (p["mn"] + (1 - er) * (p["mx"] - p["mn"]))
        chg = c[i] - (c[i - 1] if i else c[i])
        up_avg += alpha * (max(chg, 0.0) - up_avg)
        dn_avg += alpha * (max(-chg, 0.0) - dn_avg)
        out[i] = 100.0 if dn_avg == 0 else 100.0 - 100.0 / (1.0 + up_avg / dn_avg)
    return out


def rolling_min(x, n):
    return pd.Series(x).rolling(n, min_periods=1).min().to_numpy()


def rolling_max(x, n):
    return pd.Series(x).rolling(n, min_periods=1).max().to_numpy()


def shift(x, k=1, fill=np.nan):
    x = np.asarray(x, dtype=float)
    return np.r_[np.full(k, fill), x[:-k]]


def htf_direction(df5, t):
    """Direction of the previous completed 4h SuperTrend(3, 10) for each bar time t (request.security ...[1], lookahead_on)."""
    h4 = resample(df5, TF_MS["4h"])
    _, d = supertrend(*(h4[k].to_numpy() for k in ("h", "l", "c")), 3.0, 10)
    prev = pd.Series(np.r_[np.nan, d[:-1]], index=h4.open_ms.to_numpy())
    return prev.reindex(t // TF_MS["4h"] * TF_MS["4h"]).to_numpy()


# ---------------------------------------------------------------- the script's signals
def signals(df, preset, htf_dir=None):
    """Per bar: final signal direction (+1/-1/0), its type, its stop distance, plus the SuperTrend line/direction for the trail."""
    p = PRESETS[preset]
    o, h, l, c, v, t = (df[k].to_numpy() for k in ("o", "h", "l", "c", "v", "open_ms"))
    emaF, emaS = ema(c, p["ema_f"]), ema(c, p["ema_s"])
    day = t // 86_400_000
    hlc3 = (h + l + c) / 3
    cpv = pd.Series(hlc3 * v).groupby(day).cumsum().to_numpy()
    cv = pd.Series(v).groupby(day).cumsum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cv > 0, cpv / cv, np.nan)
    a = atr(h, l, c, p["st_len"])
    st_line, st_raw = supertrend(h, l, c, p["st_mult"], p["st_len"])
    st_dir = np.where(st_raw < 0, 1, -1)
    ar = adaptive_rsi(c)
    with np.errstate(invalid="ignore"):
        l1b, l1s = (emaF > emaS) & (c > vwap), (emaF < emaS) & (c < vwap)
        l2b, l2s = st_dir == 1, st_dir == -1
        l3b, l3s = (ar > ARSI["bull"]) & (ar < ARSI["ob"]), (ar < ARSI["bear"]) & (ar > ARSI["os"])
        allb, alls = l1b & l2b & l3b, l1s & l2s & l3s
        prev_dir = shift(st_dir, fill=0)
        flip_up, flip_dn = (st_dir == 1) & (prev_dir == -1), (st_dir == -1) & (prev_dir == 1)
        al_l = allb & ~shift(allb, fill=0).astype(bool) & ~flip_up
        al_s = alls & ~shift(alls, fill=0).astype(bool) & ~flip_dn
        st_l, st_s = flip_up & l1b & l3b, flip_dn & l1s & l3s
        tu, td = l1b & l2b, l1s & l2s
        zl, zs = np.fmax(emaF, vwap), np.fmin(emaF, vwap)
        zl = np.where(np.isnan(vwap), np.nan, zl)
        zs = np.where(np.isnan(vwap), np.nan, zs)
        ph, pl_ = shift(h), shift(l)
        pb_l = tu & shift(tu, fill=0).astype(bool) & (rolling_min(l, 3) <= zl + a * PB_TOUCH) & (c > ph) & (c > emaF) & (ar > ARSI["bull"]) & (ar < ARSI["ob"])
        pb_s = td & shift(td, fill=0).astype(bool) & (rolling_max(h, 3) >= zs - a * PB_TOUCH) & (c < pl_) & (c < emaF) & (ar < ARSI["bear"]) & (ar > ARSI["os"])
        upl, dnl = ARSI["bull"] + max(RS_DIP, 5), ARSI["bear"] - max(RS_DIP, 5)
        par = shift(ar)
        rs_l = tu & (rolling_min(ar, 5) <= ARSI["bull"] + 3) & (ar > upl) & (par <= upl) & (c > emaF)
        rs_s = td & (rolling_max(ar, 5) >= ARSI["bear"] - 3) & (ar < dnl) & (par >= dnl) & (c < emaF)
    m = len(c)
    raw = np.zeros(m, dtype=int)
    typ = np.array([""] * m, dtype=object)
    for cond, d, name in ((pb_l, 1, "PB"), (pb_s, -1, "PB"), (rs_l, 1, "RS"), (rs_s, -1, "RS"), (st_l, 1, "ST"), (st_s, -1, "ST"),
                          (al_l, 1, "AL"), (al_s, -1, "AL")):
        sel = cond & (raw == 0)
        raw[sel], typ[sel] = d, name
    swl = rolling_min(l, p["swing"]) - a * 0.1
    swh = rolling_max(h, p["swing"]) + a * 0.1
    with np.errstate(invalid="ignore"):
        raw_stop = np.where(raw == 1, c - swl, np.where(raw == -1, swh - c, np.nan))
        bad = (raw != 0) & (np.isnan(raw_stop) | (raw_stop <= 0) | np.isnan(a) | (a <= 0))
        risk = np.where(bad, np.nan, np.fmin(np.fmax(raw_stop, a * p["min_atr"]), a * p["max_atr"]))
        fee_r = FEE_FILTER_PCT / 100.0 * c / risk
        hr = (t // 3_600_000) % 24
        s0, s1 = p["sess"]
        in_sess = (hr >= s0) & (hr < s1)
        htf_ok = np.ones(m, dtype=bool)
        if p["htf"]:
            htf_ok = ((raw == 1) & (htf_dir < 0)) | ((raw == -1) & (htf_dir > 0))
        ok = (raw != 0) & in_sess & ~bad & htf_ok & ~(np.isnan(fee_r) | (fee_r > p["max_fee_r"]))
    sig = np.where(ok, raw, 0)
    return dict(sig=sig, typ=np.where(ok, typ, ""), risk=risk, st_line=st_line, st_dir=st_dir, raw=raw)


# ---------------------------------------------------------------- the script's trade tracker
@dataclass
class Trade:
    side: int
    kind: str
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    qty: float
    risk: float
    pnl: float
    funding: float
    reason: str

    @property
    def r(self):
        return self.pnl / (self.qty * self.risk)


@dataclass
class Result:
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)


def simulate(df, s, preset, fee, slip, fill="next_open", fund=FUND):
    p = PRESETS[preset]
    o, h, l, c, t = (df[k].to_numpy() for k in ("o", "h", "l", "c", "open_ms"))
    sig, typ, risk, st_line, st_dir = s["sig"], s["typ"], s["risk"], s["st_line"], s["st_dir"]
    tp_r = p["tps"]
    res, cash = Result(), CASH
    pos, kind, qty, entry, stop, rsk, tps, hit, e_ms, e_fee, fnd = 0, "", 0.0, 0.0, 0.0, 0.0, (), 0, 0, 0.0, 0.0
    last_exit = -1
    pending = None                                       # (dir, kind, risk) to fill at this bar's open

    def open_trade(d, k, r_dist, px_raw, ms):
        nonlocal pos, kind, qty, entry, stop, rsk, tps, hit, e_ms, e_fee, fnd, cash
        px = px_raw * (1 + d * slip)
        q = min(cash * RISK / r_dist, cash * MAX_LEV / px)
        pos, kind, qty, entry, rsk, e_ms, hit, fnd = d, k, q, px, r_dist, ms, 0, 0.0
        stop = entry - d * r_dist
        tps = tuple(entry + d * r_dist * x for x in tp_r)
        e_fee = fee * q * px
        cash -= e_fee

    def close_trade(px_raw, ms, reason):
        nonlocal pos, cash
        px = px_raw * (1 - pos * slip)
        g = pos * qty * (px - entry)
        f = fee * qty * px
        cash += g - f + fnd
        res.trades.append(Trade(pos, kind, e_ms, ms, entry, px, qty, rsk, g - f - e_fee + fnd, fnd, reason))
        pos = 0

    n = len(c)
    for i in range(n):
        ms = int(t[i])
        if pending is not None:
            if not pos:
                open_trade(*pending, o[i], ms)
            pending = None                               # filled at the open: this whole bar is managed below
        if pos:
            if fund and ms % 28_800_000 == 0:                   # funding at 00/08/16 UTC, longs pay a positive rate
                fnd -= pos * fund * qty * entry
            if pos > 0:
                stop_hit = l[i] <= stop
                hits = sum(h[i] >= x for x in tps)
            else:
                stop_hit = h[i] >= stop
                hits = sum(l[i] <= x for x in tps)
            prev = hit
            hit = max(hit, hits)
            if not stop_hit and prev == 0 and hit >= 1 and p["break_even"]:
                stop = max(stop, entry) if pos > 0 else min(stop, entry)
            if stop_hit:
                px = min(o[i], stop) if pos > 0 else max(o[i], stop)
                gap = (o[i] <= stop) if pos > 0 else (o[i] >= stop)
                locked = (stop >= entry) if pos > 0 else (stop <= entry)
                close_trade(px, ms, "gap through stop" if gap else "trail / break-even" if locked else "stopped out")
                last_exit = i
            elif hit >= 4:
                close_trade(tps[3], ms, "all targets hit")
                last_exit = i
            elif hit >= 1 and st_dir[i] == pos and not np.isnan(st_line[i]):
                stop = max(stop, st_line[i]) if pos > 0 else min(stop, st_line[i])
        # new entry decided on this bar's close
        if not pos and sig[i] != 0 and (typ[i] == "ST" or i != last_exit) and risk[i] > 0:
            if fill == "close":
                open_trade(int(sig[i]), typ[i], float(risk[i]), c[i], ms + 1)
            elif i + 1 < n:
                pending = (int(sig[i]), typ[i], float(risk[i]))
        res.equity.append(cash + (pos * qty * (c[i] - entry) + fnd if pos else 0.0))
    if pos:
        close_trade(c[-1], int(t[-1]) + 1, "end of data")
        res.equity[-1] = cash
    return res


# ---------------------------------------------------------------- statistics and the gate
def stats(r):
    eq = np.array(r.equity) if r.equity else np.array([CASH])
    pk = np.maximum.accumulate(eq)
    pnl = np.array([x.pnl for x in r.trades])
    n = len(pnl)
    R = np.array([x.r for x in r.trades])
    return {"trades": n, "net_pnl": float(pnl.sum()) if n else 0.0, "max_drawdown_pct": float(((pk - eq) / pk).max()),
            "win_rate": float((pnl > 0).mean()) if n else 0.0, "sum_r": float(R.sum()) if n else 0.0,
            "long_pnl": float(sum(x.pnl for x in r.trades if x.side > 0)), "short_pnl": float(sum(x.pnl for x in r.trades if x.side < 0)),
            "funding": float(sum(x.funding for x in r.trades)),
            "by_type_r": {k: round(float(sum(x.r for x in r.trades if x.kind == k)), 2) for k in ("PB", "RS", "ST", "AL")},
            "exit_reasons": {k: sum(x.reason == k for x in r.trades) for k in
                             ("stopped out", "trail / break-even", "gap through stop", "all targets hit", "end of data")}}


def split(r, at=SPLIT_MS):
    a = [x for x in r.trades if x.exit_ms < at]
    b = [x for x in r.trades if x.exit_ms >= at]
    f = lambda xs: {"trades": len(xs), "net_pnl": float(sum(x.pnl for x in xs)), "sum_r": float(sum(x.r for x in xs)),
                    "avg_r": float(np.mean([x.r for x in xs])) if xs else 0.0}
    return {"before_2023_11": f(a), "from_2023_11": f(b)}


def gate(df, s, preset, fee, slip):
    base, st = simulate(df, s, preset, fee, slip), simulate(df, s, preset, 2 * fee, 2 * slip)
    b, b2 = stats(base), stats(st)
    tr = sorted(base.trades, key=lambda x: x.exit_ms)
    k = len(tr) - len(tr) // 4
    ins = float(np.mean([x.pnl for x in tr[:k]])) if k else 0.0
    oos = float(np.mean([x.pnl for x in tr[k:]])) if len(tr) - k else 0.0
    ed = np.linspace(START_MS, END_MS, 5)
    per = [float(sum(x.pnl for x in tr if ed[i] <= x.exit_ms < ed[i + 1])) for i in range(4)]
    checks = [("enough trades", b["trades"] >= 100), ("positive at 2x costs", b2["net_pnl"] > 0),
              ("drawdown within limit", b["max_drawdown_pct"] <= 0.15), ("out-of-sample holds up", oos > 0 and oos >= 0.5 * ins),
              ("consistent across periods", sum(x > 0 for x in per) >= 2)]
    years = {}
    for x in tr:
        y = pd.Timestamp(x.exit_ms, unit="ms").year
        years[y] = years.get(y, 0.0) + x.pnl
    return {**b, "net_pnl_2x": b2["net_pnl"], "oos_expectancy": oos, "is_expectancy": ins, "period_pnl": per,
            "by_year_pnl": {str(y): round(v, 2) for y, v in sorted(years.items())}, "split": split(base),
            "checks": [{"name": a, "ok": bool(c)} for a, c in checks], "verdict": "PASSED" if all(c for _, c in checks) else "FAILED"}


COSTS = {"script_0.04": (0.0004, 0.0), "taker_0.05+0.01": (0.0005, 0.0001), "owner_0.10+0.02": (0.0010, 0.0002)}


def main(db, sym, tf):
    preset = "3m / 5m" if tf == "5m" else "15m / 30m"
    df = load(db, sym, tf)
    htf = None
    if PRESETS[preset]["htf"]:
        df5 = load(db, sym, "5m")
        htf = htf_direction(df5, df.open_ms.to_numpy())
    s = signals(df, preset, htf)
    out = {"symbol": sym, "timeframe": tf, "preset": preset, "bars": len(df), "raw_signals": int((s["raw"] != 0).sum()),
           "signals_after_filters": int((s["sig"] != 0).sum()), "gate_mode": {}, "reference_close_fill": {}}
    for name, (fee, slip) in COSTS.items():
        out["gate_mode"][name] = gate(df, s, preset, fee, slip)
    ref = simulate(df, s, preset, *COSTS["script_0.04"], fill="close")
    last12 = [x for x in ref.trades if x.exit_ms >= CLAIM_MS]
    out["reference_close_fill"] = {**stats(ref), "split": split(ref), "last_12_months": {"trades": len(last12),
                                   "sum_r": float(sum(x.r for x in last12))}}
    return out


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:4]), indent=1))
