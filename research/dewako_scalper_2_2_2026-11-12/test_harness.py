"""Hand-built bars. Trade-tracker rules are checked with injected signals; indicator and filter rules on their own."""
import numpy as np
import pandas as pd
import pytest

import harness as H

P15 = "15m / 30m"


def frame(bars, step=15 * H.MIN, t0=12 * 3_600_000):
    df = pd.DataFrame(bars, columns=["o", "h", "l", "c"])
    df["v"] = 1.0
    df["open_ms"] = [t0 + i * step for i in range(len(df))]
    return df


def inject(n, at, d=1, risk=1.0, kind="PB", st_line=None, st_dir=None):
    sig = np.zeros(n, dtype=int)
    sig[at] = d
    typ = np.array([""] * n, dtype=object)
    typ[at] = kind
    return dict(sig=sig, typ=typ, risk=np.full(n, risk), st_line=np.full(n, np.nan) if st_line is None else st_line,
                st_dir=np.zeros(n, dtype=int) if st_dir is None else st_dir, raw=sig)


def test_next_open_fill_sets_stop_and_targets_from_the_fill_and_manages_that_same_bar():
    df = frame([(100, 100.5, 99.5, 100), (101, 101.2, 99.9, 100.5), (100.5, 100.6, 100.4, 100.5)])
    r = H.simulate(df, inject(3, 0), P15, 0.0, 0.0, fund=0)
    t = r.trades[0]
    assert (t.entry, t.exit, t.reason) == (101, 100, "stopped out")         # stop = 101 - 1, hit inside the fill bar
    r = H.simulate(df, inject(3, 0), P15, 0.0, 0.0, fill="close", fund=0)
    assert r.trades[0].entry == 100 and r.trades[0].reason == "end of data"  # close fill at 100: stop 99, never hit


def test_a_gap_through_the_stop_fills_at_the_open_and_stop_beats_tp4_on_the_same_bar():
    df = frame([(100, 100.5, 99.5, 100), (100, 100.2, 99.8, 100), (97, 97.5, 96.5, 97)])
    t = H.simulate(df, inject(3, 0), P15, 0.0, 0.0, fund=0).trades[0]
    assert (t.exit, t.reason) == (97, "gap through stop")
    both = frame([(100, 100.5, 99.5, 100), (100, 100.2, 99.8, 100), (100, 107, 98, 100)])
    t = H.simulate(both, inject(3, 0), P15, 0.0, 0.0, fund=0).trades[0]
    assert (t.exit, t.reason) == (99, "stopped out")


def test_tp4_exit_and_r_and_fees_on_both_sides():
    df = frame([(100, 100.5, 99.5, 100), (100, 100.2, 99.8, 100), (100, 106.5, 99.9, 106)])
    t = H.simulate(df, inject(3, 0), P15, 0.001, 0.0, fund=0).trades[0]
    assert (t.exit, t.reason) == (106, "all targets hit")
    assert t.qty == pytest.approx(min(H.CASH * H.RISK / 1.0, H.CASH * H.MAX_LEV / 100))   # 1% risk = 100 units (the 10x cap of 1,000 does not bind)
    assert t.pnl == pytest.approx(t.qty * 6 - 0.001 * t.qty * (100 + 106))
    tight = H.simulate(df, inject(3, 0, risk=0.05), P15, 0.0, 0.0, fund=0).trades[0]
    assert tight.qty == pytest.approx(H.CASH * H.MAX_LEV / 100)             # 1% risk would be 2,000 units: capped at 10x = 1,000


def test_break_even_after_tp1_applies_from_the_next_bar_and_only_in_the_15m_preset():
    bars = [(100, 100.5, 99.5, 100), (100, 102.2, 99.8, 101), (101, 101.2, 99.5, 100)]
    df = frame(bars + [(100, 100.1, 99.9, 100)])
    t = H.simulate(df, inject(4, 0), P15, 0.0, 0.0, fund=0).trades[0]
    assert (t.exit, t.reason) == (100, "trail / break-even")                 # TP1 102 touched, then back to entry
    t = H.simulate(df, inject(4, 0), "3m / 5m", 0.0, 0.0, fund=0).trades[0]
    assert t.reason == "end of data"                                         # no break-even: stop stays at 99
    same_bar = frame([(100, 100.5, 99.5, 100), (100, 102.2, 99.0, 101), (101, 101.1, 100.9, 101)])
    t = H.simulate(same_bar, inject(3, 0), P15, 0.0, 0.0, fund=0).trades[0]
    assert t.reason == "stopped out" and t.exit == 99                        # TP1 and stop on one bar: the stop counts


