"""Performance metrics for a finished replay. Pure functions of the recorded trades, fills and equity curve: no clock, no database.

Conventions (stated so nobody has to guess):
  * A trade is a closed round trip. Its P&L is after fees. A position still open at the end is NOT a trade; its unrealised result is in
    the equity curve and in `total_return_pct`, not in the trade statistics, and a warning says so.
  * Win = P&L > 0, loss = P&L < 0; a trade with P&L exactly 0 is neither and ends both streaks.
  * Drawdown is measured on the mark-to-market equity curve, starting from the initial cash, so an open loss counts. Percent is of the peak.
  * Execution cost = quantity x |fill price - mid| per fill (spread + slippage); fees are separate. `pnl_before_costs` adds both back.
  * Undefined ratios are None, never infinity (profit factor with no losing trade; payoff with no wins or no losses). Profit factor with
    losers but no winner is a real 0.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional, Sequence

MIN_TRADES_FOR_ANY_CONCLUSION = 30


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Optional[float]
    net_pnl: float
    gross_profit: float
    gross_loss: float                      # positive number
    profit_factor: Optional[float]
    expectancy: Optional[float]            # mean P&L per trade
    avg_win: Optional[float]
    avg_loss: Optional[float]              # positive number
    payoff_ratio: Optional[float]          # avg win / avg loss (the realised reward-to-risk)
    best_trade: Optional[float]
    worst_trade: Optional[float]
    max_consecutive_wins: int
    max_consecutive_losses: int
    avg_holding_seconds: Optional[float]
    median_holding_seconds: Optional[float]
    max_holding_seconds: Optional[float]
    exposure_pct: float                    # share of the period with a position open
    initial_equity: float
    final_equity: float
    total_return_pct: float
    max_drawdown: float                    # in money
    max_drawdown_pct: float
    longest_drawdown_seconds: float        # longest time spent below a previous equity peak
    fees: float
    execution_cost: float
    pnl_before_costs: float
    trades_per_day: float
    open_at_end: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def _streaks(pnls: Sequence[float]) -> tuple[int, int]:
    bw = bl = w = l = 0                    # noqa: E741
    for p in pnls:
        w = w + 1 if p > 0 else 0
        l = l + 1 if p < 0 else 0          # noqa: E741
        bw, bl = max(bw, w), max(bl, l)
    return bw, bl


def drawdown(equity: Sequence[tuple[datetime, float]], initial: float) -> tuple[float, float, float]:
    """(max drawdown in money, max drawdown as a fraction of its peak, longest time below a peak in seconds)."""
    if not equity:
        return 0.0, 0.0, 0.0
    peak, since = initial, None            # `since`: when the curve last left its peak (None while at a peak)
    max_abs = max_pct = longest = 0.0
    last = equity[0][0]
    for t, e in equity:
        if e >= peak:
            if since is not None:
                longest = max(longest, (t - since).total_seconds())
            peak, since = e, None
        else:
            if since is None:
                since = last                # it was at the peak at the previous point
            max_abs = max(max_abs, peak - e)
            max_pct = max(max_pct, (peak - e) / peak if peak > 0 else 0.0)
        last = t
    if since is not None:
        longest = max(longest, (equity[-1][0] - since).total_seconds())
    return max_abs, max_pct, longest


def _exposure(fills, start: datetime, end: datetime) -> float:
    span = (end - start).total_seconds()
    if span <= 0:
        return 0.0
    held, since, total = 0.0, None, 0.0
    for f in sorted(fills, key=lambda f: f.at):
        before = held
        held += f.quantity if f.side == "BUY" else -f.quantity
        if before <= 1e-9 < held:
            since = f.at
        elif before > 1e-9 >= held and since is not None:
            total += (f.at - since).total_seconds()
            since = None
    if since is not None:
        total += (end - since).total_seconds()
    return min(1.0, total / span)


def compute_metrics(trades, equity: Sequence[tuple[datetime, float]], fills, initial_cash: float, *, open_at_end: bool = False) -> Metrics:
    if not equity:
        raise ValueError("no equity curve: nothing to measure")
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), -sum(losses)
    hold = [(t.exit_time - t.entry_time).total_seconds() for t in trades]
    start, end = equity[0][0], equity[-1][0]
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    dd, dd_pct, dd_len = drawdown(equity, initial_cash)
    fees = sum(f.fee for f in fills)
    exec_cost = sum(f.quantity * abs(f.price - f.mid) for f in fills)
    final = equity[-1][1]
    avg_win = gp / len(wins) if wins else None
    avg_loss = gl / len(losses) if losses else None
    bw, bl = _streaks(pnls)
    warn: list[str] = []
    n = len(trades)
    if n == 0:
        warn.append("No closed trades: nothing can be said about this strategy on this data.")
    elif n < MIN_TRADES_FOR_ANY_CONCLUSION:
        warn.append(f"Only {n} closed trades (fewer than {MIN_TRADES_FOR_ANY_CONCLUSION}): the statistics are mostly noise.")
    if n and not losses:
        warn.append("No losing trade: profit factor and payoff ratio are undefined, which usually means too little data, not a perfect strategy.")
    if n and not wins:
        warn.append("No winning trade.")
    if gp > 0 and max(wins) > 0.5 * gp:
        warn.append("One trade makes up more than half of the gross profit: the result depends on a single event.")
    if open_at_end:
        warn.append("A position was still open at the end: its unrealised result is in the equity curve, not in the trade statistics.")
    return Metrics(
        trades=n, wins=len(wins), losses=len(losses), breakeven=n - len(wins) - len(losses),
        win_rate=len(wins) / n if n else None, net_pnl=sum(pnls), gross_profit=gp, gross_loss=gl,
        profit_factor=(gp / gl if gl > 0 else None), expectancy=(sum(pnls) / n if n else None),
        avg_win=avg_win, avg_loss=avg_loss, payoff_ratio=(avg_win / avg_loss if avg_win is not None and avg_loss else None),
        best_trade=max(pnls) if pnls else None, worst_trade=min(pnls) if pnls else None,
        max_consecutive_wins=bw, max_consecutive_losses=bl,
        avg_holding_seconds=(sum(hold) / n if n else None), median_holding_seconds=(statistics.median(hold) if n else None),
        max_holding_seconds=(max(hold) if n else None),
        exposure_pct=_exposure(fills, start, end), initial_equity=initial_cash, final_equity=final,
        total_return_pct=(final / initial_cash - 1.0) if initial_cash > 0 else 0.0,
        max_drawdown=dd, max_drawdown_pct=dd_pct, longest_drawdown_seconds=dd_len,
        fees=fees, execution_cost=exec_cost, pnl_before_costs=sum(pnls) + fees + exec_cost,
        trades_per_day=n / days, open_at_end=open_at_end, warnings=tuple(warn))


def metrics_of(result) -> Metrics:
    """Metrics of a ReplayResult."""
    return compute_metrics(result.trades, result.equity, result.fills, result.config["initial_cash"], open_at_end=result.open_position_at_end)
