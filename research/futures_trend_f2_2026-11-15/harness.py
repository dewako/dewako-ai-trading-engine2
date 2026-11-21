"""F2 trend rule (55-day breakout, 3 ATR trailing stop), long + short, through the paper futures core. See PROTOCOL.md. Research only."""
from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend"))
from dewako.futures import FuturesConfig, MarginMode, PaperFuturesAccount, entry_guard  # noqa: E402

DAY = 86_400_000
START_MS, END_MS = 1577836800000, 1793491200000          # 2020-01-01 .. 2026-11-01 (exclusive)
OOS_MS = 1704067200000                                     # 2024-01-01
FUND_DAY = 0.0003                                          # 0.01% per 8 hours


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float


def load_daily(db: str, symbol: str, start_ms: int = START_MS, end_ms: int = END_MS) -> list[Bar]:
    q = ("SELECT open_ms, o, h, l, c FROM candles WHERE symbol=? AND timeframe='5m' AND source='binance_archive' "
         "AND open_ms>=? AND open_ms<? ORDER BY open_ms")
    out: list[Bar] = []
    for t, o, h, l, c in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(q, (symbol, start_ms, end_ms)):
        d = t // DAY * DAY
        if out and out[-1].t == d:
            b = out[-1]
            b.h, b.l, b.c = max(b.h, h), min(b.l, l), c
        else:
            out.append(Bar(d, o, h, l, c))
    return out


def atr_wilder(bars: list[Bar], n: int) -> list:
    """ATR known at each bar's close (None until n true ranges exist)."""
    out, a, trs = [], None, []
    for i, b in enumerate(bars):
        tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - bars[i - 1].c), abs(b.l - bars[i - 1].c))
        if a is None:
            trs.append(tr)
            if len(trs) == n:
                a = sum(trs) / n
        else:
            a = (a * (n - 1) + tr) / n
        out.append(a)
    return out


def signal(bars: list[Bar], i: int, n: int) -> str:
    """Breakout decided at the close of bar i against the n bars BEFORE it. '' = none."""
    if i < n:
        return ""
    prev = bars[i - n:i]
    if bars[i].c > max(b.h for b in prev):
        return "LONG"
    if bars[i].c < min(b.l for b in prev):
        return "SHORT"
    return ""


@dataclass
class Trade:
    symbol: str
    side: str
    entry_ms: int
    exit_ms: int
    pnl: float
    risked: float
    liquidated: bool = False

    @property
    def r(self) -> float:
        return self.pnl / self.risked


