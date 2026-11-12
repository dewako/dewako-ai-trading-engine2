"""Hand-built 5m bars; VWAP and ATR are passed in directly so each rule is checked on its own."""
import numpy as np
import pandas as pd
import pytest

import harness as H


def frame(bars):
    df = pd.DataFrame(bars, columns=["o", "h", "l", "c"])
    df["v"] = 1.0
    df["open_ms"] = [i * H.STEP for i in range(len(df))]
    return df


def flat(n, px=100.0):
    return [(px, px + 0.2, px - 0.2, px)] * n


def test_v1_buys_a_2_atr_stretch_below_vwap_at_the_next_open_and_exits_when_price_closes_back_at_vwap():
    bars = flat(3) + [(100, 100, 97, 97.5), (97.6, 98, 97.4, 98), (98, 100.5, 98, 100.2), (100.1, 100.3, 99.9, 100)]
    df = frame(bars)
    vw, at = np.full(len(df), 100.0), np.full(len(df), 1.0)
    t = H.run(df, "V1", 0.0, 0.0, vw, at).trades
    assert [(x.side, x.entry, x.exit, x.reason) for x in t] == [(1, 97.6, 100.1, "back to VWAP")]


def test_v1_stop_is_1_5_atr_and_a_gap_through_it_fills_at_the_open():
    bars = flat(3) + [(100, 100, 97, 97.5), (97.6, 98, 95.9, 96), (96, 96, 96, 96)]
    df = frame(bars)
    vw, at = np.full(len(df), 100.0), np.full(len(df), 1.0)
    t = H.run(df, "V1", 0.0, 0.0, vw, at).trades[0]
    assert (t.exit, t.reason) == (pytest.approx(97.6 - 1.5), "stop")
    gap = frame(flat(3) + [(100, 100, 97, 97.5), (97.6, 98, 97.4, 97.8), (90, 91, 89, 90)])
    t = H.run(gap, "V1", 0.0, 0.0, vw[:6], at[:6]).trades[0]
    assert (t.exit, t.reason) == (90, "stop")


def test_v2_shorts_a_fresh_break_below_vwap_minus_atr_with_a_2_atr_target_and_fees_on_both_sides():
    bars = flat(3) + [(100, 100, 98.8, 98.9), (98.8, 98.9, 96.7, 96.8), (96.8, 97, 96.5, 96.6)]
    df = frame(bars)
    vw, at = np.full(len(df), 100.0), np.full(len(df), 1.0)
    t = H.run(df, "V2", 0.001, 0.0, vw, at).trades[0]
    assert (t.side, t.entry, t.exit, t.reason) == (-1, 98.8, pytest.approx(96.8), "target")
    assert t.pnl == pytest.approx(t.qty * (98.8 - 96.8) - 0.001 * t.qty * (98.8 + 96.8))


def test_v2_needs_a_fresh_cross_and_a_bar_touching_stop_and_target_counts_the_stop():
    stay = frame([(101.2, 101.4, 101.0, 101.2)] * 6)               # above the band on every close: never a fresh cross
    assert H.run(stay, "V2", 0, 0, np.full(6, 100.0), np.full(6, 1.0)).trades == []
    both = frame(flat(3) + [(100, 101.3, 100, 101.2), (101.2, 104, 99, 101)])
    t = H.run(both, "V2", 0, 0, np.full(5, 100.0), np.full(5, 1.0)).trades[0]
    assert t.reason == "stop" and t.exit == pytest.approx(100.2)


def test_time_stop_after_24_bars():
    bars = flat(3) + [(100, 100, 97, 97.5)] + [(97.6, 97.8, 97.4, 97.6)] * 30
    df = frame(bars)
    t = H.run(df, "V1", 0, 0, np.full(len(df), 100.0), np.full(len(df), 1.0)).trades[0]
    assert t.reason == "time" and (t.exit_ms - t.entry_ms) // H.STEP == 24
