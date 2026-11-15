"""Runs the pre-registered confirmation (PROTOCOL.md). usage: python run.py <candles_unseen.db> <candles.db> <candles_sol.db>"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "futures_trend_f2_2026-11-15"))
import harness as H  # noqa: E402

CASH = 10_000.0


def summary(r):
    tr = r["trades"]
    dd = H.max_drawdown(r["curve"])
    net = r["final"] - CASH
    return dict(trades=len(tr), wins=sum(t.pnl > 0 for t in tr), win_rate=round(sum(t.pnl > 0 for t in tr) / len(tr), 3) if tr else None,
                net=round(net, 2), max_dd_pct=round(100 * dd, 2), avg_r=round(sum(t.r for t in tr) / len(tr), 3) if tr else None,
                net_over_dd=round(net / CASH / dd, 2) if dd else None, liquidations=r["liquidations"], skipped=r["skipped"])


def checks(v, v2, data):
    tr = v["trades"]
    avg = lambda x: sum(x) / len(x) if x else None          # noqa: E731
    ins = [t.r for t in tr if t.entry_ms < H.OOS_MS]
    oos = [t.r for t in tr if t.entry_ms >= H.OOS_MS]
    t0, t1 = v["curve"][0][0], v["curve"][-1][0]
    edges = [t0 + (t1 - t0) * i / 4 for i in range(5)]
    eq_at = lambda ms: next(e for t, e in v["curve"] if t >= ms)     # noqa: E731
    periods = [round(eq_at(edges[i + 1]) - eq_at(edges[i]), 2) for i in range(4)]
    bh = H.buy_and_hold(data)
    bh_dd = H.max_drawdown(bh)
    sv, s2 = summary(v), summary(v2)
    c = {"1 trades >= 100": (len(tr) >= 100, len(tr)),
         "2 positive at 2x costs": (s2["net"] > 0, s2["net"]),
         "3 max drawdown <= 15%": (sv["max_dd_pct"] <= 15, sv["max_dd_pct"]),
         "4 out-of-sample avg R positive and >= 50% of in-sample": (bool(oos) and bool(ins) and avg(oos) > 0 and avg(oos) >= 0.5 * avg(ins),
                                                                     dict(in_sample_avg_r=round(avg(ins), 3) if ins else None, n_in=len(ins), oos_avg_r=round(avg(oos), 3) if oos else None, n_oos=len(oos))),
         "5 at least 2 of 4 periods profitable": (sum(p > 0 for p in periods) >= 2, periods),
         "6 max drawdown below buy-and-hold": (sv["max_dd_pct"] < 100 * bh_dd, dict(strategy=sv["max_dd_pct"], buy_and_hold=round(100 * bh_dd, 2)))}
    return {k: dict(passed=bool(p), value=val) for k, (p, val) in c.items()}, dict(net=round(bh[-1][1] - CASH, 2), max_dd_pct=round(100 * bh_dd, 2))


def report(data, label):
    v, v2 = H.run(data), H.run(data, fee=0.002, slip=0.001)
    tr = v["trades"]
    ch, bh = checks(v, v2, data)
    year = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).year      # noqa: E731
    ranked = sorted(tr, key=lambda t: -t.pnl)
    total = sum(t.pnl for t in tr)
    return {"VERDICT": "PASS" if all(x["passed"] for x in ch.values()) else "FAIL", "checks": ch, "verdict cost": summary(v), "2x stress": summary(v2),
            "buy and hold": bh,
            "by coin (each alone)": {s: summary(H.run({s: b})) for s, b in data.items()},
            "by side": {sd: dict(trades=len(x), net=round(sum(t.pnl for t in x), 2), sum_r=round(sum(t.r for t in x), 2)) for sd in ("LONG", "SHORT") for x in [[t for t in tr if t.side == sd]]},
            "by year": {y: dict(trades=len(x), net=round(sum(t.pnl for t in x), 2)) for y in range(2020, 2027) for x in [[t for t in tr if year(t.entry_ms) == y]]},
            "without best trades": {"all": round(total, 2), **{f"without best {k}": round(total - sum(t.pnl for t in ranked[:k]), 2) for k in (1, 3, 5)}},
            "best 5": [dict(symbol=t.symbol, side=t.side, entry=dt.datetime.fromtimestamp(t.entry_ms / 1000, dt.timezone.utc).date().isoformat(), pnl=round(t.pnl, 2), r=round(t.r, 2)) for t in ranked[:5]]}


def main(db_new, db, db_sol):
    new = {f"{s}/USDT": H.load_daily(db_new, f"{s}/USDT") for s in ("BNB", "XRP", "ADA", "AVAX", "LINK")}
    old = {"BTC/USDT": H.load_daily(db, "BTC/USDT"), "ETH/USDT": H.load_daily(db, "ETH/USDT"), "SOL/USDT": H.load_daily(db_sol, "SOL/USDT")}
    out = {"data": {s: dict(days=len(b), first=dt.datetime.fromtimestamp(b[0].t / 1000, dt.timezone.utc).date().isoformat()) for s, b in new.items()}}
    out["FIVE UNSEEN COINS (the verdict)"] = report(new, "unseen")
    out["all eight coins together (reported)"] = report({**old, **new}, "eight")
    out["neighbours on the five unseen coins (1x, verdict cost)"] = {f"{n} days, {k} ATR": summary(H.run(new, n=n, k=k)) for n in (40, 55, 70) for k in (2.5, 3.0, 3.5)}
    json.dump(out, open("results/results.json", "w"), indent=1, default=float)
    print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main(*sys.argv[1:4])
