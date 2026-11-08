"""Standalone bar-by-bar re-implementation of the owner's Pine script (see PROTOCOL.md). Research only; not part of the engine.

usage: python harness.py <candles.db> <BTC/USDT|ETH/USDT> <5m|15m> <both|long> [fee slip]   -> prints JSON
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START_MS = 1577836800000          # 2020-01-01
END_MS = 1793491200000            # 2026-11-01
P = dict(ema_fast=50, ema_trend=200, atr_len=14, stop_atr=1.5, target_atr=2.2, buffer_atr=0.10, adx_len=14, adx_thresh=20,
         be_buffer=0.3, be_activate=1.0, pct_equity=0.10, cash=10000.0)


def load(db: str, symbol: str, tf: str) -> pd.DataFrame:
    q = ("SELECT open_ms, o, h, l, c, v FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    df = pd.read_sql_query(q, sqlite3.connect(db), params=(symbol, START_MS, END_MS))
    if tf == "15m":
        df["b"] = df.open_ms // 900_000 * 900_000
        df = df.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
        df = df.rename(columns={"b": "open_ms"})
    elif tf != "5m":
        raise SystemExit("tf must be 5m or 15m")
    return df.reset_index(drop=True)


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df.h, df.l, df.c
    up, down = h - h.shift(), l.shift() - l
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)   # bar 0: h-l, as Pine's math.max skips na
    n = P["adx_len"]
    avg_tr = tr.rolling(n).mean()
    plus_di = 100 * pd.Series(plus_dm).rolling(n).mean() / avg_tr
    minus_di = 100 * pd.Series(minus_dm).rolling(n).mean() / avg_tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    df["adx"] = dx.rolling(n).mean()
    df["ema_fast"] = c.ewm(span=P["ema_fast"], adjust=False).mean()
    df["ema_trend"] = c.ewm(span=P["ema_trend"], adjust=False).mean()
    df["atr"] = tr.ewm(alpha=1 / P["atr_len"], adjust=False).mean()          # ta.atr = RMA of true range
    ny = pd.to_datetime(df.open_ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
    df["hour"], df["day"] = ny.dt.hour, ny.dt.day
    return df


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
    trades: list[Trade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)


def _gt(a, b):        # Pine: any comparison with na is false
    return not (math.isnan(a) or math.isnan(b)) and a > b


def run(df: pd.DataFrame, allow_short: bool, fee: float, slip: float) -> Result:
    o, hi, lo, cl = df.o.to_numpy(), df.h.to_numpy(), df.l.to_numpy(), df.c.to_numpy()
    adx, ef, et, atr = df.adx.to_numpy(), df.ema_fast.to_numpy(), df.ema_trend.to_numpy(), df.atr.to_numpy()
    hour, day, ts = df.hour.to_numpy(), df.day.to_numpy(), df.open_ms.to_numpy()
    nan = float("nan")
    cash = P["cash"]
    res = Result()
    asia_h = asia_l = nan
    long_done = short_done = long_armed = short_armed = False
    pend = {}                       # side -> (stop price, qty)
    pos = 0                         # +1 / -1 / 0
    qty = ep = 0.0
    entry_ms = 0
    exit_orders = None              # (stop, limit) active on this bar, placed at the previous close
    prev_pos = 0

    def fill(side, price):          # slippage always against us
        return price * (1 + slip) if side > 0 else price * (1 - slip)

    for i in range(len(df)):
        # ---- broker: orders placed at the previous close may fill inside this bar ----
        if pos != 0 and exit_orders is not None:
            sl, tp = exit_orders
            px = reason = None
            if pos > 0:
                if o[i] <= sl:
                    px, reason = o[i], "stop"
                elif o[i] >= tp:
                    px, reason = o[i], "target"
                else:
                    high_first = (hi[i] - o[i]) < (o[i] - lo[i])
                    for leg in (("h", "l") if high_first else ("l", "h")):
                        if leg == "h" and hi[i] >= tp:
                            px, reason = tp, "target"
                            break
                        if leg == "l" and lo[i] <= sl:
                            px, reason = sl, "stop"
                            break
            else:
                if o[i] >= sl:
                    px, reason = o[i], "stop"
                elif o[i] <= tp:
                    px, reason = o[i], "target"
                else:
                    high_first = (hi[i] - o[i]) < (o[i] - lo[i])
                    for leg in (("h", "l") if high_first else ("l", "h")):
                        if leg == "h" and hi[i] >= sl:
                            px, reason = sl, "stop"
                            break
                        if leg == "l" and lo[i] <= tp:
                            px, reason = tp, "target"
                            break
            if px is not None:
                xp = fill(-pos, px)
                cash += pos * qty * (xp - ep) - fee * qty * xp            # entry fee was taken from cash at the fill
                pnl = pos * qty * (xp - ep) - fee * qty * (ep + xp)       # per-trade P&L carries both fees
                res.trades.append(Trade(pos, entry_ms, int(ts[i]), ep, xp, qty, pnl, reason))
                pos, qty, exit_orders = 0, 0.0, None
        elif pos == 0 and pend:
            hits = []
            for side, (stop, q) in pend.items():
                if side > 0 and (o[i] >= stop or hi[i] >= stop):
                    hits.append((0 if o[i] >= stop else (1 if (hi[i] - o[i]) < (o[i] - lo[i]) else 2), side, max(o[i], stop), q))
                if side < 0 and (o[i] <= stop or lo[i] <= stop):
                    hits.append((0 if o[i] <= stop else (1 if (hi[i] - o[i]) >= (o[i] - lo[i]) else 2), side, min(o[i], stop), q))
            if hits:                # both sides in one bar never happened in practice; the one reached first on the bar path wins
                _, side, px, q = min(hits)
                ep = fill(side, px)
                pos, qty, entry_ms = side, q, int(ts[i])
                cash -= fee * qty * ep
                pend = {}

        # ---- script: runs on the bar close ----
        if i > 0 and day[i] != day[i - 1]:
            asia_h = asia_l = nan
            long_done = short_done = long_armed = short_armed = False
        h = hour[i]
        in_asia = h >= 20 or h < 3
        asia_open = i > 0 and h >= 20 and hour[i - 1] < 20
        if asia_open:
            asia_h, asia_l = hi[i], lo[i]
        elif in_asia:
            asia_h = max(hi[i] if math.isnan(asia_h) else asia_h, hi[i])
            asia_l = min(lo[i] if math.isnan(asia_l) else asia_l, lo[i])
        trade_window = 3 <= h < 16
        trend_up = _gt(ef[i], et[i]) and _gt(cl[i], et[i])
        trend_down = _gt(et[i], ef[i]) and _gt(et[i], cl[i])
        buf = atr[i] * P["buffer_atr"]
        flat = pos == 0
        trending = _gt(adx[i], P["adx_thresh"])
        if trade_window and flat and trending:
            mark = cash
            if not long_done and not long_armed and not math.isnan(asia_h) and trend_up and _gt(cl[i], asia_h + buf):
                pend[1] = (asia_h + buf, P["pct_equity"] * mark / cl[i])
                long_armed = True
            if (allow_short and not short_done and not short_armed and not math.isnan(asia_l) and trend_down
                    and _gt(asia_l - buf, cl[i])):
                pend[-1] = (asia_l - buf, P["pct_equity"] * mark / cl[i])
                short_armed = True
        else:
            pend = {}
        if pos > 0 and prev_pos == 0:
            long_done, long_armed, short_armed = True, False, False
        if pos < 0 and prev_pos == 0:
            short_done, short_armed, long_armed = True, False, False
        if pos > 0:
            sl, tp = ep - atr[i] * P["stop_atr"], ep + atr[i] * P["target_atr"]
            if cl[i] > ep + atr[i] * P["be_activate"]:
                sl = max(ep + atr[i] * P["be_buffer"], sl)
            exit_orders = (sl, tp)
        elif pos < 0:
            sl, tp = ep + atr[i] * P["stop_atr"], ep - atr[i] * P["target_atr"]
            if cl[i] < ep - atr[i] * P["be_activate"]:
                sl = min(ep - atr[i] * P["be_buffer"], sl)
            exit_orders = (sl, tp)
        prev_pos = pos
        res.equity.append(cash + (pos * qty * (cl[i] - ep) if pos else 0.0))
    return res


def stats(res: Result) -> dict:
    eq = np.array(res.equity)
    peak = np.maximum.accumulate(eq)
    pnl = np.array([t.pnl for t in res.trades])
    return {"trades": len(pnl), "net_pnl": float(pnl.sum()) if len(pnl) else 0.0,
            "max_drawdown_pct": float(((peak - eq) / peak).max()),
            "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
            "longs": sum(t.side > 0 for t in res.trades), "shorts": sum(t.side < 0 for t in res.trades)}


def gate(df, allow_short, fee, slip) -> dict:
    base = run(df, allow_short, fee, slip)
    stress = run(df, allow_short, 2 * fee, 2 * slip)
    s, s2 = stats(base), stats(stress)
    tr = sorted(base.trades, key=lambda t: t.exit_ms)
    k = len(tr) - len(tr) // 4
    ins = float(np.mean([t.pnl for t in tr[:k]])) if k else 0.0
    oos = float(np.mean([t.pnl for t in tr[k:]])) if len(tr) - k else 0.0
    edges = np.linspace(START_MS, END_MS, 5)
    periods = [sum(t.pnl for t in tr if edges[j] <= t.exit_ms < edges[j + 1]) for j in range(4)]
    checks = [
        ("enough trades", s["trades"] >= 100, f"{s['trades']} closed trades (need 100)"),
        ("positive at 2x costs", s2["net_pnl"] > 0, f"net P&L {s2['net_pnl']:.2f} at 2x costs"),
        ("drawdown within limit", s["max_drawdown_pct"] <= 0.15, f"max drawdown {s['max_drawdown_pct']:.1%} (limit 15%)"),
        ("out-of-sample holds up", oos > 0 and oos >= 0.5 * ins,
         f"out-of-sample expectancy {oos:.2f} vs in-sample {ins:.2f} (need >= 50% of it, and positive)"),
        ("consistent across periods", sum(p > 0 for p in periods) >= 2,
         f"{sum(p > 0 for p in periods)} of 4 equal periods profitable (need 50%)"),
    ]
    return {**s, "net_pnl_2x": s2["net_pnl"], "oos_expectancy": oos, "is_expectancy": ins, "period_pnl": periods,
            "exit_reasons": {r: sum(t.reason == r for t in base.trades) for r in ("stop", "target")},
            "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
            "verdict": "PASSED" if all(ok for _, ok, _ in checks) else "FAILED"}


if __name__ == "__main__":
    db, sym, tf, mode = sys.argv[1:5]
    fee, slip = (float(sys.argv[5]), float(sys.argv[6])) if len(sys.argv) > 6 else (0.001, 0.0002)
    d = indicators(load(db, sym, tf))
    out = {"symbol": sym, "timeframe": tf, "mode": mode, "fee": fee, "slippage": slip, "bars": len(d),
           **gate(d, mode == "both", fee, slip)}
    print(json.dumps(out, indent=2))
