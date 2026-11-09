"""Hand-built bars with known outcomes; small windows so each rule is checked in isolation."""
import pandas as pd
import pytest

import harness as H

P = dict(regime=5, entry=3, exit=2, atr_len=2, atr_mult=2.0)


def frame(closes, highs=None, lows=None, opens=None):
    n = len(closes)
    o = opens or [closes[max(i - 1, 0)] for i in range(n)]
    hi = highs or [max(o[i], closes[i]) + 0.5 for i in range(n)]
    lo = lows or [min(o[i], closes[i]) - 0.5 for i in range(n)]
    return pd.DataFrame({"open_ms": [i * 900_000 for i in range(n)], "o": o, "h": hi, "l": lo, "c": closes, "v": 1.0})


def test_long_breakout_above_the_average_fills_at_next_open_and_exits_on_the_exit_low_at_the_following_open():
    df = frame([100, 100, 100, 100, 100, 103, 104, 104.5, 105, 101, 100])
    r = H.run(df, "15m", P, allow_short=True, fee=0.0, cost_mult=0.0)
    t = r.trades[0]
    assert t.side == 1 and t.entry == 103 and t.reason == "exit rule"       # signal on bar 5's close, filled at bar 6's open (= 103)
    assert t.exit == 101                                                    # bar 9 closes below the 2-bar low -> filled at bar 10's open


def test_short_is_the_mirror_and_is_refused_when_long_only():
    df = frame([100, 100, 100, 100, 100, 97, 96, 95.5, 95, 99, 100])
    t = H.run(df, "15m", P, True, 0.0, 0.0).trades[0]
    assert t.side == -1 and t.entry == 97 and t.exit == 99 and t.pnl == pytest.approx(t.qty * (97 - 99))
    long_only = H.run(df, "15m", P, False, 0.0, 0.0).trades
    assert long_only and all(x.side == 1 for x in long_only)              # the short is never placed; the later rebound is a long


def test_stop_gap_fills_at_the_open_and_costs_are_charged_on_both_sides():
    closes = [100, 100, 100, 100, 100, 103, 104, 104]
    opens = [100, 100, 100, 100, 100, 100, 103, 80]                          # bar 7 opens far below the stop
    df = frame(closes, opens=opens, highs=[max(a, b) + 0.5 for a, b in zip(opens, closes)],
               lows=[min(a, b) - 0.5 for a, b in zip(opens, closes)])
    t = H.run(df, "15m", P, True, 0.0, 0.0).trades[0]
    assert (t.exit, t.reason) == (80, "stop (gap)")
    t2 = H.run(df, "15m", P, True, 0.001, 0.0).trades[0]
    assert t2.pnl == pytest.approx(t2.qty * (80 - 103) - 0.001 * t2.qty * (103 + 80))


def test_size_is_half_a_percent_risk_capped_at_ten_percent_of_equity():
    df = frame([100, 100, 100, 100, 100, 103, 104, 104.5, 105, 101, 100])
    t = H.run(df, "15m", P, True, 0.0, 0.0).trades[0]
    dist = 2.0 * (1.0 + 4.0) / 2                                             # ATR(2) over bars 4-5: true ranges 1 and 4
    assert t.qty == pytest.approx(min(0.005 * 10000 / dist, 0.10 * 10000 / 103))


def test_entry_fills_at_the_next_open_not_the_signal_close():
    closes = [100, 100, 100, 100, 100, 103, 104, 104.5, 105, 101, 100]
    opens = [100, 100, 100, 100, 100, 100, 103.7, 104, 104.5, 105, 100.6]
    df = frame(closes, opens=opens)
    t = H.run(df, "15m", P, True, 0.0, 0.0).trades[0]
    assert (t.entry, t.exit) == (103.7, 100.6)


def test_no_long_breakout_below_the_regime_average():
    closes = [200, 200, 200, 200, 200, 100, 100, 100, 101, 101]            # bar 8 breaks the 3-bar high (100.5) but the 5-bar average is 120.2
    df = frame(closes, opens=closes, highs=[x + 0.5 for x in closes], lows=[x - 0.5 for x in closes])
    assert H.indicators(df, P)["ehi"][8] == 100.5 and H.indicators(df, P)["sma"][8] == 120.2
    assert H.run(df, "15m", P, False, 0.0, 0.0).trades == []
