"""Hand-built bars with known Pine outcomes. Indicators are set directly so each rule is checked in isolation."""
import pandas as pd
import pytest

import harness as H

NY_MIDNIGHT = int(pd.Timestamp("2024-01-10 00:00", tz="America/New_York").timestamp() * 1000)


def frame(bars, step_min=15, start=None, atr=10.0, adx=30.0, up=True):
    """bars: list of (o, h, l, c) starting at `start` (default NY 00:00), one per step."""
    t0 = NY_MIDNIGHT if start is None else start
    df = pd.DataFrame(bars, columns=["o", "h", "l", "c"])
    df["v"] = 1.0
    df["open_ms"] = [t0 + i * step_min * 60_000 for i in range(len(df))]
    ny = pd.to_datetime(df.open_ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
    df["hour"], df["day"] = ny.dt.hour, ny.dt.day
    df["atr"], df["adx"] = atr, adx
    df["ema_trend"] = 1.0 if up else 1e9
    df["ema_fast"] = 2.0 if up else 0.5e9
    return df


def flat_until_3am(price=100.0):
    return [(price, price + 1, price - 1, price)] * 12          # 00:00-02:45 NY: Asia range 99..101


def test_entry_fills_at_next_open_and_stop_exit_carries_both_fees_and_slippage():
    bars = flat_until_3am() + [(101, 103, 101, 102.5),           # 03:00 close > 101 + 1 buffer -> stop order at 102 armed
                               (103, 104, 102.9, 103.5),         # fills at open 103 (gap through the stop)
                               (103.5, 103.6, 80, 85)]           # stop 103 - 15 = 88 hit
    r = H.run(frame(bars), allow_short=False, fee=0.001, slip=0.0)
    assert len(r.trades) == 1
    t = r.trades[0]
    assert (t.entry, t.exit, t.reason) == (103, 88, "stop")
    qty = 0.10 * 10000 / 102.5
    assert t.qty == pytest.approx(qty)
    assert t.pnl == pytest.approx(qty * (88 - 103) - 0.001 * qty * (103 + 88))
    assert r.equity[-1] == pytest.approx(10000 + t.pnl)
    r2 = H.run(frame(bars), allow_short=False, fee=0.0, slip=0.01)
    assert r2.trades[0].entry == pytest.approx(103 * 1.01) and r2.trades[0].exit == pytest.approx((103 * 1.01 - 15) * 0.99)   # stop is from the slipped avg price


def test_no_exit_on_the_entry_bar_itself():
    bars = flat_until_3am() + [(101, 103, 101, 102.5), (103, 104, 50, 103.5), (103.5, 104, 103, 103.8)]
    r = H.run(frame(bars), allow_short=False, fee=0.0, slip=0.0)
    assert r.trades == []                                         # the 50 low is on the fill bar; exits were not placed yet


def test_bar_touching_both_uses_the_nearer_extreme_first():
    base = flat_until_3am() + [(101, 103, 101, 102.5), (103, 104, 102.9, 103.5)]
    tgt_first = base + [(103.5, 126, 80, 100)]                    # high is nearer the open -> target 103 + 22 = 125
    stop_first = base + [(103.5, 126, 101, 100)]                  # with low 101 the low is nearer, but 101 > stop 88
    stop_first2 = base + [(103.5, 140, 87, 100)]                  # low nearer -> stop 88 first
    assert H.run(frame(tgt_first), False, 0, 0).trades[0].reason == "target"
    assert H.run(frame(stop_first), False, 0, 0).trades[0].reason == "target"
    assert H.run(frame(stop_first2), False, 0, 0).trades[0].reason == "stop"


def test_breakeven_is_not_latched():
    base = flat_until_3am() + [(101, 103, 101, 102.5), (103, 104, 102.9, 103.5),
                               (103.5, 114, 103.5, 114)]          # close > 103 + 10 -> stop raised to 106
    keep = base + [(114, 114, 105, 105)]                          # low 105 < 106 -> breakeven stop fills
    r = H.run(frame(keep), False, 0, 0)
    assert (r.trades[0].exit, r.trades[0].reason) == (106, "stop")
    drop = base + [(114, 114, 108, 108),                          # close 108 < 113: stop goes back to 88
                   (108, 108, 95, 96)]                            # 95 would have hit 106, but the stop is 88 again
    assert H.run(frame(drop), False, 0, 0).trades == []


def test_asia_range_starts_at_ny_midnight_not_20h():
    start = NY_MIDNIGHT - 4 * 3600_000                            # 20:00 the evening before
    evening = [(100, 150, 99, 100)] * 16                          # 20:00-23:45: a 150 high that Pine's reset throws away
    bars = evening + flat_until_3am() + [(101, 103, 101, 102.5), (103, 104, 102.9, 103.5)]
    r = H.run(frame(bars, start=start), False, 0, 0)
    assert r.equity[-1] != 10000                                  # a long opened off the 101 range, not the 150 one


def test_one_long_per_ny_day_and_shorts_only_when_allowed():
    day = flat_until_3am() + [(101, 103, 101, 102.5), (103, 104, 102.9, 103.5), (103.5, 103.6, 80, 85),
                              (85, 104, 85, 103), (103, 106, 103, 105), (105, 106, 104, 105)]
    assert len(H.run(frame(day), False, 0, 0).trades) == 1
    down = flat_until_3am() + [(99, 99, 97, 97.5), (97, 97.1, 96, 96.5), (96.5, 120, 96, 118)]
    assert H.run(frame(down, up=False), False, 0, 0).trades == []
    t = H.run(frame(down, up=False), True, 0, 0).trades
    assert len(t) == 1 and t[0].side == -1 and t[0].entry == 97 and t[0].exit == 112 and t[0].reason == "stop"


def test_low_adx_cancels_a_pending_entry_and_the_day_stays_spent():
    bars = flat_until_3am() + [(101, 102.5, 101, 102.5),           # armed: stop 102
                               (101.5, 101.9, 101.2, 101.5),     # does not reach 102; closes with ADX 10 -> cancelled
                               (101.5, 110, 101.5, 109),         # would have filled; armed flag still set, so no new order
                               (109, 111, 108, 110)]
    df = frame(bars)
    df.loc[13, "adx"] = 10
    r = H.run(df, False, 0, 0)
    assert r.trades == [] and r.equity[-1] == 10000
    df.loc[13, "adx"] = 30                                        # control: without the cancel the order fills on bar 14
    assert H.run(df, False, 0, 0).equity[-1] != 10000
