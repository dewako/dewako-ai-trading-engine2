"""Hand-built 15m days with known Pine outcomes (n_days=2 so each case needs only three days)."""
import pandas as pd
import pytest

import harness as H

DAY0 = int(pd.Timestamp("2024-01-10 00:00", tz="America/New_York").timestamp() * 1000)
P2 = {"n_days": 2}


def slot_index(hhmm):
    return (hhmm // 100) * 4 + (hhmm % 100) // 15


def days(*specs):
    """Each spec is {hhmm: close} for one NY day; every other bar is flat at 100. The 09:30 bar opens at 100."""
    rows = []
    for d, spec in enumerate(specs):
        for i in range(96):
            hhmm = (i // 4) * 100 + (i % 4) * 15
            c = spec.get(hhmm, 100.0)
            rows.append((DAY0 + d * 86_400_000 + i * H.STEP_MS, 100.0, max(100.0, c), min(100.0, c), c, 1.0))
    return H.add_clock(pd.DataFrame(rows, columns=["open_ms", "o", "h", "l", "c", "v"]))


def test_long_on_close_above_band_fills_at_that_close_and_is_flattened_at_the_1545_bar_with_both_fees():
    df = days({930: 101}, {930: 101}, {930: 102, 1545: 104})
    r = H.run(df, allow_short=True, fee=0.001, slip=0.0, sizing="A", p=P2)
    assert len(r.trades) == 1
    t = r.trades[0]
    assert (t.side, t.entry, t.exit, t.qty, t.reason) == (1, 102, 104, 1.0, "RTH close")
    assert t.pnl == pytest.approx(2 - 0.001 * (102 + 104))
    assert r.equity[-1] == pytest.approx(100000 + t.pnl)


def test_a_slot_needs_n_full_prior_days_before_it_trades():
    df = days({930: 101}, {930: 150})                            # day 2 has only 1 prior value for 09:30
    assert H.run(df, True, 0, 0, "A", P2).trades == []


def test_band_uses_only_prior_days_not_today():
    df = days({930: 105}, {930: 101}, {930: 102})                # prior sigma 3 -> upper 103 -> no trade
    assert H.run(df, True, 0, 0, "A", P2).trades == []           # (with today pushed first, sigma 1.5 -> would trade)


def test_opposite_break_flips_and_long_only_just_closes():
    df = days({930: 101, 945: 99}, {930: 101, 945: 99}, {930: 102, 945: 98})
    both = H.run(df, True, 0, 0, "A", P2).trades
    assert [(t.side, t.entry, t.exit, t.reason) for t in both] == [(1, 102, 98, "flip"), (-1, 98, 100, "RTH close")]
    long_only = H.run(df, False, 0, 0, "A", P2).trades
    assert [(t.side, t.entry, t.exit, t.reason) for t in long_only] == [(1, 102, 98, "flip")]


def test_no_entry_on_the_bar_ending_at_1600_and_slippage_is_against_us():
    df = days({1545: 101}, {1545: 101}, {1545: 110})
    assert H.run(df, True, 0, 0, "A", P2).trades == []
    df = days({930: 101}, {930: 101}, {930: 102})
    t = H.run(df, True, 0.0, 0.01, "A", P2).trades[0]
    assert t.entry == pytest.approx(102 * 1.01) and t.exit == pytest.approx(100 * 0.99)


def test_engine_comparable_sizing_is_ten_percent_of_equity():
    df = days({930: 101}, {930: 101}, {930: 102})
    t = H.run(df, True, 0, 0, "B", P2).trades[0]
    assert t.qty == pytest.approx(0.10 * 10000 / 102)


def test_on_5m_bars_the_1555_bar_is_the_flatten_bar_and_the_band_slot_is_per_5_minutes():
    rows = []
    for d, c930, c1555 in ((0, 101, 100), (1, 101, 100), (2, 102, 103)):
        for i in range(288):
            hhmm = (i // 12) * 100 + (i % 12) * 5
            c = {930: c930, 1555: c1555}.get(hhmm, 100.0)
            rows.append((DAY0 + d * 86_400_000 + i * 300_000, 100.0, max(100.0, c), min(100.0, c), c, 1.0))
    df = H.add_clock(pd.DataFrame(rows, columns=["open_ms", "o", "h", "l", "c", "v"]), 300_000)
    t = H.run(df, True, 0, 0, "B", P2).trades
    assert [(x.side, x.entry, x.exit, x.reason) for x in t] == [(1, 102, 103, "RTH close")]
