"""Resampling 5m to a higher timeframe: exact aggregation, complete buckets only, honest provenance."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from dewako.core.types import Candle, DataQuality  # noqa: E402
from dewako.market_data.models import CandleRecord  # noqa: E402
from dewako.market_data.resample import resample, resample_store  # noqa: E402
from dewako.market_data.store import CandleStore  # noqa: E402

U = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=U)
M5 = timedelta(minutes=5)


def c5(i, o=100.0, h=None, l=None, cl=None, v=1.0):   # noqa: E741
    cl = o if cl is None else cl
    return Candle(t=T0 + M5 * i, o=o, h=max(o, cl) if h is None else h, l=min(o, cl) if l is None else l, c=cl, v=v)


def test_twelve_five_minute_candles_make_one_exact_hour():
    cs = [c5(i, o=100 + i, h=110 + i, l=90 + i, cl=101 + i, v=2.0) for i in range(12)]
    r = resample(cs, "5m", "1h")
    assert r.dropped == 0 and len(r.candles) == 1
    h = r.candles[0]
    assert (h.t, h.o, h.h, h.l, h.c, h.v) == (T0, 100.0, 121.0, 90.0, 112.0, 24.0)       # first open, max high, min low, last close, summed volume


def test_the_high_and_low_come_from_different_candles_inside_the_bucket():
    cs = [c5(i) for i in range(12)]
    cs[3] = c5(3, h=150.0)
    cs[9] = c5(9, l=50.0)
    h = resample(cs, "5m", "1h").candles[0]
    assert (h.h, h.l) == (150.0, 50.0)


def test_an_incomplete_bucket_is_dropped_and_counted_never_padded():
    cs = [c5(i) for i in range(12)] + [c5(i) for i in range(12, 23)]                      # the second hour has 11 of 12
    r = resample(cs, "5m", "1h")
    assert len(r.candles) == 1 and r.dropped == 1


def test_a_gap_inside_a_bucket_drops_that_bucket_even_when_the_count_looks_right():
    cs = [c5(i) for i in range(12) if i != 4] + [c5(12)]                                  # 11 candles in hour 1, one stray in hour 2
    r = resample(cs, "5m", "1h")
    assert r.candles == [] and r.dropped == 2


def test_buckets_are_aligned_to_the_clock_not_to_the_first_candle():
    cs = [c5(i) for i in range(6, 30)]                                                    # starts at 00:30
    r = resample(cs, "5m", "1h")
    assert [c.t for c in r.candles] == [T0 + timedelta(hours=1)] and r.dropped == 2       # 00:30-01:00 and 02:00-02:30 are partial


def test_fifteen_minute_buckets():
    r = resample([c5(i) for i in range(9)], "5m", "15m")
    assert [c.t for c in r.candles] == [T0, T0 + timedelta(minutes=15), T0 + timedelta(minutes=30)]


def test_a_timeframe_that_is_not_a_multiple_is_refused():
    with pytest.raises(ValueError):
        resample([c5(0)], "5m", "7m")
    with pytest.raises(ValueError):
        resample([c5(0)], "1h", "5m")


def test_unsorted_or_duplicate_input_is_refused():
    with pytest.raises(ValueError):
        resample([c5(1), c5(0)], "5m", "1h")
    with pytest.raises(ValueError):
        resample([c5(0), c5(0)], "5m", "1h")


def test_the_stored_result_has_its_own_source_label_and_is_public_only_when_the_hour_closes():
    st = CandleStore(":memory:")
    st.put_candles([CandleRecord(symbol="BTC/USDT", timeframe="5m", candle=c, quality=DataQuality(
        source="binance_archive", data_timestamp=c.t, publication_timestamp=c.t + M5, ingested_at=T0 + timedelta(days=1))) for c in (c5(i) for i in range(24))],
        now=T0 + timedelta(days=1))
    r = resample_store(st, "BTC/USDT", "5m", "1h", "binance_archive", T0, T0 + timedelta(hours=2), now=T0 + timedelta(days=1))
    assert r.source_label == "binance_archive:resampled:5m" and len(r.candles) == 2
    got = st.get_candles("BTC/USDT", "1h", "binance_archive:resampled:5m", T0, T0 + timedelta(hours=2), as_of=T0 + timedelta(days=1))
    assert len(got) == 2 and got[0].quality.publication_timestamp == T0 + timedelta(hours=1)
    assert st.get_candles("BTC/USDT", "1h", "binance_archive", T0, T0 + timedelta(hours=2), as_of=T0 + timedelta(days=1)) == []
    early = st.get_candles("BTC/USDT", "1h", "binance_archive:resampled:5m", T0, T0 + timedelta(hours=2), as_of=T0 + timedelta(minutes=59))
    assert early == []                                                                    # the first hour is not known before it closes


def test_a_bucket_with_the_right_count_but_off_grid_candles_is_still_dropped():
    cs = [c5(i) for i in range(11)] + [Candle(t=T0 + timedelta(minutes=57), o=100.0, h=100.0, l=100.0, c=100.0, v=1.0)]   # 12 candles, one at :57
    r = resample(cs, "5m", "1h")
    assert r.candles == [] and r.dropped == 1
