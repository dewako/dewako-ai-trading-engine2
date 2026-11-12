"""Market regimes, labelled from PAST data only, and the strategy's result in each.

Two independent labels per candle, each computed from that candle and earlier ones (a test changes the future and checks nothing moves):
  trend       UP / DOWN / RANGE   from the efficiency ratio of the last `trend_window` closes (net move / total path): a straight
                                  move has ratio near 1, a back-and-forth market near 0. UP/DOWN need ratio >= `trend_ratio`.
  volatility  HIGH / NORMAL / LOW  from ATR% against its own trailing history: top third = HIGH, bottom third = LOW.
`UNKNOWN` until there is enough history; unknown trades are reported, never dropped silently.
A trade belongs to the regime of the last candle that was closed when it entered. Regime results are descriptive: a strategy that
only works in one regime is a finding, not a promotion criterion.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from dewako.backtesting.metrics import Metrics, compute_metrics
from dewako.core.types import Candle
from dewako.market_data.timeframes import seconds as timeframe_seconds

UP, DOWN, RANGE, HIGH, NORMAL, LOW, UNKNOWN = "UP", "DOWN", "RANGE", "HIGH", "NORMAL", "LOW", "UNKNOWN"


@dataclass(frozen=True)
class RegimeConfig:
    trend_window: int = 50
    trend_ratio: float = 0.30
    atr_window: int = 14
    vol_history: int = 500            # trailing ATR% values the current one is ranked against
    vol_min_history: int = 50
    high_rank: float = 2 / 3
    low_rank: float = 1 / 3

    def __post_init__(self) -> None:
        if self.trend_window < 2 or self.atr_window < 1 or self.vol_min_history < 2 or self.vol_history < self.vol_min_history:
            raise ValueError("invalid regime windows")
        if not (0 < self.low_rank < self.high_rank < 1) or not (0 < self.trend_ratio <= 1):
            raise ValueError("invalid regime thresholds")


@dataclass(frozen=True)
class Labels:
    time: datetime          # candle open time
    trend: str
    volatility: str


def label_candles(candles: Sequence[Candle], cfg: RegimeConfig = RegimeConfig()) -> list[Labels]:
    n = len(candles)
    tr = [0.0] * n
    for i, c in enumerate(candles):
        tr[i] = c.h - c.l if i == 0 else max(c.h - c.l, abs(c.h - candles[i - 1].c), abs(c.l - candles[i - 1].c))
    atr_pct: list[float | None] = [None] * n
    for i in range(cfg.atr_window - 1, n):
        atr_pct[i] = (sum(tr[i - cfg.atr_window + 1:i + 1]) / cfg.atr_window) / candles[i].c if candles[i].c > 0 else None
    out = []
    for i, c in enumerate(candles):
        trend = UNKNOWN
        if i >= cfg.trend_window:
            w = candles[i - cfg.trend_window:i + 1]
            path = sum(abs(w[k].c - w[k - 1].c) for k in range(1, len(w)))
            net = w[-1].c - w[0].c
            ratio = abs(net) / path if path > 0 else 0.0
            trend = (UP if net > 0 else DOWN) if ratio >= cfg.trend_ratio else RANGE
        vol = UNKNOWN
        cur = atr_pct[i]
        if cur is not None:
            hist = [x for x in atr_pct[max(0, i - cfg.vol_history):i] if x is not None]          # strictly earlier values
            if len(hist) >= cfg.vol_min_history:
                rank = (sum(1 for x in hist if x < cur) + 0.5 * sum(1 for x in hist if x == cur)) / len(hist)      # ties count half
                vol = HIGH if rank >= cfg.high_rank else LOW if rank <= cfg.low_rank else NORMAL
        out.append(Labels(c.t, trend, vol))
    return out


def regime_at(labels: Sequence[Labels], timeframe: str, instant: datetime) -> Labels | None:
    """Labels of the last candle already closed at `instant` (the candle whose open + one bar is <= instant)."""
    step = timedelta(seconds=timeframe_seconds(timeframe))
    last = None
    for lb in labels:
        if lb.time + step <= instant:
            last = lb
        else:
            break
    return last


def by_regime(trades, labels: Sequence[Labels], timeframe: str, initial_cash: float, cfg: RegimeConfig = RegimeConfig()) -> dict[str, dict[str, Metrics]]:
    """{'trend': {'UP': Metrics, ...}, 'volatility': {...}} from the trades only (equity = running closed P&L of that group)."""
    groups: dict[str, dict[str, list]] = {"trend": defaultdict(list), "volatility": defaultdict(list)}
    for t in trades:
        lb = regime_at(labels, timeframe, t.entry_time)
        groups["trend"][lb.trend if lb else UNKNOWN].append(t)
        groups["volatility"][lb.volatility if lb else UNKNOWN].append(t)
    out: dict[str, dict[str, Metrics]] = {"trend": {}, "volatility": {}}
    for dim, g in groups.items():
        for name, ts in g.items():
            ts = sorted(ts, key=lambda x: x.exit_time)
            eq, run = [], initial_cash
            for t in ts:
                run += t.pnl
                eq.append((t.exit_time, run))
            out[dim][name] = compute_metrics(ts, eq, [], initial_cash)
    return out
