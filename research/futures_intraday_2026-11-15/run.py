"""Runs the pre-registered intraday test (PROTOCOL.md) and writes results/results.json. usage: python run.py <candles.db> <candles_sol.db>"""
import datetime as dt
import json
import sys
import time

import harness as H

COSTS = {"verdict": (0.001, 0.0005), "2x stress": (0.002, 0.001), "taker": (0.0005, 0.0001), "maker-like": (0.0002, 0.0), "no costs": (0.0, 0.0)}
CASH = 10_000.0


def cell(data, code, tf, fee, slip):
    """Three sub-accounts. Returns (trades with symbol, daily portfolio equity [(day, equity)])."""
    trades, curves = [], {}
    for sym, (t, o, h, l, c) in data[tf].items():
        tr, cv = H.simulate(t, o, h, l, c, RULES[(code, tf, sym)], tf, fee, slip, CASH / 3)
        trades += [(sym, x) for x in tr]
        curves[sym] = dict(cv)
    days = sorted({d for cv in curves.values() for d in cv})
    last = {s: CASH / 3 for s in curves}
    port = []
    for d in days:
        for s in curves:
            last[s] = curves[s].get(d, last[s])
        port.append((d, sum(last.values())))
    return trades, port


def summary(trades, port):
    n = len(trades)
    net = port[-1][1] - CASH
    return dict(trades=n, win_rate=round(sum(x.pnl > 0 for _, x in trades) / n, 3) if n else None, net=round(net, 2),
                max_dd_pct=round(100 * H.max_drawdown([e for _, e in port]), 2), avg_r=round(sum(x.r for _, x in trades) / n, 4) if n else None)


def main(db, db_sol):
    global RULES
    t0 = time.time()
    data = {tf: {"BTC/USDT": H.load(db, "BTC/USDT", tf), "ETH/USDT": H.load(db, "ETH/USDT", tf), "SOL/USDT": H.load(db_sol, "SOL/USDT", tf)}
            for tf in ("5m", "15m")}
    RULES = {(code, tf, s): H.make_rule(code, tf, *d[1:]) for tf, syms in data.items() for s, d in syms.items() for code in ("BRK", "MR", "PB", "SQZ")}
    out = {"bars": {tf: {s: len(d[0]) for s, d in syms.items()} for tf, syms in data.items()}, "cells": {}}
    avg = lambda x: sum(x) / len(x) if x else None        # noqa: E731
    for code in ("BRK", "MR", "PB", "SQZ"):
        for tf in ("5m", "15m"):
            res = {name: cell(data, code, tf, f, s) for name, (f, s) in COSTS.items()}
            tr, port = res["verdict"]
            for name, (trs, p) in res.items():            # the books must add up in every run
                assert abs(CASH + sum(x.pnl for _, x in trs) - p[-1][1]) < 1e-6, (code, tf, name)
            ins = [x.r for _, x in tr if x.entry_ms < H.OOS_MS]
            oos = [x.r for _, x in tr if x.entry_ms >= H.OOS_MS]
            d0, d1 = port[0][0], port[-1][0]
            eq_at = lambda ms: next(e for d, e in port if d >= ms)      # noqa: E731
            edges = [d0 + (d1 - d0) * i / 4 for i in range(5)]
            periods = [round(eq_at(edges[i + 1]) - eq_at(edges[i]), 2) for i in range(4)]
            sv, s2 = summary(tr, port), summary(*res["2x stress"])
            checks = {"1 trades >= 100": len(tr) >= 100, "2 positive at 2x costs": s2["net"] > 0, "3 max drawdown <= 15%": sv["max_dd_pct"] <= 15,
                      "4 out-of-sample avg R positive and >= 50% of in-sample": bool(oos) and bool(ins) and avg(oos) > 0 and avg(oos) >= 0.5 * avg(ins),
                      "5 at least 2 of 4 periods profitable": sum(p > 0 for p in periods) >= 2}
            year = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).year      # noqa: E731
            out["cells"][f"{code} {tf}"] = dict(
                verdict="PASS" if all(checks.values()) else "FAIL", checks={k: bool(v) for k, v in checks.items()}, by_cost={k: summary(*v) for k, v in res.items()},
                in_sample_avg_r=round(avg(ins), 4) if ins else None, oos_avg_r=round(avg(oos), 4) if oos else None, periods=periods,
                by_side={n: dict(trades=len(x), net=round(sum(y.pnl for y in x), 2)) for n, sd in (("long", 1), ("short", -1)) for x in [[y for _, y in tr if y.side == sd]]},
                by_coin={s: dict(trades=len(x), net=round(sum(y.pnl for y in x), 2)) for s in data[tf] for x in [[y for q, y in tr if q == s]]},
                by_year={y: round(sum(x.pnl for _, x in tr if year(x.entry_ms) == y), 2) for y in range(2020, 2027)},
                move_per_trade_pct_before_costs=round(100 * avg([x.pnl / x.value for _, x in res["no costs"][0]]), 4),
                move_per_trade_pct_at_verdict_cost=round(100 * avg([x.pnl / x.value for _, x in tr]), 4),
                avg_hold_bars=round(avg([(x.exit_ms - x.entry_ms) / H.TF_MS[tf] for _, x in tr]), 1) if tr else None)
            c = out["cells"][f"{code} {tf}"]
            print(f"{code} {tf}: {c['verdict']}  " + "  ".join(f"{k}: n={v['trades']} net={v['net']} dd={v['max_dd_pct']}% R={v['avg_r']}" for k, v in c["by_cost"].items()), flush=True)
    out["seconds"] = round(time.time() - t0)
    json.dump(out, open("results/results.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
