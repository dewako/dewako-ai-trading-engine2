"""Import Binance SPOT kline archives (the monthly files from data.binance.vision) into a candle store. Offline: reads a file, never downloads.

    python -m dewako.market_data.archive_import --zip BTCUSDT-5m.zip --store candles.db

What it does and refuses (nothing is repaired or guessed):
  * Only spot files. A delivery-futures name such as BTCUSD_250328-5m-2024-10.csv is refused: those are separate expiring contracts.
  * A file is imported whole or not at all. Any malformed row, non-finite or non-positive price, high/low that does not contain open/close,
    timestamp off the timeframe grid, duplicate or out-of-order timestamp, or a candle outside the month in the file name refuses that file.
  * Timestamps are milliseconds or microseconds (Binance switched in 2025); the unit is detected per file from the magnitude and reported.
  * Checksums: a monthly .zip inside the archive is checked against its .CHECKSUM file when the archive carries one. Without one the file is
    reported UNVERIFIED. Integrity relative to Binance cannot be established from a checksum that is not there.
  * Gaps and zero-volume candles are reported, not filled. build_dataset refuses a series with an unexplained gap later.

Candle source label: "binance_archive". A candle becomes public when it closes, so publication time = open time + one timeframe.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from dewako.core import symbols
from dewako.core.types import Candle, DataQuality
from dewako.market_data.models import CandleRecord
from dewako.market_data.store import CandleStore
from dewako.market_data.timeframes import delta, from_ms, seconds

SOURCE = "binance_archive"
_SPOT = re.compile(r"^(?P<sym>[A-Z0-9]+)-(?P<tf>\d+[mhd])-(?P<y>\d{4})-(?P<m>\d{2})\.csv$")
_FUTURES = re.compile(r"^[A-Z0-9]+_\d{6}-")


@dataclass
class FileReport:
    name: str
    symbol: str = ""
    timeframe: str = ""
    rows: int = 0
    first: Optional[str] = None
    last: Optional[str] = None
    unit: str = ""
    checksum: str = "NOT_APPLICABLE"       # OK | MISMATCH | MISSING | NOT_APPLICABLE (a bare CSV has nothing to check against)
    zero_volume: int = 0
    refused: Optional[str] = None
    imported_new: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    files: list[FileReport] = field(default_factory=list)
    gaps: dict[str, list[dict]] = field(default_factory=dict)      # symbol|timeframe -> [{"after": iso, "before": iso, "missing": n}]

    @property
    def refused(self) -> list[FileReport]:
        return [f for f in self.files if f.refused]

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.refused and not any(f.checksum in ("MISMATCH",) for f in self.files)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "files": [f.__dict__ for f in self.files], "gaps": self.gaps,
                "rows": sum(f.rows for f in self.files if not f.refused), "refused": len(self.refused)}


def _unit_ms(t: int) -> tuple[int, str]:
    """Binance spot archives: milliseconds before 2025, microseconds after. A 13-digit value is ms, a 16-digit value is µs."""
    if t >= 10 ** 15:
        return t // 1000, "us"
    if t >= 10 ** 11:
        return t, "ms"
    raise ValueError(f"timestamp {t} is neither milliseconds nor microseconds")


def parse_csv(raw: bytes, name: str, timeframe: str, year: int, month: int) -> tuple[list[Candle], str, int]:
    """-> candles, unit, zero_volume. Raises ValueError with the first problem found (the whole file is then refused)."""
    step_ms = seconds(timeframe) * 1000
    out: list[Candle] = []
    unit, zero, prev = "", 0, None
    for n, row in enumerate(csv.reader(io.StringIO(raw.decode("utf-8"))), 1):
        if not row:
            continue
        if n == 1 and not row[0].strip().lstrip("-").isdigit():
            continue                                                  # a header line
        if len(row) < 7:
            raise ValueError(f"line {n}: {len(row)} columns, expected at least 7")
        try:
            t_raw = int(row[0])
            close_raw = int(row[6])
            o, h, lo, c, v = (float(x) for x in row[1:6])
        except ValueError:
            raise ValueError(f"line {n}: a field is not a number") from None
        open_ms, u = _unit_ms(t_raw)
        close_ms, u2 = _unit_ms(close_raw)
        if u2 != u:
            raise ValueError(f"line {n}: open and close times use different units")
        if unit and u != unit:
            raise ValueError(f"line {n}: the time unit changes inside the file ({unit} to {u})")
        unit = u
        if not all(math.isfinite(x) for x in (o, h, lo, c, v)) or min(o, h, lo, c) <= 0 or v < 0:
            raise ValueError(f"line {n}: a price is not positive and finite, or the volume is negative")
        if h < max(o, c) or lo > min(o, c) or h < lo:
            raise ValueError(f"line {n}: high/low do not contain open and close")
        if open_ms % step_ms:
            raise ValueError(f"line {n}: time {open_ms} is not on the {timeframe} grid")
        if close_ms != open_ms + step_ms - 1:
            raise ValueError(f"line {n}: close time is not open time + {timeframe} - 1 ms")
        if prev is not None and open_ms <= prev:
            raise ValueError(f"line {n}: timestamp {open_ms} is {'a duplicate' if open_ms == prev else 'out of order'}")
        t = from_ms(open_ms)
        if (t.year, t.month) != (year, month):
            raise ValueError(f"line {n}: candle {t.isoformat()} is outside {year}-{month:02d} named in the file")
        prev = open_ms
        zero += v == 0
        out.append(Candle(t=t, o=o, h=h, l=lo, c=c, v=v))
    if not out:
        raise ValueError("the file has no candles")
    return out, unit, zero


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_digest(text: str, fname: str) -> Optional[str]:
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) and (len(parts) == 1 or parts[-1].lstrip("*").endswith(fname)):
            return parts[0].lower()
    return None


def _members(z: zipfile.ZipFile):
    """Yield (csv_name, raw_bytes, checksum_state). Handles bare CSVs and monthly zips (with their .CHECKSUM sibling) at any depth of one."""
    names = {i.filename for i in z.infolist() if not i.is_dir()}
    for n in sorted(names):
        base = n.rsplit("/", 1)[-1]
        if base.lower().endswith(".csv"):
            yield base, z.read(n), "NOT_APPLICABLE"
        elif base.lower().endswith(".zip"):
            blob = z.read(n)
            sib = next((x for x in (n + ".CHECKSUM", n[:-4] + ".CHECKSUM", n + ".checksum") if x in names), None)
            state = "MISSING"
            if sib:
                want = _expected_digest(z.read(sib).decode("utf-8", "replace"), base)
                state = "MISSING" if want is None else ("OK" if want == _sha256_of(blob) else "MISMATCH")
            with zipfile.ZipFile(io.BytesIO(blob)) as inner:
                for m in inner.infolist():
                    if m.filename.lower().endswith(".csv"):
                        yield m.filename.rsplit("/", 1)[-1], inner.read(m), state


def import_archive(zip_path: str, store: CandleStore, *, now: Optional[datetime] = None, timeframe: Optional[str] = None) -> ImportReport:
    rep, now = ImportReport(), now or datetime.now(timezone.utc)
    seen: dict[tuple[str, str], set[int]] = {}
    with zipfile.ZipFile(zip_path) as z:
        for name, raw, state in _members(z):
            fr = FileReport(name=name, checksum=state)
            rep.files.append(fr)
            if _FUTURES.match(name):
                fr.refused = "delivery-futures contract file: not spot data (separate expiring instruments); not imported"
                continue
            m = _SPOT.match(name)
            if not m:
                fr.refused = "file name is not a Binance spot kline archive name (SYMBOL-TF-YYYY-MM.csv)"
                continue
            tf = m["tf"]
            fr.timeframe = tf
            if timeframe and tf != timeframe:
                fr.refused = f"timeframe {tf} is not the requested {timeframe}"
                continue
            try:
                fr.symbol = symbols.normalize(m["sym"], "binance")
                candles, fr.unit, fr.zero_volume = parse_csv(raw, name, tf, int(m["y"]), int(m["m"]))
            except ValueError as e:
                fr.refused = str(e)
                continue
            if state == "MISMATCH":
                fr.refused = "checksum does not match the .CHECKSUM file: the file is corrupted or altered"
                continue
            if state in ("MISSING", "NOT_APPLICABLE"):
                fr.notes.append("no checksum available for this file: integrity UNVERIFIED")
            step = delta(tf)
            recs = [CandleRecord(symbol=fr.symbol, timeframe=tf, candle=c, quality=DataQuality(
                source=SOURCE, data_timestamp=c.t, publication_timestamp=c.t + step, ingested_at=now)) for c in candles]
            res = store.put_candles(recs, now=now)
            fr.rows, fr.first, fr.last, fr.imported_new = len(candles), candles[0].t.isoformat(), candles[-1].t.isoformat(), res.new
            if res.revised:
                fr.notes.append(f"{res.revised} candle(s) differ from what the store already held (stored as revisions)")
            if fr.zero_volume:
                fr.notes.append(f"{fr.zero_volume} zero-volume candle(s)")
            seen.setdefault((fr.symbol, tf), set()).update(int(c.t.timestamp() * 1000) for c in candles)
    for (sym, tf), ts in seen.items():
        step_ms, order = seconds(tf) * 1000, sorted(ts)
        g = [{"after": from_ms(a).isoformat(), "before": from_ms(b).isoformat(), "missing": (b - a) // step_ms - 1}
             for a, b in zip(order, order[1:]) if b - a != step_ms]
        if g:
            rep.gaps[f"{sym}|{tf}"] = g
    return rep


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dewako.market_data.archive_import", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", required=True, help="archive holding monthly spot kline CSVs / zips")
    p.add_argument("--store", required=True, help="candle store (SQLite) to write")
    p.add_argument("--timeframe", default=None)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    rep = import_archive(a.zip, CandleStore(a.store), timeframe=a.timeframe)
    if a.json:
        print(json.dumps(rep.as_dict(), indent=1))
    else:
        for f in rep.files:
            print(f"{'REFUSED ' if f.refused else 'ok      '}{f.name:30} rows={f.rows:6} unit={f.unit or '-':2} checksum={f.checksum:14} {f.refused or '; '.join(f.notes)}")
        for k, g in rep.gaps.items():
            print(f"GAPS {k}: {len(g)} gap(s), {sum(x['missing'] for x in g)} candle(s) missing")
        print(f"files={len(rep.files)} imported_rows={sum(f.rows for f in rep.files if not f.refused)} refused={len(rep.refused)}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
