"""Validated, hashed, reproducible historical datasets (Stage 3A, step 1).

A backtest is only as trustworthy as its data, so a dataset is built in one place and refused unless it is clean:

* every candle is on the timeframe grid, inside the requested range, in time order, with no duplicates;
* prices are finite and positive, volume is finite and not negative, and high/low really bound open/close;
* a missing candle is an ERROR unless the caller lists it as an explained gap (maintenance window, listing date, ...);
* a close-to-close jump or a candle range above `spike_limit` is an ERROR unless the caller acknowledges that exact candle;
* the content hash covers symbol, timeframe, source and every value bit-for-bit, so the same hash means the same data.

Data comes from the point-in-time `CandleStore` as of a fixed time (default: the end of the range), so a rerun later sees the same
candles, and a correction published after `as_of` cannot change an old result. The provider that filled the store is only a source name:
exchange adapters added in a later phase fill the same store and need no change here.

Nothing here contacts an exchange or places an order.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from dewako.core.types import Candle
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import from_ms, seconds, to_ms

DEFAULT_SPIKE_LIMIT = 0.10      # 10 % between two consecutive closes, or inside one candle


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str               # "ERROR" refuses the dataset; "INFO" is recorded only
    detail: str
    at: Optional[datetime] = None


@dataclass(frozen=True)
class ValidationReport:
    findings: tuple[Finding, ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "ERROR")

    @property
    def ok(self) -> bool:
        return not self.errors

    def kinds(self) -> set[str]:
        return {f.kind for f in self.findings}

    def summary(self) -> dict:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out


class DatasetRejected(ValueError):
    """The data is not fit to be backtested on. `report` lists every problem found."""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("dataset rejected: " + "; ".join(f"{f.kind}: {f.detail}" for f in report.errors[:5])
                         + (f" (+{len(report.errors) - 5} more)" if len(report.errors) > 5 else ""))


@dataclass(frozen=True)
class Dataset:
    symbol: str
    timeframe: str
    source: str
    start: datetime
    end: datetime
    as_of: datetime
    candles: tuple[Candle, ...]
    content_hash: str
    report: ValidationReport = field(compare=False)
    allowed_gaps: tuple[datetime, ...] = ()
    acknowledged_spikes: tuple[datetime, ...] = ()

    def slice(self, start: datetime, end: datetime) -> "Dataset":
        """A sub-period [start, end) as its own dataset: re-validated and re-hashed, so it is identified by its own data."""
        part = [c for c in self.candles if start <= c.t < end]
        gaps = tuple(g for g in self.allowed_gaps if start <= g < end)
        spikes = tuple(x for x in self.acknowledged_spikes if start <= x < end)
        report = validate_candles(part, self.timeframe, start, end, allowed_gaps=gaps, acknowledged_spikes=spikes)
        if not report.ok:
            raise DatasetRejected(report)
        return Dataset(self.symbol, self.timeframe, self.source, start, end, self.as_of, tuple(part),
                       content_hash(self.symbol, self.timeframe, self.source, part), report, gaps, spikes)

    def record(self) -> dict:
        """What a run record stores so the exact data can be identified again."""
        return {"symbol": self.symbol, "timeframe": self.timeframe, "source": self.source, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "as_of": self.as_of.isoformat(), "candles": len(self.candles),
                "content_hash": self.content_hash, "allowed_gaps": [g.isoformat() for g in self.allowed_gaps],
                "acknowledged_spikes": [s.isoformat() for s in self.acknowledged_spikes], "findings": self.report.summary()}


def content_hash(symbol: str, timeframe: str, source: str, candles: Iterable[Candle]) -> str:
    """SHA-256 over identity and every value (floats as exact hex), in time order."""
    h = hashlib.sha256()
    h.update(f"dewako-dataset-v1|{symbol}|{timeframe}|{source}\n".encode())
    for c in sorted(candles, key=lambda x: x.t):
        h.update(f"{to_ms(c.t)},{c.o.hex()},{c.h.hex()},{c.l.hex()},{c.c.hex()},{c.v.hex()}\n".encode())
    return h.hexdigest()


def _finite(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


def validate_candles(candles: list[Candle], timeframe: str, start: datetime, end: datetime, *, allowed_gaps: Iterable[datetime] = (),
                     acknowledged_spikes: Iterable[datetime] = (), spike_limit: float = DEFAULT_SPIKE_LIMIT) -> ValidationReport:
    """Check an arbitrary candle list (in the order given) against the rules in the module docstring."""
    if not (spike_limit > 0):
        raise ValueError("spike_limit must be positive")
    step = seconds(timeframe) * 1000
    f: list[Finding] = []
    if to_ms(end) <= to_ms(start):
        raise ValueError("end must be after start")
    if not candles:
        return ValidationReport((Finding("EMPTY", "ERROR", "no candles in the requested range"),))
    allowed = {to_ms(g) for g in allowed_gaps}
    acked = {to_ms(s) for s in acknowledged_spikes}
    seen: set[int] = set()
    prev_ms: Optional[int] = None
    prev_close: Optional[float] = None
    usable: list[tuple[int, Candle]] = []
    zero_vol = 0
    for c in candles:
        if c.t.tzinfo is None:
            f.append(Finding("NAIVE_TIME", "ERROR", "candle time has no timezone"))
            continue
        ms = to_ms(c.t)
        if ms % step != 0:
            f.append(Finding("OFF_GRID", "ERROR", f"{c.t.isoformat()} is not on the {timeframe} grid", c.t))
        if not (to_ms(start) <= ms < to_ms(end)):
            f.append(Finding("OUT_OF_RANGE", "ERROR", f"{c.t.isoformat()} is outside [{start.isoformat()}, {end.isoformat()})", c.t))
        if ms in seen:
            f.append(Finding("DUPLICATE", "ERROR", f"{c.t.isoformat()} appears more than once", c.t))
            continue
        seen.add(ms)
        if prev_ms is not None and ms < prev_ms:
            f.append(Finding("OUT_OF_ORDER", "ERROR", f"{c.t.isoformat()} comes after {from_ms(prev_ms).isoformat()}", c.t))
        prev_ms = ms if prev_ms is None else max(prev_ms, ms)
        if not _finite(c.o, c.h, c.l, c.c) or min(c.o, c.h, c.l, c.c) <= 0:
            f.append(Finding("BAD_PRICE", "ERROR", f"{c.t.isoformat()} has a non-finite or non-positive price", c.t))
            continue
        if not _finite(c.v) or c.v < 0:
            f.append(Finding("BAD_VOLUME", "ERROR", f"{c.t.isoformat()} has a non-finite or negative volume", c.t))
        elif c.v == 0:
            zero_vol += 1
        if c.h < max(c.o, c.c, c.l) or c.l > min(c.o, c.c, c.h):
            f.append(Finding("BAD_OHLC", "ERROR", f"{c.t.isoformat()} high/low do not bound open/close", c.t))
            continue
        usable.append((ms, c))
    if zero_vol:
        f.append(Finding("ZERO_VOLUME", "INFO", f"{zero_vol} candle(s) with zero volume"))
    # gaps: every grid point in the range must be present or explained
    first = -(-to_ms(start) // step) * step
    missing, t = [], first
    while t < to_ms(end):
        if t not in seen:
            missing.append(t)
        t += step
    run_start, run_len, prev_m = None, 0, None
    unexplained: list[tuple[int, int]] = []
    explained = 0
    for m in missing:
        if m in allowed:
            explained += 1
            continue
        if prev_m is not None and m == prev_m + step and run_start is not None:
            run_len += 1
        else:
            if run_start is not None:
                unexplained.append((run_start, run_len))
            run_start, run_len = m, 1
        prev_m = m
    if run_start is not None:
        unexplained.append((run_start, run_len))
    for s, n in unexplained:
        f.append(Finding("GAP", "ERROR", f"{n} candle(s) missing from {from_ms(s).isoformat()}", from_ms(s)))
    if explained:
        f.append(Finding("ALLOWED_GAP", "INFO", f"{explained} missing candle(s) explained by the caller"))
    for g in allowed:
        if g in seen:
            f.append(Finding("ALLOWED_GAP_PRESENT", "ERROR", f"{from_ms(g).isoformat()} was listed as a gap but the candle exists"))
    # spikes, only between candles that are adjacent in time
    by_time = sorted(usable, key=lambda x: x[0])
    prev_ms, prev_close = None, None
    for ms, c in by_time:
        suspect = []
        if prev_close is not None and prev_ms == ms - step and abs(c.c / prev_close - 1) > spike_limit:
            suspect.append(f"close moved {c.c / prev_close - 1:+.1%} from the previous candle")
        if (c.h - c.l) / c.l > spike_limit:
            suspect.append(f"range {(c.h - c.l) / c.l:.1%} inside one candle")
        if suspect and ms not in acked:
            f.append(Finding("SUSPECT_SPIKE", "ERROR", f"{from_ms(ms).isoformat()}: " + "; ".join(suspect), from_ms(ms)))
        prev_ms, prev_close = ms, c.c
    return ValidationReport(tuple(f))


def build_dataset(store: CandleStore, symbol: str, timeframe: str, source: str, start: datetime, end: datetime, *,
                  as_of: Optional[datetime] = None, allowed_gaps: Iterable[datetime] = (),
                  acknowledged_spikes: Iterable[datetime] = (), spike_limit: float = DEFAULT_SPIKE_LIMIT) -> Dataset:
    """Load [start, end) from the store as known at `as_of` (default `end`), validate, hash and freeze it. Raises DatasetRejected."""
    asof = as_of or end
    recs = store.get_candles(symbol, timeframe, source, start, end, as_of=asof)
    candles = [r.candle for r in recs]
    allowed, acked = tuple(sorted(set(allowed_gaps))), tuple(sorted(set(acknowledged_spikes)))
    report = validate_candles(candles, timeframe, start, end, allowed_gaps=allowed, acknowledged_spikes=acked, spike_limit=spike_limit)
    extra = []
    revised = sum(1 for r in recs if r.quality.revision > 0)
    if revised:
        extra.append(Finding("REVISED_CANDLES", "INFO", f"{revised} candle(s) use a correction published before as_of"))
    low = sum(1 for r in recs if r.quality.confidence < 1.0)
    if low:
        extra.append(Finding("LOW_CONFIDENCE", "INFO", f"{low} candle(s) carry confidence below 1.0"))
    report = ValidationReport(report.findings + tuple(extra))
    if not report.ok:
        raise DatasetRejected(report)
    return Dataset(symbol, timeframe, source, start, end, asof, tuple(candles), content_hash(symbol, timeframe, source, candles),
                   report, allowed, acked)
