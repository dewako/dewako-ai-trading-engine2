"""Spot kline archive importer: accepts real-format files, refuses anything suspect, never repairs or guesses."""
import hashlib
import io
import os
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.market_data.archive_import import SOURCE, import_archive, parse_csv  # noqa: E402
from dewako.market_data.store import CandleStore  # noqa: E402

U = timezone.utc
STEP = 300_000


def month_csv(year=2024, month=3, n=12, unit="ms", header=False, mutate=None, start_ms=None):
    t0 = int(datetime(year, month, 1, tzinfo=U).timestamp() * 1000) if start_ms is None else start_ms
    mult = 1000 if unit == "us" else 1
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tq,ignore"] if header else []
    for i in range(n):
        t = t0 + i * STEP
        row = [t * mult, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 5.0, (t + STEP - 1) * mult, 500.0, 10, 2.0, 200.0, 0]
        if mutate:
            res = mutate(i, row)
            row = res if isinstance(res, list) else row                                   # a mutator edits in place or returns a replacement row
        lines.append(",".join(str(x) for x in row))
    return ("\n".join(lines) + "\n").encode()


def archive(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return str(path)


def inner_zip(csv_name, data):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(csv_name, data)
    return b.getvalue()


def run(tmp_path, members):
    st = CandleStore(":memory:")
    return import_archive(archive(tmp_path / "a.zip", members), st, now=datetime(2026, 11, 1, tzinfo=U)), st


def test_a_clean_month_in_milliseconds_imports_with_the_exchange_symbol_normalised(tmp_path):
    rep, st = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv()})
    f = rep.files[0]
    assert (f.refused, f.rows, f.unit, f.symbol, f.imported_new) == (None, 12, "ms", "BTC/USDT", 12)
    got = st.get_candles("BTC/USDT", "5m", SOURCE, datetime(2024, 3, 1, tzinfo=U), datetime(2024, 3, 2, tzinfo=U))
    assert len(got) == 12 and got[0].candle.o == 100.0 and got[0].quality.source == SOURCE


def test_microsecond_timestamps_give_the_same_candles_as_milliseconds(tmp_path):
    a, sa = run(tmp_path, {"BTCUSDT-5m-2025-03.csv": month_csv(2025, 3, unit="ms")})
    b, sb = run(tmp_path, {"BTCUSDT-5m-2025-03.csv": month_csv(2025, 3, unit="us")})
    assert (a.files[0].unit, b.files[0].unit) == ("ms", "us")
    w = (datetime(2025, 3, 1, tzinfo=U), datetime(2025, 3, 2, tzinfo=U))
    assert [r.candle for r in sa.get_candles("BTC/USDT", "5m", SOURCE, *w)] == [r.candle for r in sb.get_candles("BTC/USDT", "5m", SOURCE, *w)]


def test_a_header_line_is_skipped(tmp_path):
    rep, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(header=True)})
    assert rep.files[0].rows == 12 and not rep.files[0].refused


def test_a_publication_time_is_the_close_not_the_open(tmp_path):
    _, st = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(n=2)})
    r = st.get_candles("BTC/USDT", "5m", SOURCE, datetime(2024, 3, 1, tzinfo=U), datetime(2024, 3, 2, tzinfo=U))[0]
    assert (r.quality.publication_timestamp - r.quality.data_timestamp).total_seconds() == 300


def test_delivery_futures_files_are_refused_and_nothing_is_stored(tmp_path):
    rep, st = run(tmp_path, {"BTCUSD_250328-5m-2024-10.csv": month_csv(2024, 10)})
    assert "not spot" in rep.files[0].refused and not rep.ok
    assert st.get_candles("BTC/USD", "5m", SOURCE, datetime(2024, 1, 1, tzinfo=U), datetime(2025, 1, 1, tzinfo=U)) == []


def test_a_file_with_one_bad_row_is_refused_whole_not_partly_imported(tmp_path):
    def bad(i, r):
        if i == 7:
            r[2] = 50.0                                                                    # high below open/close
    rep, st = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(mutate=bad)})
    assert "line 8" in rep.files[0].refused and "high/low" in rep.files[0].refused
    assert st.get_candles("BTC/USDT", "5m", SOURCE, datetime(2024, 3, 1, tzinfo=U), datetime(2024, 3, 2, tzinfo=U)) == []


def test_every_kind_of_malformed_row_is_refused_with_its_line(tmp_path):
    cases = {
        "non-numeric": (lambda i, r: r.__setitem__(1, "abc") if i == 2 else None, "line 3: a field is not a number"),
        "zero price": (lambda i, r: r.__setitem__(3, 0.0) if i == 2 else None, "not positive and finite"),
        "negative volume": (lambda i, r: r.__setitem__(5, -1.0) if i == 2 else None, "not positive and finite"),
        "nan": (lambda i, r: r.__setitem__(4, "nan") if i == 2 else None, "not positive and finite"),
        "off grid": (lambda i, r: (r.__setitem__(0, r[0] + 1), r.__setitem__(6, r[6] + 1)) if i == 2 else None, "not on the 5m grid"),
        "bad close time": (lambda i, r: r.__setitem__(6, r[6] + 5) if i == 2 else None, "close time"),
        "duplicate": (lambda i, r: (r.__setitem__(0, r[0] - STEP), r.__setitem__(6, r[6] - STEP)) if i == 2 else None, "duplicate"),
        "out of order": (lambda i, r: (r.__setitem__(0, r[0] - 3 * STEP), r.__setitem__(6, r[6] - 3 * STEP)) if i == 4 else None, "out of order"),
        "short row": (lambda i, r: r[:5] if i == 2 else None, "columns"),
    }
    for label, (mut, expect) in cases.items():
        rep, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(mutate=mut)})
        assert rep.files[0].refused and expect in rep.files[0].refused, (label, rep.files[0].refused)


