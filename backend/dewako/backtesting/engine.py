"""Deterministic backtester.

Look-ahead protection: at bar i the strategy receives candles[: i + 1] only, and any entry
is filled at the OPEN of bar i + 1. Stops are checked against later bars' high/low.
Every run records its full assumptions so it can be reproduced and audited.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

from dewako.core.types import Candle, MarketState, Side
from dewako.strategies.base import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    timeframe: str
    starting_capital: float = 10_000.0
    fee_rate: float = 0.001  # per side
    slippage: float = 0.0005  # per side
    risk_per_trade_pct: float = 0.5
    max_leverage: float = 1.0
    execution: str = "next_bar_open"


@dataclass
class Trade:
    entry_index: int
    exit_index: int
    side: str
    entry: float
    exit: float
    quantity: float
    pnl: float
    reason: list[str]
    exit_reason: str


@dataclass
class BacktestResult:
    record: dict
    trades: list[Trade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def _state(symbol: str, candles: list[Candle]) -> MarketState:
    vols = [c.v for c in candles[-21:-1]] or [candles[-1].v]
    ratio = candles[-1].v / (sum(vols) / len(vols) or 1)
    closes = [c.c for c in candles[-20:]]
    trend = "BULLISH" if closes[-1] > closes[0] * 1.005 else "BEARISH" if closes[-1] < closes[0] * 0.995 else "NEUTRAL"
    return MarketState(symbol=symbol, as_of=candles[-1].t, candles=list(candles), trend=trend, volume_ratio=ratio)


def run(strategy: Strategy, candles: list[Candle], cfg: BacktestConfig, warmup: int = 60) -> BacktestResult:
    equity = cfg.starting_capital
    curve = [equity]
    trades: list[Trade] = []
    pos = None  # dict
    for i in range(warmup, len(candles) - 1):
        bar = candles[i]
        if pos:
            hit = None
            if pos["side"] is Side.BUY and candles[i].l <= pos["stop"]:
                hit = pos["stop"]
            elif pos["side"] is Side.SELL and candles[i].h >= pos["stop"]:
                hit = pos["stop"]
            if hit is not None:
                direction = 1 if pos["side"] is Side.BUY else -1
                fill = hit * (1 - cfg.slippage * direction)
                pnl = (fill - pos["entry"]) * direction * pos["qty"] - (fill + pos["entry"]) * pos["qty"] * cfg.fee_rate
                equity += pnl
                trades.append(Trade(pos["i"], i, pos["side"].value, pos["entry"], fill, pos["qty"], pnl, pos["reason"], "stop"))
                pos = None
        sig = None if pos else strategy.generate_signal(_state(cfg.symbol, candles[: i + 1]))
        if sig and sig.stop_price is not None:
            nxt = candles[i + 1]
            direction = 1 if sig.side is Side.BUY else -1
            entry = nxt.o * (1 + cfg.slippage * direction)
            risk_unit = abs(entry - sig.stop_price)
            if risk_unit > 0:
                qty = min(equity * cfg.risk_per_trade_pct / 100 / risk_unit, equity * cfg.max_leverage / entry)
                pos = {"side": sig.side, "entry": entry, "stop": sig.stop_price, "qty": qty, "i": i + 1, "reason": sig.reason}
        curve.append(equity)
    if pos:  # close at the final close
        last = candles[-1].c
        direction = 1 if pos["side"] is Side.BUY else -1
        pnl = (last - pos["entry"]) * direction * pos["qty"] - (last + pos["entry"]) * pos["qty"] * cfg.fee_rate
        equity += pnl
        trades.append(Trade(pos["i"], len(candles) - 1, pos["side"].value, pos["entry"], last, pos["qty"], pnl, pos["reason"], "end_of_data"))
        curve.append(equity)

    record = {
        "strategy": strategy.name, "version": strategy.version, "parameters": strategy.parameters,
        "config": asdict(cfg), "data_start": candles[0].t.isoformat(), "data_end": candles[-1].t.isoformat(),
        "bars": len(candles),
    }
    record["run_id"] = hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return BacktestResult(record=record, trades=trades, equity=curve, metrics=_metrics(cfg, trades, curve))


def _metrics(cfg: BacktestConfig, trades: list[Trade], curve: list[float]) -> dict:
    n = len(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0.0
    net = curve[-1] - cfg.starting_capital
    return {"net_profit": net, "roi_pct": net / cfg.starting_capital * 100, "trades": n,
            "win_rate_pct": len(wins) / n * 100 if n else 0.0,
            "profit_factor": (sum(wins) / sum(losses)) if losses else (math.inf if wins else 0.0),
            "max_drawdown_pct": mdd * 100, "avg_trade": net / n if n else 0.0,
            "largest_win": max(wins, default=0.0), "largest_loss": -max(losses, default=0.0),
            "sharpe_per_bar": mean / sd if sd else 0.0}


def overfitting_warnings(n_params: int, trades: int, train_roi: float, validation_roi: float,
                         fee_rate: float, slippage: float) -> list[str]:
    w = []
    if n_params > 6:
        w.append("many tunable parameters")
    if trades < 30:
        w.append("fewer than 30 trades")
    if train_roi > 0 and validation_roi < train_roi * 0.35:
        w.append("training performance far better than validation")
    if fee_rate == 0 or slippage == 0:
        w.append("zero fees or slippage is unrealistic")
    return w
