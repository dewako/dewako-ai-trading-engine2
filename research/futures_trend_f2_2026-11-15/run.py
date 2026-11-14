"""Runs the pre-registered F2 test (PROTOCOL.md) and writes results/results.json. usage: python run.py <candles.db> <candles_sol.db>"""
import datetime as dt
import json
import sys

import harness as H

COSTS = {"verdict (0.10% + 0.05%)": (0.001, 0.0005), "2x stress (0.20% + 0.10%)": (0.002, 0.001), "Binance taker (0.05% + 0.01%)": (0.0005, 0.0001)}


def year(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).year


def summary(r, cash=10_000.0):
    tr = r["trades"]
    wins = [t for t in tr if t.pnl > 0]
    dd = H.max_drawdown(r["curve"])
    net = r["final"] - cash
    return dict(trades=len(tr), wins=len(wins), win_rate=round(len(wins) / len(tr), 3) if tr else None, net=round(net, 2),
                max_dd_pct=round(100 * dd, 2), sum_r=round(sum(t.r for t in tr), 2), avg_r=round(sum(t.r for t in tr) / len(tr), 3) if tr else None,
                net_over_dd=round(net / cash / dd, 2) if dd else None, liquidations=r["liquidations"], skipped=r["skipped"],
                open_at_end=r["open_at_end"])


def main(db, db_sol):
    data = {"BTC/USDT": H.load_daily(db, "BTC/USDT"), "ETH/USDT": H.load_daily(db, "ETH/USDT"), "SOL/USDT": H.load_daily(db_sol, "SOL/USDT")}
    out = {"data": {s: dict(days=len(b), first=b[0].t, last=b[-1].t) for s, b in data.items()}}
    base = {name: H.run(data, fee=f, slip=s) for name, (f, s) in COSTS.items()}
    out["1x by cost"] = {k: summary(v) for k, v in base.items()}
    v = base["verdict (0.10% + 0.05%)"]
    tr = v["trades"]
    # verdict checks
    ins = [t.r for t in tr if t.entry_ms < H.OOS_MS]
    oos = [t.r for t in tr if t.entry_ms >= H.OOS_MS]
    t0, t1 = v["curve"][0][0], v["curve"][-1][0]
    edges = [t0 + (t1 - t0) * i / 4 for i in range(5)]
    eq_at = lambda ms: next(e for t, e in v["curve"] if t >= ms)       # noqa: E731
    periods = [round(eq_at(edges[i + 1]) - eq_at(edges[i]), 2) for i in range(4)]
    bh = H.buy_and_hold(data)
    bh_dd = H.max_drawdown(bh)
    avg = lambda x: sum(x) / len(x) if x else None                      # noqa: E731
    s_v, s_2 = summary(v), summary(base["2x stress (0.20% + 0.10%)"])
    checks = {
        "1 trades >= 100": (len(tr) >= 100, len(tr)),
        "2 positive at 2x costs": (s_2["net"] > 0, s_2["net"]),
        "3 max drawdown <= 15%": (s_v["max_dd_pct"] <= 15, s_v["max_dd_pct"]),
        "4 out-of-sample avg R positive and >= 50% of in-sample": (bool(oos) and avg(oos) > 0 and avg(oos) >= 0.5 * avg(ins),
                                                                    dict(in_sample_avg_r=round(avg(ins), 3), n_in=len(ins), oos_avg_r=round(avg(oos), 3) if oos else None, n_oos=len(oos))),
        "5 at least 2 of 4 periods profitable": (sum(p > 0 for p in periods) >= 2, periods),
        "6 max drawdown below buy-and-hold": (s_v["max_dd_pct"] < 100 * bh_dd, dict(strategy=s_v["max_dd_pct"], buy_and_hold=round(100 * bh_dd, 2))),
    }
    out["verdict checks"] = {k: dict(passed=bool(p), value=val) for k, (p, val) in checks.items()}
    out["VERDICT"] = "PASS" if all(p for p, _ in checks.values()) else "FAIL"
    out["buy and hold"] = dict(net=round(bh[-1][1] - 10_000, 2), max_dd_pct=round(100 * bh_dd, 2), net_over_dd=round((bh[-1][1] - 10_000) / 10_000 / bh_dd, 2))
    # reported only
    out["by coin (verdict cost, each coin alone with the whole account)"] = {s: summary(H.run({s: b})) for s, b in data.items()}
    out["by side"] = {side: dict(trades=len(x), net=round(sum(t.pnl for t in x), 2), sum_r=round(sum(t.r for t in x), 2),
                                 wins=sum(t.pnl > 0 for t in x)) for side in ("LONG", "SHORT") for x in [[t for t in tr if t.side == side]]}
    out["by year (trades by entry year)"] = {y: dict(trades=len(x), net=round(sum(t.pnl for t in x), 2), sum_r=round(sum(t.r for t in x), 2))
                                             for y in sorted({year(t.entry_ms) for t in tr}) for x in [[t for t in tr if year(t.entry_ms) == y]]}
    ranked = sorted(tr, key=lambda t: -t.pnl)
    total = sum(t.pnl for t in tr)
    out["without best trades (sum of trade P&L)"] = {"all": round(total, 2), **{f"without best {k}": round(total - sum(t.pnl for t in ranked[:k]), 2) for k in (1, 3, 5)}}
    out["best 5 trades"] = [dict(symbol=t.symbol, side=t.side, entry=dt.datetime.fromtimestamp(t.entry_ms / 1000, dt.timezone.utc).date().isoformat(),
                                 pnl=round(t.pnl, 2), r=round(t.r, 2)) for t in ranked[:5]]
    out["neighbours (1x, verdict cost)"] = {f"{n} days, {k} ATR": summary(H.run(data, n=n, k=k)) for n in (40, 55, 70) for k in (2.5, 3.0, 3.5)}
    out["leverage (verdict cost)"] = {f"{L}x": summary(H.run(data, leverage=L)) for L in (1, 2, 3)}
    out["funding paid by both sides (1x, verdict cost)"] = summary(H.run(data, shorts_funding="pay"))
    out["long only / short only (1x, verdict cost)"] = {"long only": summary(H.run(data, sides=("LONG",))), "short only": summary(H.run(data, sides=("SHORT",)))}
    json.dump(out, open("results/results.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
