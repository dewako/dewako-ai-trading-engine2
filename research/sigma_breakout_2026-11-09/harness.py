"""Bar-by-bar re-implementation of the owner's Pine "Sigma Breakout" (see PROTOCOL.md). Research only; not part of the engine.

usage: python harness.py <candles.db> <ETH/USDT> <A|B> <both|long> [fee slip [5m|15m]]   -> prints JSON
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

START_MS = 1577836800000          # 2020-01-01
END_MS = 1793491200000            # 2026-11-01
P = dict(n_days=14, band_mult=1.0, open_et=930, close_et=1600, flatten_et=1600)
SIZING = {"A": dict(cash=100000.0, fixed_qty=1.0, pct=None), "B": dict(cash=10000.0, fixed_qty=None, pct=0.10)}
STEP_MS = 900_000


def load(db: str, symbol: str, tf: str = "15m") -> pd.DataFrame:
    q = ("SELECT open_ms, o, h, l, c, v FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    df = pd.read_sql_query(q, sqlite3.connect(db), params=(symbol, START_MS, END_MS))
    if tf == "5m":
        return df.reset_index(drop=True)
    df["b"] = df.open_ms // STEP_MS * STEP_MS
    df = df.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    return df.rename(columns={"b": "open_ms"}).reset_index(drop=True)


def add_clock(df: pd.DataFrame, step_ms: int = STEP_MS) -> pd.DataFrame:
    o = pd.to_datetime(df.open_ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
    e = pd.to_datetime(df.open_ms + step_ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
    df["dkey"] = o.dt.year * 10000 + o.dt.month * 100 + o.dt.day
    df["hhmm"] = o.dt.hour * 100 + o.dt.minute
    df["end_mm"] = e.dt.hour * 100 + e.dt.minute
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


def run(df: pd.DataFrame, allow_short: bool, fee: float, slip: float, sizing: str = "A", p: dict | None = None) -> Result:
    p = {**P, **(p or {})}
    sz = SIZING[sizing]
    cash = sz["cash"]
    res = Result()
    pos = 0                      # +1 long, -1 short, 0 flat
    qty = entry = 0.0
    entry_ms = 0
    entry_fee = 0.0
    hist: dict[int, list[float]] = {}
    cur_day, have_open, day_open = None, False, float("nan")

    def fill(side, price):       # slippage always against us
        return price * (1 + slip) if side > 0 else price * (1 - slip)

    def close_pos(ms, price, reason):
        nonlocal pos, cash
        px = fill(-pos, price)
        f = fee * qty * px
        pnl = pos * qty * (px - entry) - entry_fee - f
        cash += pos * qty * (px - entry) - f            # entry fee was already taken from cash
        res.trades.append(Trade(pos, entry_ms, ms, entry, px, qty, pnl, reason))
        pos = 0

    def open_pos(ms, side, price):
        nonlocal pos, qty, entry, entry_ms, entry_fee, cash
        px = fill(side, price)
        q = sz["fixed_qty"] if sz["fixed_qty"] else sz["pct"] * cash / price
        if q <= 0:
            return
        pos, qty, entry, entry_ms = side, q, px, ms
        entry_fee = fee * q * px
        cash -= entry_fee

    for r in df.itertuples(index=False):
        ms, c = int(r.open_ms), float(r.c)
        if r.dkey != cur_day:
            if pos:
                close_pos(ms, c, "day rollover")
            cur_day, have_open = r.dkey, False
        if r.end_mm >= p["flatten_et"] and pos:
            close_pos(ms, c, "RTH close")
        if p["open_et"] <= r.hhmm < p["close_et"]:
            if not have_open:
                day_open, have_open = float(r.o), True
            h = hist.setdefault(int(r.hhmm), [])
            full = len(h) == p["n_days"]
            sigma = sum(h) / len(h) if h else 0.0
            upper, lower = day_open + p["band_mult"] * sigma, day_open - p["band_mult"] * sigma
            if full and sigma > 0 and r.end_mm < p["flatten_et"]:
                if c > upper and pos <= 0:
                    if pos < 0:
                        close_pos(ms, c, "flip")
                    open_pos(ms, 1, c)
                elif c < lower and pos >= 0:
                    if pos > 0:
                        close_pos(ms, c, "flip")
                    if allow_short:
                        open_pos(ms, -1, c)
            h.append(abs(c - day_open))
            while len(h) > p["n_days"]:
                h.pop(0)
        res.equity.append(cash + (pos * qty * (c - entry) if pos else 0.0))
    if pos:
        close_pos(int(df.open_ms.iloc[-1]), float(df.c.iloc[-1]), "end of data")
        res.equity[-1] = cash
    return res


def stats(res: Result) -> dict:
    eq = np.array(res.equity)
    peak = np.maximum.accumulate(eq)
    pnl = np.array([t.pnl for t in res.trades])
    return {"trades": len(pnl), "net_pnl": float(pnl.sum()) if len(pnl) else 0.0,
            "max_drawdown_pct": float(((peak - eq) / peak).max()),
            "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
            "longs": sum(t.side > 0 for t in res.trades), "shorts": sum(t.side < 0 for t in res.trades),
            "funding_times_crossed_per_trade": float(np.mean([len(range((t.entry_ms // 28_800_000) + 1, (t.exit_ms // 28_800_000) + 1))
                                                              for t in res.trades])) if len(pnl) else 0.0,
            "avg_hold_h": float(np.mean([(t.exit_ms - t.entry_ms) / 3.6e6 for t in res.trades])) if len(pnl) else 0.0}


def gate(df, allow_short, fee, slip, sizing) -> dict:
    base = run(df, allow_short, fee, slip, sizing)
    stress = run(df, allow_short, 2 * fee, 2 * slip, sizing)
    zero = run(df, allow_short, 0.0, 0.0, sizing)
    s, s2, sz = stats(base), stats(stress), stats(zero)
    tr = sorted(base.trades, key=lambda t: t.exit_ms)
    k = len(tr) - len(tr) // 4
    ins = float(np.mean([t.pnl for t in tr[:k]])) if k else 0.0
    oos = float(np.mean([t.pnl for t in tr[k:]])) if len(tr) - k else 0.0
    edges = np.linspace(START_MS, END_MS, 5)
    periods = [float(sum(t.pnl for t in tr if edges[j] <= t.exit_ms < edges[j + 1])) for j in range(4)]
    notional = float(np.mean([t.qty * t.entry for t in base.trades])) if base.trades else 0.0
    checks = [
        ("enough trades", s["trades"] >= 100, f"{s['trades']} closed trades (need 100)"),
        ("positive at 2x costs", s2["net_pnl"] > 0, f"net P&L {s2['net_pnl']:.2f} at 2x costs"),
        ("drawdown within limit", s["max_drawdown_pct"] <= 0.15, f"max drawdown {s['max_drawdown_pct']:.1%} (limit 15%)"),
        ("out-of-sample holds up", oos > 0 and oos >= 0.5 * ins,
         f"out-of-sample expectancy {oos:.2f} vs in-sample {ins:.2f} (need >= 50% of it, and positive)"),
        ("consistent across periods", sum(p > 0 for p in periods) >= 2,
         f"{sum(p > 0 for p in periods)} of 4 equal periods profitable (need 50%)"),
    ]
    return {**s, "net_pnl_2x": s2["net_pnl"], "zero_cost_net_pnl_diag": sz["net_pnl"],
            "zero_cost_per_trade_pct_of_notional_diag": (sz["net_pnl"] / max(sz["trades"], 1)) / notional if notional else 0.0,
            "avg_notional": notional, "oos_expectancy": oos, "is_expectancy": ins, "period_pnl": periods,
            "exit_reasons": {r: sum(t.reason == r for t in base.trades) for r in ("flip", "RTH close", "day rollover", "end of data")},
            "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
            "verdict": "PASSED" if all(ok for _, ok, _ in checks) else "FAILED"}


if __name__ == "__main__":
    db, sym, sizing, mode = sys.argv[1:5]
    fee, slip = (float(sys.argv[5]), float(sys.argv[6])) if len(sys.argv) > 6 else (0.001, 0.0002)
    tf = sys.argv[7] if len(sys.argv) > 7 else "15m"
    d = add_clock(load(db, sym, tf), 300_000 if tf == "5m" else STEP_MS)
    out = {"symbol": sym, "timeframe": tf, "sizing": sizing, "mode": mode, "fee": fee, "slippage": slip, "bars": len(d),
           **gate(d, mode == "both", fee, slip, sizing)}
    print(json.dumps(out, indent=2))
