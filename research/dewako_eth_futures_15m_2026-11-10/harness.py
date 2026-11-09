"""Dewako ETH rules, long + short, bar by bar, with the engine's fill timing and cost model (see PROTOCOL.md). Research only.

usage: python harness.py <candles.db> <ETH/USDT> <4h|15m> <R0|R1|R2> <both|long> <fee> [cost_mult] -> prints JSON
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
TF_MS = {"15m": 900_000, "4h": 14_400_000}
READINGS = {   # regime, entry, exit, atr_len, atr_mult
    "R0": dict(regime=1200, entry=20, exit=10, atr_len=14, atr_mult=2.0),       # Dewako ETH as registered (4h)
    "R1": dict(regime=1200, entry=20, exit=10, atr_len=14, atr_mult=2.0),       # same bar counts on 15m
    "R2": dict(regime=19200, entry=320, exit=160, atr_len=224, atr_mult=8.0),   # same time spans on 15m (4h x 16; ATR stop x sqrt(16))
}
CASH, RISK, CAP = 10000.0, 0.005, 0.10
FUNDING_MS = 28_800_000


def load(db: str, symbol: str, tf: str) -> pd.DataFrame:
    q = ("SELECT open_ms, o, h, l, c, v FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    df = pd.read_sql_query(q, sqlite3.connect(db), params=(symbol, START_MS, END_MS))
    step = TF_MS[tf]
    df["b"] = df.open_ms // step * step
    df = df.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
    return df.rename(columns={"b": "open_ms"}).reset_index(drop=True)


def indicators(df: pd.DataFrame, p: dict) -> dict:
    """Values known at the CLOSE of bar k (index k). Donchian windows exclude bar k itself, as the engine's donchian() does."""
    h, l, c = df.h, df.l, df.c
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return dict(sma=c.rolling(p["regime"]).mean().to_numpy(),
                ehi=h.shift(1).rolling(p["entry"]).max().to_numpy(), elo=l.shift(1).rolling(p["entry"]).min().to_numpy(),
                xhi=h.shift(1).rolling(p["exit"]).max().to_numpy(), xlo=l.shift(1).rolling(p["exit"]).min().to_numpy(),
                atr=tr.rolling(p["atr_len"]).mean().to_numpy())


def fill_cost(bar_h, bar_l, bar_c, bar_ms, mult):
    """The engine's SimConfig defaults: half spread max(0.01%, 5% of range) + slippage 0.01% + 5% of range, range scaled to a 5m bar."""
    r = max(0.0, (bar_h - bar_l) / bar_c) * math.sqrt(300_000 / bar_ms)
    return mult * (max(1e-4, 0.05 * r) + 1e-4 + 0.05 * r)


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
    funding_times: int


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)


