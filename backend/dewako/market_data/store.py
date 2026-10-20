"""Point-in-time candle store (SQLite).

Each row is one version of one candle. A query "as of T" returns, for every candle, the newest
version that was KNOWN at T:
  - revision 0 is known when it was published (the candle's close).
  - a later revision is known when we ingested it.
So a backtest at T never sees a candle that had not closed, nor a correction made after T.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from dewako.core.types import Candle, DataQuality
from dewako.market_data.models import CandleRecord
from dewako.market_data.timeframes import delta, from_ms, seconds, to_ms

_FAR_FUTURE = 2**62
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL, open_ms INTEGER NOT NULL,
  revision INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
  published_ms INTEGER NOT NULL, known_ms INTEGER NOT NULL, ingested_ms INTEGER NOT NULL, confidence REAL NOT NULL,
  PRIMARY KEY(symbol, timeframe, source, open_ms, revision));
CREATE INDEX IF NOT EXISTS ix_known ON candles(symbol, timeframe, source, open_ms, known_ms);
"""


class PutResult:
    def __init__(self, new=0, unchanged=0, revised=0):
        self.new, self.unchanged, self.revised = new, unchanged, revised

    def __repr__(self):
        return f"PutResult(new={self.new}, unchanged={self.unchanged}, revised={self.revised})"


class CandleStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def put_candles(self, records: list[CandleRecord], now: Optional[datetime] = None) -> PutResult:
        res, now_ms = PutResult(), to_ms(now or datetime.now(timezone.utc))
        with self._lock, self._db:
            for r in records:
                c, q = r.candle, r.quality
                key = (r.symbol, r.timeframe, q.source, to_ms(c.t))
                row = self._db.execute(
                    "SELECT revision,o,h,l,c,v FROM candles WHERE symbol=? AND timeframe=? AND source=? AND open_ms=? "
                    "ORDER BY revision DESC LIMIT 1", key).fetchone()
                vals = (c.o, c.h, c.l, c.c, c.v)
                if row is None:
                    rev, known = 0, to_ms(q.publication_timestamp)
                    res.new += 1
                elif tuple(row[1:]) == vals:
                    res.unchanged += 1
                    continue
                else:
                    rev, known = row[0] + 1, now_ms  # a correction is known from the moment we saw it
                    res.revised += 1
                self._db.execute("INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (*key, rev, *vals, to_ms(q.publication_timestamp), known, to_ms(q.ingested_at), q.confidence))
        return res

    def get_candles(self, symbol: str, timeframe: str, source: str, start: datetime, end: datetime,
                    as_of: Optional[datetime] = None) -> list[CandleRecord]:
        """Candles with open time in [start, end), as known at `as_of` (default: everything we have)."""
        asof = to_ms(as_of) if as_of else _FAR_FUTURE
        with self._lock:
            rows = self._db.execute(
                "SELECT c.open_ms,c.o,c.h,c.l,c.c,c.v,c.published_ms,c.ingested_ms,c.confidence,c.revision FROM candles c "
                "WHERE c.symbol=? AND c.timeframe=? AND c.source=? AND c.open_ms>=? AND c.open_ms<? AND c.known_ms<=? "
                "AND c.revision=(SELECT MAX(d.revision) FROM candles d WHERE d.symbol=c.symbol AND d.timeframe=c.timeframe "
                "AND d.source=c.source AND d.open_ms=c.open_ms AND d.known_ms<=?) ORDER BY c.open_ms",
                (symbol, timeframe, source, to_ms(start), to_ms(end), asof, asof)).fetchall()
        return [CandleRecord(symbol=symbol, timeframe=timeframe,
                             candle=Candle(t=from_ms(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[5]),
                             quality=DataQuality(source=source, data_timestamp=from_ms(r[0]), publication_timestamp=from_ms(r[6]),
                                                 ingested_at=from_ms(r[7]), confidence=r[8], revision=r[9])) for r in rows]

    def find_gaps(self, symbol: str, timeframe: str, source: str, start: datetime, end: datetime,
                  as_of: Optional[datetime] = None) -> list[datetime]:
        """Open times in [start, end) that should exist and are missing. Only fully closed candles count."""
        have = {to_ms(r.candle.t) for r in self.get_candles(symbol, timeframe, source, start, end, as_of)}
        step, horizon = seconds(timeframe) * 1000, to_ms(as_of) if as_of else _FAR_FUTURE
        first = -(-to_ms(start) // step) * step  # round start up to the candle grid
        out, t = [], first
        while t < to_ms(end):
            if t + step <= horizon and t not in have:
                out.append(from_ms(t))
            t += step
        return out

    def latest_open(self, symbol: str, timeframe: str, source: str) -> Optional[datetime]:
        with self._lock:
            r = self._db.execute("SELECT MAX(open_ms) FROM candles WHERE symbol=? AND timeframe=? AND source=?",
                                 (symbol, timeframe, source)).fetchone()
        return from_ms(r[0]) if r and r[0] is not None else None