def run(data: dict, n: int = 55, atr_n: int = 20, k: float = 3.0, leverage: int = 1, fee: float = 0.001, slip: float = 0.0005,
        shorts_funding: str = "none", cash: float = 10_000.0, sides=("LONG", "SHORT"), dd_limit: float | None = None) -> dict:
    """data: {symbol: [Bar]}. shorts_funding: 'none' (shorts receive nothing), 'pay' (shorts pay too), 'receive'.
    dd_limit: None = every entry risks 1% of equity (F2, unchanged). A number such as 0.15 = drawdown-aware sizing: an entry risks
    1% x max(0, 1 - drawdown / dd_limit), where drawdown is (peak closing equity - equity now) / peak; at or beyond dd_limit no new entry is taken."""
    acct = PaperFuturesAccount(cash, FuturesConfig(fee_rate=fee, slippage=slip, max_leverage=3))
    risk = 0.01 * leverage
    idx = {s: {b.t: i for i, b in enumerate(bars)} for s, bars in data.items()}
    atrs = {s: atr_wilder(bars, atr_n) for s, bars in data.items()}
    for s in data:
        acct.set_margin_mode(s, MarginMode.ISOLATED)
        acct.set_leverage(s, leverage)
    days = sorted({b.t for bars in data.values() for b in bars})
    pending: dict = {}             # symbol -> (side, atr at signal)
    open_: dict = {}               # symbol -> dict(side, stop, best, entry_ms, risked, money0)
    trades: list[Trade] = []
    curve: list = []
    skipped = 0
    skipped_dd = 0
    peak = cash

    def finish(s, t, liquidated=False):
        st = open_.pop(s)      # one trade's profit = this symbol's ledger entries since its entry (fees, funding, P&L, liquidation)
        pnl = sum(a for _, sym, a in acct.ledger[st["ledger0"]:] if sym == s)
        trades.append(Trade(s, st["side"], st["entry_ms"], t, pnl, st["risked"], liquidated))

    for t in days:
        for s, bars in data.items():
            i = idx[s].get(t)
            if i is None:
                continue
            b = bars[i]
            acct.set_mark(s, b.o)
            if s in open_ and s not in acct.positions:
                finish(s, t, True)
            if s in open_:
                st = open_[s]
                long = st["side"] == "LONG"
                hit_open = b.o <= st["stop"] if long else b.o >= st["stop"]
                hit_day = b.l <= st["stop"] if long else b.h >= st["stop"]
                if hit_open or hit_day:
                    if not hit_open:
                        acct.set_mark(s, st["stop"])
                    if s in acct.positions:
                        acct.close(s)
                        finish(s, t)
                    else:
                        finish(s, t, True)
            if s in pending and s not in open_:
                side, a = pending.pop(s)
                eq = acct.equity()
                scale = 1.0 if dd_limit is None else max(0.0, 1.0 - max(0.0, (peak - eq) / peak) / dd_limit)
                if scale <= 0.0:
                    skipped_dd += 1
                    continue
                qty = min(eq * risk * scale / (k * a), leverage * eq / 3 / b.o)
                stop = b.o - k * a if side == "LONG" else b.o + k * a
                mmr = acct.config.mmr(qty * b.o)
                liq = b.o * (1 - 1 / leverage) / (1 - mmr) if side == "LONG" else b.o * (1 + 1 / leverage) / (1 + mmr)
                if stop <= 0 or entry_guard(side, b.o, stop, liq if liq > 0 else None, leverage, acct.config.max_leverage):
                    skipped += 1
                else:
                    l0 = len(acct.ledger)
                    f = acct.market_order(s, "BUY" if side == "LONG" else "SELL", qty)
                    open_[s] = dict(side=side, stop=stop, best=b.o, entry_ms=t, risked=f.qty * k * a, ledger0=l0)
                    if b.l <= stop if side == "LONG" else b.h >= stop:      # stopped on the entry day itself
                        acct.set_mark(s, stop)
                        if s in acct.positions:
                            acct.close(s)
                            finish(s, t)
                        else:
                            finish(s, t, True)
            # the close: marks, liquidation, funding, trailing stop, next signal
            acct.set_mark(s, b.c)
            if s in open_ and s not in acct.positions:
                finish(s, t, True)
            if s in open_:
                st = open_[s]
                if st["side"] == "LONG":
                    acct.apply_funding(s, FUND_DAY)
                elif shorts_funding != "none":
                    acct.apply_funding(s, FUND_DAY if shorts_funding == "receive" else -FUND_DAY)
                if s not in acct.positions:
                    finish(s, t, True)
                else:
                    a = atrs[s][i]
                    if st["side"] == "LONG":
                        st["best"] = max(st["best"], b.c)
                        st["stop"] = max(st["stop"], st["best"] - k * a)
                    else:
                        st["best"] = min(st["best"], b.c)
                        st["stop"] = min(st["stop"], st["best"] + k * a)
            elif atrs[s][i] is not None:
                sig = signal(bars, i, n)
                if sig and sig in sides:
                    pending[s] = (sig, atrs[s][i])
        acct.check()
        curve.append((t, acct.equity()))
        peak = max(peak, curve[-1][1])
    return dict(trades=trades, curve=curve, skipped=skipped, skipped_dd=skipped_dd, final=curve[-1][1], liquidations=len(acct.liquidations),
                open_at_end=len(open_))


def max_drawdown(curve) -> float:
    peak, dd = curve[0][1], 0.0
    for _, e in curve:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return dd


def buy_and_hold(data: dict, cash: float = 10_000.0) -> list:
    days = sorted({b.t for bars in data.values() for b in bars})
    px = {s: {b.t: b.c for b in bars} for s, bars in data.items()}
    units = {s: (cash / len(data)) / bars[0].o for s, bars in data.items()}
    first = {s: bars[0].t for s, bars in data.items()}
    last, out = {}, []
    for t in days:
        e = 0.0
        for s in data:
            if t < first[s]:
                e += cash / len(data)
            else:
                last[s] = px[s].get(t, last.get(s))
                e += units[s] * last[s]
        out.append((t, e))
    return out
