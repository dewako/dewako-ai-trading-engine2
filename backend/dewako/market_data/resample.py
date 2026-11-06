"""Build a higher timeframe from stored candles, offline and deterministically (5m -> 15m / 1h / ...).

    python -m dewako.market_data.resample --store candles.db --symbol BTC/USDT --from 5m --to 1h --source binance_archive \\
        --start 2025-11-01 --end 2026-11-01

Rules: buckets are aligned to the UTC epoch; a bucket is built only from a COMPLETE, gap-free set of source candles, an incomplete bucket is
dropped and counted (never padded or interpolated); open = first, high = max, low = min, close = last, volume = sum. A candle is public when its
bucket closes. The result is stored under its own source label "<source>:resampled:<from>" so it can never be mistaken for exchange-published
candles of that timeframe (exchange bars may differ from the aggregate, e.g. in volume rounding).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dewako.core.types import Candle, DataQuality
from dewako.market_data.models import CandleRecord
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import delta, floor, seconds


@dataclass
class ResampleResult:
    candles: list[Candle]
    dropped: int          # buckets with at least one source candle but not all of them
    source_label: str = ""


def resample(candles: list[Candle], from_tf: str, to_tf: str) -> ResampleResult:
    a, b = seconds(from_tf), seconds(to_tf)
    if b <= a or b % a:
        raise ValueError(f"{to_tf} is not a whole multiple of {from_tf}")
    need = b // a
    buckets: dict[datetime, list[Candle]] = {}
    last = None
    for c in candles:
        if last is not None and c.t <= last:
            raise ValueError("candles must be strictly increasing in time")
        last = c.t
        buckets.setdefault(floor(c.t, to_tf), []).append(c)
    out, dropped = [], 0
    for t0, grp in sorted(buckets.items()):
        complete = len(grp) == need and all(grp[i].t == t0 + delta(from_tf) * i for i in range(need))
        if not complete:
            dropped += 1
            continue
        out.append(Candle(t=t0, o=grp[0].o, h=max(c.h for c in grp), l=min(c.l for c in grp), c=grp[-1].c, v=sum(c.v for c in grp)))
    return ResampleResult(out, dropped)


def resample_store(store: CandleStore, symbol: str, from_tf: str, to_tf: str, source: str, start: datetime, end: datetime, *,
                   now: Optional[datetime] = None) -> ResampleResult:
    now = now or datetime.now(timezone.utc)
    recs = store.get_candles(symbol, from_tf, source, start, end, as_of=end)
    res = resample([r.candle for r in recs], from_tf, to_tf)
    label = f"{source}:resampled:{from_tf}"
    step = delta(to_tf)
    store.put_candles([CandleRecord(symbol=symbol, timeframe=to_tf, candle=c, quality=DataQuality(
        source=label, data_timestamp=c.t, publication_timestamp=c.t + step, ingested_at=now)) for c in res.candles], now=now)
    res.source_label = label
    return res


def _t(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dewako.market_data.resample", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="from_tf", required=True)
    p.add_argument("--to", dest="to_tf", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--start", required=True, type=_t)
    p.add_argument("--end", required=True, type=_t)
    a = p.parse_args(argv)
    r = resample_store(CandleStore(a.store), a.symbol, a.from_tf, a.to_tf, a.source, a.start, a.end)
    print(f"{len(r.candles)} {a.to_tf} candles written as source {r.source_label!r}; {r.dropped} incomplete bucket(s) dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