def test_a_candle_outside_the_month_named_in_the_file_is_refused(tmp_path):
    rep, _ = run(tmp_path, {"BTCUSDT-5m-2024-04.csv": month_csv(2024, 3)})
    assert "outside 2024-04" in rep.files[0].refused


def test_a_name_that_is_not_a_spot_archive_name_is_refused(tmp_path):
    rep, _ = run(tmp_path, {"prices.csv": month_csv()})
    assert "not a Binance spot kline archive name" in rep.files[0].refused


def test_a_unit_change_inside_a_file_is_refused(tmp_path):
    def flip(i, r):
        if i >= 6:
            r[0], r[6] = r[0] * 1000, r[6] * 1000
    rep, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(mutate=flip)})
    assert "unit changes" in rep.files[0].refused


def test_checksums_are_verified_and_a_mismatch_refuses_the_file(tmp_path):
    z = inner_zip("BTCUSDT-5m-2024-03.csv", month_csv())
    good = hashlib.sha256(z).hexdigest()
    ok, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.zip": z, "BTCUSDT-5m-2024-03.zip.CHECKSUM": f"{good}  BTCUSDT-5m-2024-03.zip\n"})
    assert ok.files[0].checksum == "OK" and not ok.files[0].refused and ok.ok
    bad, st = run(tmp_path, {"BTCUSDT-5m-2024-03.zip": z, "BTCUSDT-5m-2024-03.zip.CHECKSUM": f"{'0' * 64}  BTCUSDT-5m-2024-03.zip\n"})
    assert bad.files[0].checksum == "MISMATCH" and "corrupted or altered" in bad.files[0].refused and not bad.ok
    assert st.get_candles("BTC/USDT", "5m", SOURCE, datetime(2024, 3, 1, tzinfo=U), datetime(2024, 3, 2, tzinfo=U)) == []


def test_a_file_without_a_checksum_is_imported_but_marked_unverified(tmp_path):
    z = inner_zip("BTCUSDT-5m-2024-03.csv", month_csv())
    missing, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.zip": z})
    bare, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv()})
    assert missing.files[0].checksum == "MISSING" and bare.files[0].checksum == "NOT_APPLICABLE"
    for r in (missing, bare):
        assert not r.files[0].refused and any("UNVERIFIED" in n for n in r.files[0].notes)


def test_a_missing_day_between_months_is_reported_as_a_gap_and_never_filled(tmp_path):
    feb = month_csv(2024, 2, n=2)
    mar_start = int(datetime(2024, 3, 1, tzinfo=U).timestamp() * 1000)
    mar = month_csv(2024, 3, n=2, start_ms=mar_start + 3 * STEP)                           # three candles missing right after the month turns
    rep, st = run(tmp_path, {"BTCUSDT-5m-2024-02.csv": feb, "BTCUSDT-5m-2024-03.csv": mar})
    g = rep.gaps["BTC/USDT|5m"]
    assert len(g) == 1 and g[0]["missing"] > 3
    assert len(st.get_candles("BTC/USDT", "5m", SOURCE, datetime(2024, 1, 1, tzinfo=U), datetime(2024, 4, 1, tzinfo=U))) == 4


def test_zero_volume_candles_are_counted_and_reported(tmp_path):
    rep, _ = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(mutate=lambda i, r: r.__setitem__(5, 0.0) if i in (1, 2) else None)})
    assert rep.files[0].zero_volume == 2 and any("2 zero-volume" in n for n in rep.files[0].notes)


def test_importing_the_same_archive_twice_changes_nothing(tmp_path):
    members = {"BTCUSDT-5m-2024-03.csv": month_csv()}
    st = CandleStore(":memory:")
    p = archive(tmp_path / "a.zip", members)
    first = import_archive(p, st, now=datetime(2026, 11, 1, tzinfo=U))
    second = import_archive(p, st, now=datetime(2026, 11, 2, tzinfo=U))
    assert (first.files[0].imported_new, second.files[0].imported_new) == (12, 0)
    assert not second.files[0].notes or all("revision" not in n for n in second.files[0].notes)


def test_a_changed_candle_on_reimport_is_kept_as_a_revision_and_flagged(tmp_path):
    st = CandleStore(":memory:")
    import_archive(archive(tmp_path / "a.zip", {"BTCUSDT-5m-2024-03.csv": month_csv()}), st, now=datetime(2026, 11, 1, tzinfo=U))
    rep = import_archive(archive(tmp_path / "b.zip", {"BTCUSDT-5m-2024-03.csv": month_csv(mutate=lambda i, r: r.__setitem__(4, 100.6 + i) if i == 3 else None)}),
                         st, now=datetime(2026, 11, 2, tzinfo=U))
    assert any("revisions" in n for n in rep.files[0].notes)


def test_parse_csv_reports_the_first_problem_it_finds():
    try:
        parse_csv(b"", "x.csv", "5m", 2024, 3)
    except ValueError as e:
        assert "no candles" in str(e)
    else:
        raise AssertionError("an empty file must be refused")


def test_an_imported_series_passes_through_the_dataset_validator(tmp_path):
    from dewako.backtesting.datasets import build_dataset
    _, st = run(tmp_path, {"BTCUSDT-5m-2024-03.csv": month_csv(n=100)})
    d = build_dataset(st, "BTC/USDT", "5m", SOURCE, datetime(2024, 3, 1, tzinfo=U), datetime(2024, 3, 1, 8, 20, tzinfo=U))
    assert len(d.candles) == 100
