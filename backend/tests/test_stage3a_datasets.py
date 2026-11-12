"""Stage 3A step 1: a backtest dataset is validated, hashed and reproducible, or it is refused.

Every rule in `dewako.backtesting.datasets` has a test that fails when the rule is removed (mutation-checked, see the changelog).
Uses the point-in-time CandleStore with in-memory data. Nothing here contacts an exchange.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.datasets import DatasetRejected, build_dataset, content_hash, validate_candles  # noqa: E402
from dewako.core.types import Candle, DataQuality  # noqa: E402
from dewako.market_data.models import CandleRecord  # noqa: E402
from dewako.market_data.store import CandleStore  # noqa: E402

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)
SYM, TF, SRC = "BTC/USDT", "5m", "binance"


def candle(i, o=100.0, h=100.5, l=99.5, c=100.2, v=10.0, base=T0):   # noqa: E741
    return Candle(t=base + M5 * i, o=o, h=h, l=l, c=c, v=v)


def series(n=20, **kw):
    return [candle(i, **kw) for i in range(n)]


def rec(c, conf=1.0, src=SRC):
    return CandleRecord(symbol=SYM, timeframe=TF, candle=c,
                        quality=DataQuality(source=src, data_timestamp=c.t, publication_timestamp=c.t + M5, ingested_at=c.t + M5, confidence=conf))


def store_with(candles, **kw):
    s = CandleStore()
    s.put_candles([rec(c, **kw) for c in candles], now=T0 + timedelta(days=1))
    return s


END = T0 + M5 * 20


def kinds(report):
    return {f.kind for f in report.errors}


class CleanData(unittest.TestCase):
    def test_a_clean_dataset_is_accepted_frozen_and_described(self):
        ds = build_dataset(store_with(series()), SYM, TF, SRC, T0, END)
        self.assertEqual((len(ds.candles), ds.as_of, ds.report.ok), (20, END, True))
        r = ds.record()
        self.assertEqual((r["candles"], r["content_hash"], r["source"], r["findings"]), (20, ds.content_hash, SRC, {}))
        self.assertIsInstance(ds.candles, tuple)                                           # frozen: cannot be edited afterwards

    def test_the_same_data_gives_the_same_hash_and_any_change_gives_another(self):
        a = build_dataset(store_with(series()), SYM, TF, SRC, T0, END).content_hash
        self.assertEqual(a, build_dataset(store_with(series()), SYM, TF, SRC, T0, END).content_hash)
        changed = series()
        changed[7] = candle(7, c=100.2000001)                                              # one tiny change in one value
        self.assertNotEqual(a, build_dataset(store_with(changed), SYM, TF, SRC, T0, END).content_hash)
        self.assertNotEqual(a, content_hash(SYM, TF, "okx", series()))                      # the source is part of the identity
        self.assertNotEqual(a, content_hash("ETH/USDT", TF, SRC, series()))
        self.assertEqual(content_hash(SYM, TF, SRC, series()), content_hash(SYM, TF, SRC, list(reversed(series()))))   # order of input does not matter

    def test_every_field_of_every_candle_is_part_of_the_hash(self):
        base = content_hash(SYM, TF, SRC, series())
        edits = {"open": dict(o=100.1), "high": dict(h=100.6), "low": dict(l=99.4), "close": dict(c=100.3), "volume": dict(v=10.5)}
        for name, kw in edits.items():
            cs = series()
            cs[9] = candle(9, **kw)
            self.assertNotEqual(base, content_hash(SYM, TF, SRC, cs), name)
        shifted = series()
        shifted[9] = candle(9, base=T0 + timedelta(minutes=5))                              # same values, another time
        self.assertNotEqual(base, content_hash(SYM, TF, SRC, shifted))

    def test_zero_volume_is_recorded_but_does_not_refuse_the_dataset(self):
        cs = series()
        cs[3] = candle(3, v=0.0)
        ds = build_dataset(store_with(cs), SYM, TF, SRC, T0, END)
        self.assertEqual(ds.report.summary(), {"ZERO_VOLUME": 1})


class RefusedData(unittest.TestCase):
    def refused(self, cs, expect, **kw):
        with self.assertRaises(DatasetRejected) as cm:
            build_dataset(store_with(cs), SYM, TF, SRC, T0, END, **kw)
        self.assertIn(expect, kinds(cm.exception.report), cm.exception.report.summary())
        return cm.exception

    def test_an_empty_range_is_refused(self):
        with self.assertRaises(DatasetRejected) as cm:
            build_dataset(CandleStore(), SYM, TF, SRC, T0, END)
        self.assertEqual(kinds(cm.exception.report), {"EMPTY"})

    def test_a_missing_candle_is_refused_with_its_position_and_run_length(self):
        cs = [c for i, c in enumerate(series()) if i not in (5, 6, 7, 12)]
        e = self.refused(cs, "GAP")
        gaps = [f for f in e.report.errors if f.kind == "GAP"]
        self.assertEqual([(g.at, g.detail.split()[0]) for g in gaps], [(T0 + M5 * 5, "3"), (T0 + M5 * 12, "1")])

    def test_a_missing_candle_the_caller_explains_is_accepted_and_recorded(self):
        cs = [c for i, c in enumerate(series()) if i != 5]
        ds = build_dataset(store_with(cs), SYM, TF, SRC, T0, END, allowed_gaps=[T0 + M5 * 5])
        self.assertEqual(ds.report.summary(), {"ALLOWED_GAP": 1})
        self.assertEqual(ds.record()["allowed_gaps"], [(T0 + M5 * 5).isoformat()])

    def test_explaining_a_gap_that_does_not_exist_is_refused(self):
        self.refused(series(), "ALLOWED_GAP_PRESENT", allowed_gaps=[T0 + M5 * 5])

    def test_a_missing_first_or_last_candle_is_a_gap_too(self):
        self.refused(series()[1:], "GAP")
        self.refused(series()[:-1], "GAP")

    def test_high_or_low_that_do_not_bound_the_candle_are_refused(self):
        for bad in (candle(4, h=99.0), candle(4, l=101.0), candle(4, o=101.0, h=100.5)):
            cs = series()
            cs[4] = bad
            self.refused(cs, "BAD_OHLC")

    def test_non_finite_zero_or_negative_prices_and_bad_volume_are_refused(self):
        for bad, expect in ((candle(4, o=float("inf"), h=float("inf")), "BAD_PRICE"),
                            (candle(4, l=0.0), "BAD_PRICE"), (candle(4, o=-1.0), "BAD_PRICE"),
                            (candle(4, v=-1.0), "BAD_VOLUME")):
            cs = series()
            cs[4] = bad
            self.refused(cs, expect)

    def test_a_suspect_spike_is_refused_unless_that_exact_candle_is_acknowledged(self):
        cs = series()
        cs[10] = candle(10, o=100.0, h=121.0, l=99.5, c=120.0)                              # +20 % in one candle, then straight back
        self.refused(cs, "SUSPECT_SPIKE")
        both = [T0 + M5 * 10, T0 + M5 * 11]                                                # the jump up and the jump back
        ds = build_dataset(store_with(cs), SYM, TF, SRC, T0, END, acknowledged_spikes=both)
        self.assertTrue(ds.report.ok)
        for partial in ([T0 + M5 * 10], [T0 + M5 * 3]):                                    # acknowledging only part, or another candle, does not help
            with self.assertRaises(DatasetRejected):
                build_dataset(store_with(cs), SYM, TF, SRC, T0, END, acknowledged_spikes=partial)

    def test_a_jump_between_two_closes_is_a_spike_even_when_each_candle_is_calm(self):
        cs = series(10) + [candle(i, o=125.0, h=125.5, l=124.5, c=125.1) for i in range(10, 20)]    # level shift of +25 %
        self.refused(cs, "SUSPECT_SPIKE")

    def test_the_spike_limit_is_adjustable_and_must_be_positive(self):
        cs = series()
        cs[10] = candle(10, h=103.0, l=99.5, c=102.5)                                      # about 3 %
        build_dataset(store_with(cs), SYM, TF, SRC, T0, END)                                 # fine at the default 10 %
        self.refused(cs, "SUSPECT_SPIKE", spike_limit=0.02)
        with self.assertRaises(ValueError):
            build_dataset(store_with(cs), SYM, TF, SRC, T0, END, spike_limit=0)


class RawLists(unittest.TestCase):
    """validate_candles also guards data that did not come from the store (files, other providers)."""

    def v(self, cs, **kw):
        return validate_candles(cs, TF, T0, END, **kw)

    def test_duplicates_disorder_off_grid_naive_time_and_out_of_range_are_found(self):
        cs = series()
        self.assertIn("DUPLICATE", kinds(self.v(cs + [cs[3]])))
        swapped = list(cs)
        swapped[4], swapped[5] = swapped[5], swapped[4]
        self.assertIn("OUT_OF_ORDER", kinds(self.v(swapped)))
        off = list(cs)
        off[2] = Candle(t=T0 + M5 * 2 + timedelta(seconds=7), o=100, h=100.5, l=99.5, c=100.2, v=1)
        self.assertIn("OFF_GRID", kinds(self.v(off)))
        naive = list(cs)
        naive[2] = Candle(t=datetime(2026, 1, 5, 0, 10), o=100, h=100.5, l=99.5, c=100.2, v=1)
        self.assertIn("NAIVE_TIME", kinds(self.v(naive)))
        outside = cs + [candle(20)]
        self.assertIn("OUT_OF_RANGE", kinds(self.v(outside)))

    def test_nan_prices_and_volume_in_a_raw_list_are_found(self):                          # the store itself already refuses NaN
        for bad, expect in ((candle(4, c=float("nan")), "BAD_PRICE"), (candle(4, v=float("nan")), "BAD_VOLUME")):
            cs = series()
            cs[4] = bad
            self.assertIn(expect, kinds(self.v(cs)))

    def test_every_problem_is_reported_not_only_the_first(self):
        cs = series()
        cs[2] = candle(2, h=99.0)
        cs[9] = candle(9, v=-5.0)
        self.assertEqual(kinds(self.v(cs)), {"BAD_OHLC", "BAD_VOLUME"})

    def test_an_invalid_range_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            validate_candles(series(), TF, END, T0)
        with self.assertRaises(ValueError):
            validate_candles(series(), "7m", T0, END)


class PointInTime(unittest.TestCase):
    def test_a_correction_published_after_as_of_does_not_change_the_dataset(self):
        s = store_with(series())
        before = build_dataset(s, SYM, TF, SRC, T0, END, as_of=END)
        fixed = candle(8, c=100.3)
        s.put_candles([rec(fixed)], now=END + timedelta(hours=3))                           # a correction arrives later
        again = build_dataset(s, SYM, TF, SRC, T0, END, as_of=END)
        self.assertEqual(before.content_hash, again.content_hash)
        later = build_dataset(s, SYM, TF, SRC, T0, END, as_of=END + timedelta(hours=4))
        self.assertNotEqual(before.content_hash, later.content_hash)
        self.assertEqual(later.report.summary(), {"REVISED_CANDLES": 1})

    def test_candles_not_yet_closed_at_as_of_are_not_in_the_dataset(self):
        s = store_with(series())
        with self.assertRaises(DatasetRejected) as cm:                                     # as_of in the middle of the range
            build_dataset(s, SYM, TF, SRC, T0, END, as_of=T0 + M5 * 10)
        self.assertIn("GAP", kinds(cm.exception.report))

    def test_only_the_requested_source_is_used(self):
        s = store_with(series(), src="okx")
        with self.assertRaises(DatasetRejected):
            build_dataset(s, SYM, TF, "binance", T0, END)
        self.assertEqual(build_dataset(s, SYM, TF, "okx", T0, END).source, "okx")

    def test_low_confidence_candles_are_recorded(self):
        ds = build_dataset(store_with(series(), conf=0.5), SYM, TF, SRC, T0, END)
        self.assertEqual(ds.report.summary(), {"LOW_CONFIDENCE": 1})
        self.assertIn("20 candle(s)", ds.report.findings[0].detail)


if __name__ == "__main__":
    unittest.main()
