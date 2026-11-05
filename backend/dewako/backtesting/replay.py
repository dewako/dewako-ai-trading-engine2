"""Replay a validated Dataset through the REAL trading pipeline, on simulated time and a simulated exchange.

    Strategy -> Signal -> Risk Engine -> Execution Engine -> SimulatedExchange   (entries, BotRunner.tick)
    Exit Monitor -> Execution Engine -> SimulatedExchange                        (stop-loss, take-profit, time stop)

Nothing here re-implements trading logic: the same classes the paper runtime uses make every decision, against a throwaway database.
Timing rules (all of them are the pessimistic choice):
  * A strategy only sees candles that were CLOSED at the moment it decides. It decides at the instant a bar closes, and its order can
    only fill at the NEXT bar's open price, plus spread, slippage and fees. It never trades the close it has just seen.
  * Inside a bar the price path is open -> low -> high -> close (the worst order for a long: a stop is reached before a take-profit).
    The exit monitor checks every point on that path. A stop or take-profit level that the path crosses gets its own point, so the exit
    fills near the level (plus costs), not at the bar extreme. A gap (the next open is beyond a level) fills at the gapped quote.
  * Every stored timestamp follows the virtual clock (`virtual_time`), so time stops and stall detection use simulated time.
Equity is marked to market at the bid after every bar close and recorded in the risk state, as the paper runtime does.
Results are marked SIMULATED. Nothing here can reach an exchange: the only adapter is the simulated one.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from dewako.backtesting.clock import VirtualClock
from dewako.backtesting.datasets import Dataset
from dewako.backtesting.sim_adapter import SimConfig, SimFill, SimulatedExchange
from dewako.core import timesource
from dewako.core.types import Candle, MarketState
from dewako.execution.engine import ExecutionEngine
from dewako.execution.exit_monitor import ExitMonitor
from dewako.execution.validation import QuoteGuardConfig
from dewako.market_data.timeframes import seconds as timeframe_seconds
from dewako.persistence.db import Database
from dewako.risk.engine import RiskEngine, RiskLimits
from dewako.risk.state import RiskStateStore
from dewako.runtime.bot_runner import BotConfig, BotRunner
from dewako.safety.controller import SafetyController
from dewako.strategies import base, library  # noqa: F401  (library registers the built-in strategies)

SCOPE = "PAPER"
EXCHANGE = "sim"
_NUDGE = 1e-12


@dataclass(frozen=True)
class ReplayConfig:
    strategy: str
    version: str
    symbol: str
    timeframe: str
    initial_cash: float = 10_000.0
    lookback: int = 300                          # closed candles shown to the strategy
    sim: SimConfig = field(default_factory=SimConfig)
    limits: Optional[RiskLimits] = None          # None = the platform default limits
    max_hold_seconds: Optional[float] = None     # time stop (None = off), as in the paper runtime
    # The runtime refuses to trade without explicit quote-guard limits, so a replay states its own. Recorded with the run.
    quote_guard: QuoteGuardConfig = field(default_factory=lambda: QuoteGuardConfig(max_spread_pct=0.5, max_price_deviation_pct=2.0,
                                                                                    max_quote_age_seconds=30.0, clock_skew_tolerance_seconds=2.0))

    def __post_init__(self) -> None:
        if not (self.initial_cash > 0):
            raise ValueError("initial_cash must be positive")
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")

    def record(self) -> dict[str, Any]:
        return {"strategy": self.strategy, "version": self.version, "symbol": self.symbol, "timeframe": self.timeframe,
                "initial_cash": self.initial_cash, "lookback": self.lookback, "max_hold_seconds": self.max_hold_seconds,
                "sim": {k: (v if not isinstance(v, tuple) else [[a.isoformat(), b.isoformat()] for a, b in v])
                        for k, v in self.sim.__dict__.items()},
                "quote_guard": dict(self.quote_guard.__dict__),
                "limits": self.limits.model_dump() if self.limits is not None and hasattr(self.limits, "model_dump") else None}


@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    quantity: float
    entry_price: float
    exit_price: float
    fees: float
    pnl: float                  # after fees
    exit_reason: str


@dataclass
class ReplayResult:
    simulated: bool
    config: dict
    dataset: dict
    trades: list[Trade]
    fills: list[SimFill]
    equity: list[tuple[datetime, float]]          # (bar close time, mark-to-market equity)
    ticks: dict[str, int]                          # BotRunner outcomes -> count
    exits: dict[str, int]                          # exit reasons -> count
    notes: list[tuple[datetime, str, str]]         # every tick that was not 'no signal': (time, outcome, detail)
    final_cash: float
    open_position_at_end: bool
    incidents: int
    bars: int
    digest: str                                    # identical inputs must give an identical digest


def _bar_seconds(timeframe: str) -> int:
    return int(timeframe_seconds(timeframe))


class _Run:
    def __init__(self, ds: Dataset, cfg: ReplayConfig, db: Database, clock: VirtualClock):
        if ds.symbol != cfg.symbol or ds.timeframe != cfg.timeframe:
            raise ValueError("the dataset's symbol/timeframe do not match the replay config")
        self.ds, self.cfg, self.db = ds, cfg, db
        base.get(cfg.strategy, cfg.version)          # an unknown strategy must fail loudly, not "trade" zero times (the runner would swallow it)
        self.candles: list[Candle] = list(ds.candles)
        if len(self.candles) < 2:
            raise ValueError("a replay needs at least two candles")
        self.step = timedelta(seconds=_bar_seconds(cfg.timeframe))
        self.clock = clock
        self.sim = SimulatedExchange(self.clock, balance=cfg.initial_cash, config=cfg.sim)
        safety = SafetyController(db)
        self.risk_state = RiskStateStore(db)
        self.risk_state.init_scope(SCOPE, cfg.initial_cash)
        limits = cfg.limits or RiskLimits()
        self.engine = ExecutionEngine(db, safety, {EXCHANGE: self.sim}, quote_guard=cfg.quote_guard, clock=self.clock,
                                      limits=limits, risk_state=self.risk_state)
        self.monitor = ExitMonitor(self.engine, RiskEngine(), clock=self.clock, max_hold_seconds=cfg.max_hold_seconds)
        self._i = 0                                  # index of the first candle NOT yet closed for the strategy
        self.runner = BotRunner(self.engine, RiskEngine(limits), self.risk_state, self._state, clock=self.clock)
        self.bot = BotConfig("replay", cfg.strategy, cfg.version, cfg.symbol, cfg.timeframe, "PAPER", EXCHANGE)
        self.equity: list[tuple[datetime, float]] = []
        self.ticks: dict[str, int] = {}
        self.exits: dict[str, int] = {}
        self.notes: list[tuple[datetime, str, str]] = []
        self._reason: Optional[str] = None
        self._trades: list[Trade] = []
        self._cur: dict[str, Any] = {}

    async def _state(self, symbol: str, timeframe: str) -> MarketState:
        # only candles closed before the current instant, never the bar being played
        shown = self.candles[max(0, self._i - self.cfg.lookback):self._i]
        if not shown:
            raise LookupError("no closed candle yet")
        return MarketState(symbol=symbol, as_of=shown[-1].t + self.step, candles=shown)

    # ------------------------------------------------------------------ price path
    def _positions(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM positions WHERE status<>'CLOSED'")]

    def _vertices(self, b: Candle) -> list[tuple[float, float]]:
        # (fraction of the bar, mid). Worst case for a long: the low comes before the high.
        return [(0.0, b.o), (1 / 3, b.l), (2 / 3, b.h), (1.0, b.c)]

    def _level_points(self, b: Candle, a: tuple[float, float], z: tuple[float, float]) -> list[tuple[float, float]]:
        pos = [p for p in self._positions() if p["stop_loss"] is not None or p["take_profit"] is not None]
        if not pos or a[1] == z[1]:
            return []
        half = self.sim.half_spread(b)
        levels: list[float] = []
        for p in pos:
            if p["stop_loss"] is not None:
                levels.append(p["stop_loss"] / (1 - half) * (1 - _NUDGE))      # bid reaches the stop
            if p["take_profit"] is not None:
                levels.append(p["take_profit"] / (1 - half) * (1 + _NUDGE))    # bid reaches the take profit
        lo, hi = min(a[1], z[1]), max(a[1], z[1])
        inside = sorted({lv for lv in levels if lo < lv < hi}, reverse=a[1] > z[1])
        span = z[1] - a[1]
        return [(a[0] + (lv - a[1]) / span * (z[0] - a[0]), lv) for lv in inside]

    async def _point(self, b: Candle, frac: float, mid: float) -> None:
        t = b.t + self.step * frac if frac < 1 else b.t + self.step
        self.clock.set(t)                                             # never backwards: the clock refuses, loudly
        self.sim.set_market(self.cfg.symbol, mid, self.clock(), b)
        await self.engine.sync_active_orders("PAPER")                   # resting / partial orders: the exchange report is applied
        rep = await self.monitor.run_once()
        for _pid, reason, outcome in rep.exits:
            self.exits[reason] = self.exits.get(reason, 0) + 1
            self._reason = reason
        self._collect()

    def _collect(self) -> None:
        """Turn fills into round trips: a trade starts at the first buy from flat and ends when the holding is flat again."""
        for f in self.sim.fills[self._seen:]:
            self._seen += 1
            if f.side == "BUY":
                c = self._cur.setdefault("buy", {"q": 0.0, "cost": 0.0, "fees": 0.0, "t": f.at})
                c["q"] += f.quantity
                c["cost"] += f.quantity * f.price
                c["fees"] += f.fee
            else:
                c = self._cur.get("buy")
                if c is None:
                    continue
                s = self._cur.setdefault("sell", {"q": 0.0, "proceeds": 0.0, "fees": 0.0})
                s["q"] += f.quantity
                s["proceeds"] += f.quantity * f.price
                s["fees"] += f.fee
                self._cur["last"] = f.at
                if s["q"] >= c["q"] - self.cfg.sim.step_size / 2:
                    fees = c["fees"] + s["fees"]
                    self._trades.append(Trade(c["t"], f.at, c["q"], c["cost"] / c["q"], s["proceeds"] / s["q"], fees,
                                              s["proceeds"] - c["cost"] - fees, self._reason or "UNKNOWN"))
                    self._cur, self._reason = {}, None

    _seen = 0

    # ------------------------------------------------------------------ main loop
    async def play(self) -> None:
        for j, b in enumerate(self.candles):
            self._i = j                                                   # candles 0..j-1 are closed now
            await self._point(b, 0.0, b.o)                               # the open: waiting orders fill, gaps are exposed to the stops
            if j > 0:
                had_position = bool(self._positions())
                r = await self.runner.tick(self.bot)
                self.ticks[r.outcome] = self.ticks.get(r.outcome, 0) + 1
                if r.outcome != "NO_SIGNAL":
                    self.notes.append((self.clock(), r.outcome, r.detail))
                if r.outcome == "EXECUTED" and had_position:
                    self._reason = "STRATEGY"
                self._collect()
            vs = self._vertices(b)
            for k in range(1, len(vs)):
                for frac, mid in self._level_points(b, vs[k - 1], vs[k]):
                    await self._point(b, frac, mid)
                await self._point(b, vs[k][0], vs[k][1])
            await self._mark(b)
        self._i = len(self.candles)

    async def _mark(self, b: Candle) -> None:
        q = await self.sim.get_ticker(self.cfg.symbol)
        held = self.sim.holdings.get(self.cfg.symbol, 0.0)
        equity = self.sim.cash + held * q["bid"]
        self.risk_state.record_equity(SCOPE, equity, now=self.clock())
        self.equity.append((b.t + self.step, equity))


def _digest(fills: list[SimFill], equity: list[tuple[datetime, float]]) -> str:
    h = hashlib.sha256()
    for f in fills:                                     # client order ids are excluded: they carry random correlation material
        h.update(f"{f.side}|{f.quantity.hex()}|{f.price.hex()}|{f.fee.hex()}|{f.at.isoformat()}|{f.bar_time.isoformat()}\n".encode())
    for t, e in equity:
        h.update(f"{t.isoformat()}|{e.hex()}\n".encode())
    return h.hexdigest()


def replay(dataset: Dataset, config: ReplayConfig, *, workdir: Optional[str] = None) -> ReplayResult:
    """Run the replay in a throwaway SQLite database and return the result. Deterministic: same dataset and config, same digest.
    Virtual time is switched on before the database is even created, so no stored timestamp (not even a migration stamp) is wall-clock."""
    if not dataset.candles:
        raise ValueError("empty dataset")
    own = workdir is None
    tmp = tempfile.TemporaryDirectory() if own else None
    path = os.path.join(tmp.name if tmp else workdir, "replay.db")
    clock = VirtualClock(dataset.candles[0].t)
    db = None
    try:
        with timesource.virtual_time(clock):
            db = Database(path)
            run = _Run(dataset, config, db, clock)
            asyncio.run(run.play())
            with db.read() as c:
                incidents = c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        return ReplayResult(
            simulated=True, config=config.record(), dataset=dataset.record(), trades=run._trades, fills=list(run.sim.fills),
            equity=run.equity, ticks=dict(run.ticks), exits=dict(run.exits), notes=list(run.notes), final_cash=run.sim.cash,
            open_position_at_end=run.sim.holdings.get(config.symbol, 0.0) > config.sim.step_size / 2,
            incidents=incidents, bars=len(run.candles), digest=_digest(run.sim.fills, run.equity))
    finally:
        if db is not None:
            db.close()
        if tmp:
            tmp.cleanup()


def result_json(r: ReplayResult) -> str:
    return json.dumps({"simulated": r.simulated, "digest": r.digest, "bars": r.bars, "ticks": r.ticks, "exits": r.exits,
                       "trades": len(r.trades), "final_cash": r.final_cash}, sort_keys=True)
