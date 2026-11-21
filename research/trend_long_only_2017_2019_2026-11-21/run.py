"""Runs the pre-registered test in PROTOCOL.md once and writes results/results.json.
usage: python run.py <candles_pre2020.db> <candles_btc_eth_2020.db> <candles_unseen_2020.db>"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "futures_trend_f2_2026-11-15"))
import harness as H  # noqa: E402

DAY = H.DAY
PRE_START = 1502928000000       # 2017-08-17 (first Binance BTC/ETH day)
PRE_END = 1577836800000         # 2020-01-01 exclusive
REF_START, REF_END = H.START_MS, H.END_MS
COINS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT"]
DD_LIMIT = 0.15
COSTS = {"verdict (0.10% + 0.05%)": (0.001, 0.0005), "2x stress (0.20% + 0.10%)": (0.002, 0.001)}
LONG = ("LONG",)


def summary(r, cash=10_000.0):
    tr = r["trades"]
    dd = H.max_drawdown(r["curve"])
    net = r["final"] - cash
    return dict(trades=len(tr), wins=sum(t.pnl > 0 for t in tr), net=round(net, 2), max_dd_pct=round(100 * dd, 2),
                avg_r=round(sum(t.r for t in tr) / len(tr), 3) if tr else None, sum_r=round(sum(t.r for t in tr), 2),
                liquidations=r["liquidations"], skipped_dd=r["skipped_dd"], open_at_end=r["open_at_end"])


def partial_days(db, symbol, a, b):
    import sqlite3
    q = "SELECT open_ms/86400000, COUNT(*) FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' AND open_ms>=? AND open_ms<? GROUP BY 1"
    rows = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(q, (symbol, a, b)).fetchall()
    return dict(days=len(rows), under_90pct=sum(n < 259 for _, n in rows))


def main(db_pre, db_be, db_un):
    ref_db = {"BTC/USDT": db_be, "ETH/USDT": db_be, "BNB/USDT": db_un, "XRP/USDT": db_un, "ADA/USDT": db_un, "LINK/USDT": db_un}
    pre = {s: H.load_daily(db_pre, s, PRE_START, PRE_END) for s in COINS}
    ref = {s: H.load_daily(ref_db[s], s, REF_START, REF_END) for s in COINS}
    out = {"data": {"pre": {s: dict(days=len(b), first=b[0].t, last=b[-1].t, **partial_days(db_pre, s, PRE_START, PRE_END)) for s, b in pre.items()},
                    "reference": {s: dict(days=len(b), first=b[0].t, last=b[-1].t) for s, b in ref.items()}}}
    run = lambda d, f, s, **kw: H.run(d, fee=f, slip=s, sides=LONG, dd_limit=DD_LIMIT, **kw)      # noqa: E731
    base = {n: run(pre, f, s) for n, (f, s) in COSTS.items()}
    v, v2 = base["verdict (0.10% + 0.05%)"], base["2x stress (0.20% + 0.10%)"]
    ref_v = run(ref, 0.001, 0.0005)
    out["verdict run"] = summary(v)
    out["2x stress run"] = summary(v2)
    out["reference run (2020-2026, same rule)"] = summary(ref_v)
    tr = v["trades"]
    avg = lambda x: sum(x) / len(x) if x else None                      # noqa: E731
    pre_r, ref_r = avg([t.r for t in tr]), avg([t.r for t in ref_v["trades"]])
    t0, t1 = v["curve"][0][0], v["curve"][-1][0]
    edges = [t0 + (t1 - t0) * i / 4 for i in range(5)]
    eq_at = lambda ms: next(e for t, e in v["curve"] if t >= ms)       # noqa: E731
    slices = [round(eq_at(edges[i + 1]) - eq_at(edges[i]), 2) for i in range(4)]
    bh = H.buy_and_hold(pre)
    s_v, s_2 = summary(v), summary(v2)
    checks = {
        "1 at least 20 closed trades": (len(tr) >= 20, len(tr)),
        "2 positive at base and at 2x costs": (s_v["net"] > 0 and s_2["net"] > 0, dict(base=s_v["net"], x2=s_2["net"])),
        "3 max drawdown <= 15%": (s_v["max_dd_pct"] <= 15, s_v["max_dd_pct"]),
        "4 avg R positive and >= 50% of the reference avg R": (bool(tr) and ref_r is not None and pre_r > 0 and pre_r >= 0.5 * ref_r,
                                                              dict(pre_avg_r=round(pre_r, 3) if pre_r is not None else None, reference_avg_r=round(ref_r, 3) if ref_r is not None else None)),
        "5 at least 2 of 4 equal slices profitable": (sum(p > 0 for p in slices) >= 2, slices),
        "6 max drawdown below buy-and-hold": (s_v["max_dd_pct"] < 100 * H.max_drawdown(bh), dict(strategy=s_v["max_dd_pct"], buy_and_hold=round(100 * H.max_drawdown(bh), 2))),
    }
    out["verdict checks"] = {k: dict(passed=bool(p), value=val) for k, (p, val) in checks.items()}
    out["VERDICT"] = "PASS" if all(p for p, _ in checks.values()) else "FAIL"
    out["buy and hold (equal-weight start, no rebalancing)"] = dict(net=round(bh[-1][1] - 10_000, 2), max_dd_pct=round(100 * H.max_drawdown(bh), 2))
    # reported only, never part of the verdict
    out["by coin (each alone with the whole account)"] = {s: summary(run({s: b}, 0.001, 0.0005)) for s, b in pre.items()}
    out["by calendar year (entry year)"] = {y: dict(trades=len(x), net=round(sum(t.pnl for t in x), 2), sum_r=round(sum(t.r for t in x), 2))
                                            for y in sorted({dt.datetime.fromtimestamp(t.entry_ms / 1000, dt.timezone.utc).year for t in tr})
                                            for x in [[t for t in tr if dt.datetime.fromtimestamp(t.entry_ms / 1000, dt.timezone.utc).year == y]]}
    ranked = sorted(tr, key=lambda t: -t.pnl)
    total = sum(t.pnl for t in tr)
    out["without best trades (sum of trade P&L)"] = {"all": round(total, 2), **{f"without best {k}": round(total - sum(t.pnl for t in ranked[:k]), 2) for k in (1, 3)}}
    out["unscaled long-only (dd_limit none)"] = summary(H.run(pre, fee=0.001, slip=0.0005, sides=LONG))
    out["original long+short rule, unscaled"] = summary(H.run(pre, fee=0.001, slip=0.0005))
    out["neighbours (scaled long-only)"] = {f"{n} days, {k} ATR": summary(run(pre, 0.001, 0.0005, n=n, k=k)) for n in (40, 55, 70) for k in (2.5, 3.0, 3.5)}
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "results.json"), "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])
