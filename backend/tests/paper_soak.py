"""Bounded paper-trading soak: the REAL Application (supervised workers, real database, PAPER adapter) runs for N cycles against a
deterministic, seeded price path, with periodic restarts on the same database. Not collected by pytest (no test_ prefix).

  cd backend/tests && PYTHONPATH=.. python paper_soak.py --cycles 2000 --restart-every 250 --report soak.json [--junit soak.xml]
  (DEWAKO_TEST_DB=postgres + DEWAKO_TEST_PG_URL runs it on PostgreSQL, like the test suite.)

Paper money only. Market data is injected here, exactly as the application tests do; production code has no fake-price path.
Each cycle is one newly closed 15m bar for the strategy (so every cycle can be a new signal). The rest of the system runs on the wall
clock, so the soak sets the paper account's risk limits it needs through the operator limits store (cooldown 0, wide loss limits) and
records them in the report: this measures stability under churn; the risk rules themselves are tested by the suite with their defaults.
Each cycle: set a paper price (normal / through the stop / through the target), run every supervised worker once except the two that
need the real network (market_data, price_refresh), then check invariants. Exit 1 if any invariant fails:
  * no worker iteration fails;                    * no CRITICAL incident is opened;
  * never more than one OPEN position per bot;    * every OPEN position has a stop-loss;
  * client order ids are unique;                  * after a restart the open positions are exactly what was there before;
  * peak memory growth after the warm-up stays under --max-rss-growth-mb;
  * at least --min-round-trips positions were opened and closed (a soak that never trades proves nothing).
GitHub-hosted runners are short-lived: long soaks belong on a dedicated machine; CI runs a bounded one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scenarios as S  # noqa: E402
from hardening_helpers import tmp_db  # noqa: E402

from dewako.core.types import MarketState  # noqa: E402
from dewako.runtime.application import Application, Config  # noqa: E402

GUARD_ENV = {"DEWAKO_MAX_SPREAD_PCT": "0.5", "DEWAKO_MAX_PRICE_DEVIATION_PCT": "1.0", "DEWAKO_MAX_QUOTE_AGE_SECONDS": "120"}
SKIP_WORKERS = {"market_data", "price_refresh"}          # need the real network and wall-clock candles
FIRST = ("exit_monitor", "risk_equity")                  # protection first: entries are refused while exit protection is not verified
DATA = S.steady_uptrend()
NOW0 = DATA[-1].t + S.STEP
LAST = DATA[-1].c
CLOCK = {"bars": 0}                 # the market moves one closed bar per cycle, so each cycle is a new signal


def now():
    return NOW0 + S.STEP * CLOCK["bars"]


def candles():
    shift = S.STEP * CLOCK["bars"]
    return [type(c)(t=c.t + shift, o=c.o, h=c.h, l=c.l, c=c.c, v=c.v) for c in DATA]


def rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024.0 if sys.platform != "darwin" else r / (1024.0 * 1024.0)


def rows(app, sql):
    with app.rt.db.read() as c:
        return [dict(r) for r in c.execute(sql)]


async def start(target):
    async def prov(symbol, timeframe):
        return MarketState(symbol=symbol, as_of=now(), candles=candles())
    app = Application(Config(db_target=target, workers_enabled=True, paper_enabled=True, market_data="none", lease_ttl=60),
                      state_provider=prov, clock=now)
    status = await app.startup()
    if status != "READY":
        raise SystemExit(f"application did not start READY: {status} {[(s.name, s.detail) for s in app.steps if not s.ok]}")
    app.adapters["paper"].set_price("BTC/USDT", LAST, as_of=now())
    return app


def open_positions(app):
    return rows(app, "SELECT bot_id, symbol, quantity, stop_loss, take_profit, status FROM positions WHERE status='OPEN' ORDER BY id")


async def soak(a) -> dict:
    os.environ.update(GUARD_ENV)
    rng = random.Random(a.seed)
    _db, target = tmp_db()
    app = await start(target)
    soak_limits = {"cooldown_minutes": 0, "max_consecutive_losses": 1_000_000, "max_daily_loss_pct": 1000.0, "max_drawdown_pct": 1000.0}
    app.limits_store.set(soak_limits, actor={"username": "paper-soak", "role": "ADMIN"}, reason="paper soak: stability under churn")
    await app.bot_service.create(bot_id="soak1", strategy="Trend AI", version="2.2", symbol="BTC/USDT", timeframe="15m",
                                 environment="PAPER", exchange="paper")
    app.bot_service.set_enabled("soak1", True)
    problems, lat = [], {}
    worker_runs = worker_fail = restarts = 0
    rss0 = rss_peak_warm = None
    t0 = time.monotonic()
    try:
        for cyc in range(1, a.cycles + 1):
            CLOCK["bars"] = cyc
            pos = open_positions(app)
            r = rng.random()
            if pos and r < 0.15:
                price = pos[0]["stop_loss"] * 0.98                       # through the stop
            elif pos and pos[0]["take_profit"] and r < 0.30:
                price = pos[0]["take_profit"] * 1.01                     # through the target
            else:
                price = LAST * (1 + rng.uniform(-0.002, 0.002))          # normal noise around the last close
            app.adapters["paper"].set_price("BTC/USDT", price, as_of=now())
            order = sorted(app.supervisor.workers, key=lambda w: (w.name not in FIRST, FIRST.index(w.name) if w.name in FIRST else 0))
            for w in order:
                if w.name in SKIP_WORKERS:
                    continue
                s = time.perf_counter()
                ok = await app.supervisor.run_once(w)
                lat.setdefault(w.name, []).append((time.perf_counter() - s) * 1000)
                worker_runs += 1
                if not ok:
                    worker_fail += 1
                    problems.append(f"cycle {cyc}: worker {w.name} iteration failed")
            ops = open_positions(app)
            if len([p for p in ops if p["bot_id"] == "soak1"]) > 1:
                problems.append(f"cycle {cyc}: more than one OPEN position for one bot")
            if any(p["stop_loss"] is None for p in ops):
                problems.append(f"cycle {cyc}: an OPEN position has no stop-loss")
            if a.restart_every and cyc % a.restart_every == 0:
                before = open_positions(app)
                await app.shutdown()
                app = await start(target)
                restarts += 1
                after = open_positions(app)
                if after != before:
                    problems.append(f"cycle {cyc}: open positions changed across a restart: {before} -> {after}")
            if cyc == a.warmup:
                rss0 = rss_mb()
            if rss0 is not None:
                rss_peak_warm = rss_mb()
            if len(problems) > 50:
                break
        dup = rows(app, "SELECT client_order_id, COUNT(*) AS n FROM orders GROUP BY client_order_id HAVING COUNT(*) > 1")
        if dup:
            problems.append(f"duplicate client order ids: {dup[:5]}")
        crit = rows(app, "SELECT kind, detail FROM incidents WHERE severity='CRITICAL'")
        if crit:
            problems.append(f"{len(crit)} CRITICAL incident(s): {crit[:3]}")
        closed_n = rows(app, "SELECT COUNT(*) AS n FROM positions WHERE status='CLOSED'")[0]["n"]
        if closed_n < a.min_round_trips:
            problems.append(f"only {closed_n} round trips (need {a.min_round_trips}): a soak that does not trade proves nothing")
        growth = (rss_peak_warm - rss0) if rss0 is not None else 0.0
        if growth > a.max_rss_growth_mb:
            problems.append(f"peak memory grew {growth:.1f} MB after warm-up (limit {a.max_rss_growth_mb} MB)")
        orders = rows(app, "SELECT side, state, COUNT(*) AS n FROM orders GROUP BY side, state")
        closed = rows(app, "SELECT COUNT(*) AS n FROM positions WHERE status='CLOSED'")[0]["n"]
        incidents = rows(app, "SELECT severity, COUNT(*) AS n FROM incidents GROUP BY severity")
        db_bytes = os.path.getsize(target) if os.path.exists(str(target)) else None
    finally:
        await app.shutdown()
    q = lambda xs, p: round(statistics.quantiles(xs, n=100)[p - 1], 3) if len(xs) >= 100 else round(max(xs), 3)
    return {"backend": "postgres" if str(target).startswith("postgres") else "sqlite", "cycles_run": cyc, "seed": a.seed,
            "risk_limits_set_for_the_soak": soak_limits,
            "wall_seconds": round(time.monotonic() - t0, 1), "restarts": restarts, "worker_iterations": worker_runs,
            "worker_failures": worker_fail, "orders_by_side_state": orders, "positions_closed": closed, "incidents_by_severity": incidents,
            "rss_mb_after_warmup": round(rss0, 1) if rss0 else None, "rss_mb_peak": round(rss_mb(), 1),
            "rss_growth_after_warmup_mb": round(growth, 1), "db_bytes_end": db_bytes,
            "latency_ms": {k: {"p50": round(statistics.median(v), 3), "p95": q(v, 95), "max": round(max(v), 3), "n": len(v)}
                           for k, v in sorted(lat.items())},
            "problems": problems, "ok": not problems}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=500)
    ap.add_argument("--restart-every", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--max-rss-growth-mb", type=float, default=100.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--min-round-trips", type=int, default=5)
    ap.add_argument("--report")
    ap.add_argument("--junit")
    a = ap.parse_args()
    if a.cycles < 1 or a.warmup >= a.cycles:
        raise SystemExit("--cycles must be positive and larger than --warmup")
    res = asyncio.run(soak(a))
    out = json.dumps(res, indent=1, default=str)
    print(out)
    if a.report:
        open(a.report, "w").write(out)
    if a.junit:
        from xml.sax.saxutils import escape
        fail = "" if res["ok"] else f'<failure message="soak invariants failed">{escape(chr(10).join(res["problems"]))}</failure>'
        open(a.junit, "w").write(f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="paper_soak" tests="1" '
                                 f'failures="{0 if res["ok"] else 1}"><testcase classname="paper_soak.{res["backend"]}" '
                                 f'name="test_paper_soak_invariants_hold">{fail}</testcase></testsuite></testsuites>')
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