def run(df: pd.DataFrame, tf: str, p: dict, allow_short: bool, fee: float, cost_mult: float = 1.0) -> Result:
    ind = indicators(df, p)
    o, h, l, c, t = (df[k].to_numpy() for k in ("o", "h", "l", "c", "open_ms"))
    step = TF_MS[tf]
    need = max(p["regime"], p["entry"] + 1, p["exit"] + 1, p["atr_len"] + 1)
    res, cash = Result(), CASH
    pos, qty, entry, stop, entry_ms, entry_fee = 0, 0.0, 0.0, 0.0, 0, 0.0

    def px_with_cost(side, price, j):                 # side +1 buy, -1 sell; spread + slippage always against us
        k = fill_cost(h[j], l[j], c[j], step, cost_mult)
        return price * (1 + k) if side > 0 else price * (1 - k)

    def close(j, price, ms, reason):
        nonlocal pos, cash
        px = px_with_cost(-pos, price, j)
        f = fee * qty * px
        gross = pos * qty * (px - entry)
        cash += gross - f
        n_f = len(range(entry_ms // FUNDING_MS + 1, ms // FUNDING_MS + 1))
        res.trades.append(Trade(pos, entry_ms, ms, entry, px, qty, gross - f - entry_fee, reason, n_f))
        pos = 0

    for j in range(len(df)):
        bar_open_ms = int(t[j])
        # 1. the open: a stop the bar gaps through fills at the open
        if pos > 0 and o[j] <= stop:
            close(j, o[j], bar_open_ms, "stop (gap)")
        elif pos < 0 and o[j] >= stop:
            close(j, o[j], bar_open_ms, "stop (gap)")
        # 2. the strategy tick, on bars 0..j-1 (bar k = j-1 is the last closed one); orders fill at this open
        k = j - 1
        if k + 1 >= need:
            price = c[k]
            if pos > 0 and (price < ind["xlo"][k] or price < ind["sma"][k]):
                close(j, o[j], bar_open_ms, "exit rule")
            elif pos < 0 and (price > ind["xhi"][k] or price > ind["sma"][k]):
                close(j, o[j], bar_open_ms, "exit rule")
            elif pos == 0:
                dist = p["atr_mult"] * ind["atr"][k]
                side = 0
                if price > ind["sma"][k] and price > ind["ehi"][k]:
                    side = 1
                elif allow_short and price < ind["sma"][k] and price < ind["elo"][k]:
                    side = -1
                if side and dist > 0 and (side < 0 or price - dist > 0):
                    q = min(RISK * cash / dist, CAP * cash / price)
                    px = px_with_cost(side, o[j], j)
                    pos, qty, entry, entry_ms = side, q, px, bar_open_ms
                    stop = price - dist if side > 0 else price + dist
                    entry_fee = fee * q * px
                    cash -= entry_fee
        # 3. the bar's path, worst case for the position: a long sees the low first, a short the high first
        if pos > 0 and l[j] <= stop:
            close(j, stop, bar_open_ms + step // 3, "stop")
        elif pos < 0 and h[j] >= stop:
            close(j, stop, bar_open_ms + step // 3, "stop")
        res.equity.append(cash + (pos * qty * (c[j] - entry) if pos else 0.0))
    if pos:
        close(len(df) - 1, c[-1], int(t[-1]) + step, "end of data")
        res.equity[-1] = cash
    return res


def stats(res: Result) -> dict:
    eq = np.array(res.equity)
    peak = np.maximum.accumulate(eq)
    pnl = np.array([x.pnl for x in res.trades])
    n = len(pnl)
    return {"trades": n, "net_pnl": float(pnl.sum()) if n else 0.0, "max_drawdown_pct": float(((peak - eq) / peak).max()),
            "win_rate": float((pnl > 0).mean()) if n else 0.0,
            "long_trades": sum(x.side > 0 for x in res.trades), "short_trades": sum(x.side < 0 for x in res.trades),
            "long_pnl": float(sum(x.pnl for x in res.trades if x.side > 0)), "short_pnl": float(sum(x.pnl for x in res.trades if x.side < 0)),
            "avg_hold_h": float(np.mean([(x.exit_ms - x.entry_ms) / 3.6e6 for x in res.trades])) if n else 0.0,
            "funding_times_per_trade": float(np.mean([x.funding_times for x in res.trades])) if n else 0.0,
            "funding_stress_cost_0.01pct": float(sum(1e-4 * x.qty * x.entry * x.funding_times for x in res.trades))}


def gate(df, tf, p, allow_short, fee) -> dict:
    base, stress, zero = (run(df, tf, p, allow_short, fee, 1.0), run(df, tf, p, allow_short, 2 * fee, 2.0),
                          run(df, tf, p, allow_short, 0.0, 0.0))
    s, s2, sz = stats(base), stats(stress), stats(zero)
    tr = sorted(base.trades, key=lambda x: x.exit_ms)
    kk = len(tr) - len(tr) // 4
    ins = float(np.mean([x.pnl for x in tr[:kk]])) if kk else 0.0
    oos = float(np.mean([x.pnl for x in tr[kk:]])) if len(tr) - kk else 0.0
    edges = np.linspace(START_MS, END_MS, 5)
    periods = [float(sum(x.pnl for x in tr if edges[i] <= x.exit_ms < edges[i + 1])) for i in range(4)]
    top3 = sorted((x.pnl for x in tr), reverse=True)[:3]
    checks = [
        ("enough trades", s["trades"] >= 100, f"{s['trades']} closed trades (need 100)"),
        ("positive at 2x costs", s2["net_pnl"] > 0, f"net P&L {s2['net_pnl']:.2f} at 2x costs"),
        ("drawdown within limit", s["max_drawdown_pct"] <= 0.15, f"max drawdown {s['max_drawdown_pct']:.1%} (limit 15%)"),
        ("out-of-sample holds up", oos > 0 and oos >= 0.5 * ins,
         f"out-of-sample expectancy {oos:.2f} vs in-sample {ins:.2f} (need >= 50% of it, and positive)"),
        ("consistent across periods", sum(x > 0 for x in periods) >= 2, f"{sum(x > 0 for x in periods)} of 4 equal periods profitable"),
    ]
    return {**s, "net_pnl_2x": s2["net_pnl"], "zero_cost_net_pnl_diag": sz["net_pnl"], "net_without_top3": s["net_pnl"] - sum(top3),
            "oos_expectancy": oos, "is_expectancy": ins, "period_pnl": periods,
            "exit_reasons": {r: sum(x.reason == r for x in base.trades) for r in ("stop", "stop (gap)", "exit rule", "end of data")},
            "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
            "verdict": "PASSED" if all(ok for _, ok, _ in checks) else "FAILED"}


if __name__ == "__main__":
    db, sym, tf, rd, mode, fee = sys.argv[1:7]
    d = load(db, sym, tf)
    out = {"symbol": sym, "timeframe": tf, "reading": rd, **READINGS[rd], "mode": mode, "fee": float(fee), "bars": len(d),
           **gate(d, tf, READINGS[rd], mode == "both", float(fee))}
    print(json.dumps(out, indent=2))