def test_supertrend_trail_after_tp1_only_tightens_and_only_while_the_trend_agrees():
    df = frame([(100, 100.5, 99.5, 100), (100, 102.5, 99.9, 102), (102, 102.4, 101.6, 102), (102, 102.3, 101.3, 102),
                (102, 102.1, 101.0, 101.2)])
    line = np.array([np.nan, 101.5, 101.0, 101.0, 101.8])                    # 101.0 would loosen the 101.5 stop: ignored
    d = np.array([0, 1, 1, 1, 1])
    t = H.simulate(df, inject(5, 0, st_line=line, st_dir=d), P15, 0.0, 0.0, fund=0).trades[0]
    assert (t.exit, t.reason, t.exit_ms) == (pytest.approx(101.5), "trail / break-even", df.open_ms[3])   # low 101.3 hits 101.5
    t = H.simulate(df, inject(5, 0, st_line=line, st_dir=-d), P15, 0.0, 0.0, fund=0).trades[0]
    assert t.reason == "end of data"                                        # trend disagrees: stop stays at break-even 100


def test_short_side_and_funding_paid_by_longs_received_by_shorts():
    bars = [(100, 100.5, 99.5, 100)] + [(100, 100.3, 99.7, 100)] * 40
    df = frame(bars, t0=0)                                                   # 15m bars from 00:00: funding at 00:00 and 08:00
    t = H.simulate(df, inject(41, 0, d=-1), P15, 0.0, 0.0).trades[0]
    assert t.side == -1 and t.funding == pytest.approx(H.FUND * t.qty * 100)  # one funding time (08:00) while open
    t = H.simulate(df, inject(41, 0, d=1), P15, 0.0, 0.0).trades[0]
    assert t.funding == pytest.approx(-H.FUND * t.qty * 100)


def test_no_reentry_on_the_exit_bar_unless_a_supertrend_flip():
    df = frame([(100, 100.5, 99.5, 100), (100, 100.2, 98.5, 99), (99, 99.5, 98.5, 99), (99, 99.2, 98.8, 99)])
    s = inject(4, 0)
    s["sig"][1], s["typ"][1] = 1, "PB"
    t = H.simulate(df, s, P15, 0, 0, fund=0).trades
    assert len(t) == 1 and t[0].reason == "stopped out"                     # stopped on bar 1; bar 1's PB signal is ignored
    s["typ"][1] = "ST"
    assert len(H.simulate(df, s, P15, 0, 0, fund=0).trades) == 2


def test_supertrend_turns_up_in_a_rally_and_down_in_a_selloff():
    c = np.r_[np.linspace(100, 80, 60), np.linspace(80, 120, 60), np.linspace(120, 90, 60)]
    _, d = H.supertrend(c + 0.3, c - 0.3, c, 3.0, 10)
    assert d[55] == 1 and d[110] == -1 and d[175] == 1                      # Pine: -1 = up


def test_rma_and_ema_match_pine_definitions():
    x = np.array([1.0, 2, 3, 4, 5, 6])
    r = H.rma(x, 3)
    assert np.isnan(r[1]) and r[2] == pytest.approx(2.0) and r[3] == pytest.approx(2 + (4 - 2) / 3)
    e = H.ema(x, 3)
    assert e[0] == 1 and e[1] == pytest.approx(1 + 0.5 * (2 - 1))


def test_session_and_fee_filters_block_signals():
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0.05, 0.4, 3000))
    df = frame([(x, x + 0.3, x - 0.3, x) for x in c], t0=0)
    s = H.signals(df, P15)
    hr = (df.open_ms.to_numpy() // 3_600_000) % 24
    assert (s["raw"] != 0).sum() > (s["sig"] != 0).sum() > 0
    assert not ((s["sig"] != 0) & ((hr < 12) | (hr >= 21))).any()
    with np.errstate(invalid="ignore"):
        fr = H.FEE_FILTER_PCT / 100 * df.c.to_numpy() / s["risk"]
    assert not ((s["sig"] != 0) & (fr > 0.25)).any()
    assert ((s["raw"] != 0) & (hr >= 12) & (hr < 21) & (fr <= 0.25)).sum() == (s["sig"] != 0).sum()
