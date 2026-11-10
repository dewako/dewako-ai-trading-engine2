"""ATR + VWAP scalper, long + short (see PROTOCOL.md). Research only.

usage: python harness.py <candles.db> <BTC/USDT|ETH/USDT> <V1|V2> <fee> <slip> -> JSON
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START_MS, END_MS = 1577836800000, 1793491200000
STEP = 300_000
CASH, PCT, ATR_LEN, MAX_BARS = 10000.0, 0.10, 14, 24
V = {"V1": dict(entry_k=2.0, stop_k=1.5, target_k=None), "V2": dict(entry_k=1.0, stop_k=1.0, target_k=2.0)}


def load(db, symbol):
    q = ("SELECT open_ms, o, h, l, c, v FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    return pd.read_sql_query(q, sqlite3.connect(db), params=(symbol, START_MS, END_MS))


def indicators(df):
    tp = (df.h + df.l + df.c) / 3
    day = df.open_ms // 86_400_000
    pv, vol = (tp * df.v).groupby(day).cumsum(), df.v.groupby(day).cumsum()
    vwap = (pv / vol.where(vol > 0)).fillna(tp)
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(), (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / ATR_LEN, adjust=False, min_periods=ATR_LEN).mean()
    return vwap.to_numpy(), atr.to_numpy()


@dataclass
class Trade:
    side: int
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    qty: float
    pnl: float
    reason: str


@dataclass
class Result:
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)


def run(df, variant, fee, slip, vwap=None, atr=None):
    p = V[variant]
    if vwap is None:
        vwap, atr = indicators(df)
    o, h, l, c, t = (df[k].to_numpy() for k in ("o", "h", "l", "c", "open_ms"))
    res, cash = Result(), CASH
    pos, qty, entry, stop, tgt, e_ms, e_fee, e_bar = 0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0
    pending = 0          # +1/-1 entry to place at this open; 2 = exit at this open

    def close(px_raw, ms, reason):
        nonlocal pos, cash
        px = px_raw * (1 - slip) if pos > 0 else px_raw * (1 + slip)
        f = fee * qty * px
        g = pos * qty * (px - entry)
        cash += g - f
        res.trades.append(Trade(pos, e_ms, ms, entry, px, qty, g - f - e_fee, reason))
        pos = 0

    n = len(df)
    for j in range(n):
        ms = int(t[j])
        if pending == 2 and pos:
            close(o[j], ms, pending_reason)
        elif pending in (1, -1) and not pos:
            a = atr[j - 1]
            px = o[j] * (1 + slip) if pending > 0 else o[j] * (1 - slip)
            qty = PCT * cash / o[j]
            pos, entry, e_ms, e_bar = pending, px, ms, j
            stop = o[j] - pending * p["stop_k"] * a
            tgt = o[j] + pending * p["target_k"] * a if p["target_k"] else None
            e_fee = fee * qty * px
            cash -= e_fee
        pending = 0
        if pos:                                        # intrabar: gap, then stop before target (worst case)
            if (pos > 0 and o[j] <= stop) or (pos < 0 and o[j] >= stop):
                close(o[j], ms, "stop")
            elif (pos > 0 and l[j] <= stop) or (pos < 0 and h[j] >= stop):
                close(stop, ms + STEP // 2, "stop")
            elif tgt is not None and ((pos > 0 and h[j] >= tgt) or (pos < 0 and l[j] <= tgt)):
                close(tgt, ms + STEP // 2, "target")
        a, vw = atr[j], vwap[j]
        if j + 1 < n and not np.isnan(a) and a > 0:
            if pos:
                if p["target_k"] is None and ((pos > 0 and c[j] >= vw) or (pos < 0 and c[j] <= vw)):
                    pending, pending_reason = 2, "back to VWAP"
                elif j - e_bar + 1 >= MAX_BARS:
                    pending, pending_reason = 2, "time"
            else:
                k = p["entry_k"]
                if variant == "V1":
                    pending = 1 if c[j] < vw - k * a else -1 if c[j] > vw + k * a else 0
                elif j > 0 and not np.isnan(atr[j - 1]):
                    up_now, up_prev = c[j] > vw + k * a, c[j - 1] > vwap[j - 1] + k * atr[j - 1]
                    dn_now, dn_prev = c[j] < vw - k * a, c[j - 1] < vwap[j - 1] - k * atr[j - 1]
                    pending = 1 if up_now and not up_prev else -1 if dn_now and not dn_prev else 0
        res.equity.append(cash + (pos * qty * (c[j] - entry) if pos else 0.0))
    if pos:
        close(c[-1], int(t[-1]) + STEP, "end of data")
        res.equity[-1] = cash
    return res


def stats(r):
    eq = np.array(r.equity)
    pk = np.maximum.accumulate(eq)
    pnl = np.array([x.pnl for x in r.trades])
    n = len(pnl)
    notional = float(np.mean([x.qty * x.entry for x in r.trades])) if n else 0.0
    return {"trades": n, "net_pnl": float(pnl.sum()) if n else 0.0, "max_drawdown_pct": float(((pk - eq) / pk).max()),
            "win_rate": float((pnl > 0).mean()) if n else 0.0, "long_pnl": float(sum(x.pnl for x in r.trades if x.side > 0)),
            "short_pnl": float(sum(x.pnl for x in r.trades if x.side < 0)), "avg_notional": notional,
            "per_trade_pct_of_notional": float(pnl.mean() / notional) if n and notional else 0.0,
            "avg_hold_min": float(np.mean([(x.exit_ms - x.entry_ms) / 60000 for x in r.trades])) if n else 0.0,
            "funding_times_per_trade": float(np.mean([len(range(x.entry_ms // 28_800_000 + 1, x.exit_ms // 28_800_000 + 1))
                                                     for x in r.trades])) if n else 0.0,
            "exit_reasons": {k: sum(x.reason == k for x in r.trades) for k in ("stop", "target", "back to VWAP", "time", "end of data")}}


def gate(df, variant, fee, slip):
    vw, at = indicators(df)
    base, st, z = (run(df, variant, fee, slip, vw, at), run(df, variant, 2 * fee, 2 * slip, vw, at), run(df, variant, 0, 0, vw, at))
    s, s2, sz = stats(base), stats(st), stats(z)
    tr = sorted(base.trades, key=lambda x: x.exit_ms)
    k = len(tr) - len(tr) // 4
    ins = float(np.mean([x.pnl for x in tr[:k]])) if k else 0.0
    oos = float(np.mean([x.pnl for x in tr[k:]])) if len(tr) - k else 0.0
    ed = np.linspace(START_MS, END_MS, 5)
    per = [float(sum(x.pnl for x in tr if ed[i] <= x.exit_ms < ed[i + 1])) for i in range(4)]
    checks = [("enough trades", s["trades"] >= 100), ("positive at 2x costs", s2["net_pnl"] > 0),
              ("drawdown within limit", s["max_drawdown_pct"] <= 0.15), ("out-of-sample holds up", oos > 0 and oos >= 0.5 * ins),
              ("consistent across periods", sum(x > 0 for x in per) >= 2)]
    return {**s, "net_pnl_2x": s2["net_pnl"], "zero_cost_net_pnl_diag": sz["net_pnl"],
            "zero_cost_per_trade_pct_diag": sz["per_trade_pct_of_notional"], "oos_expectancy": oos, "is_expectancy": ins,
            "period_pnl": per, "checks": [{"name": a, "ok": bool(b)} for a, b in checks],
            "verdict": "PASSED" if all(b for _, b in checks) else "FAILED"}


if __name__ == "__main__":
    db, sym, var, fee, slip = sys.argv[1:6]
    d = load(db, sym)
    print(json.dumps({"symbol": sym, "variant": var, "fee": float(fee), "slip": float(slip), "bars": len(d),
                      **gate(d, var, float(fee), float(slip))}, indent=2))
