"""Stage 3A step 3: the replay runs the REAL pipeline on simulated time and is pessimistic about timing.

A probe strategy buys / sells at chosen bars so each rule of the replay can be checked against exact numbers. Nothing contacts an exchange.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from dewako.backtesting.datasets import build_dataset  # noqa: E402
from dewako.backtesting.replay import ReplayConfig, replay  # noqa: E402
from dewako.backtesting.sim_adapter import SimConfig  # noqa: E402
from dewako.core import timesource  # noqa: E402
from dewako.core.types import Candle, DataQuality, Side, Signal  # noqa: E402
from dewako.market_data.models import CandleRecord  # noqa: E402
from dewako.market_data.store import CandleStore  # noqa: E402
from dewako.persistence.db import Database  # noqa: E402
from dewako.strategies import base  # noqa: E402

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
M5 = timedelta(minutes=5)
SYM, TF, SRC = "BTC/USDT", "5m", "binance"
BIG = 1e9


def bar(i, o=100.0, h=None, l=None, c=None, v=BIG):   # noqa: E741
    c = o if c is None else c
    return Candle(t=T0 + M5 * i, o=o, h=max(o, c) if h is None else h, l=min(o, c) if l is None else l, c=c, v=v)


def dataset(candles):
    store = CandleStore()
    store.put_candles([CandleRecord(symbol=SYM, timeframe=TF, candle=c, quality=DataQuality(
        source=SRC, data_timestamp=c.t, publication_timestamp=c.t + M5, ingested_at=c.t + M5)) for c in candles], now=T0 + timedelta(days=2))
    return build_dataset(store, SYM, TF, SRC, candles[0].t, candles[-1].t + M5)


def make_probe(name, buy_at, stop_pct=0.02, tp_pct=0.03, sell_at=None):
    """BUY once when exactly `buy_at` candles are closed; optionally SELL (manage) when `sell_at` are closed."""
    seen = []

    class Probe(base.Strategy):
        supported_timeframes = ["5m"]
        default_parameters = {}
        seen_log = seen

        def generate_signal(self, state):
            seen.append((len(state.candles), state.candles[-1].t, state.as_of))
            if len(state.candles) != buy_at:
                return None
            c = state.candles[-1].c
            return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name, strategy_version=self.version,
                          reason=["probe"], entry_price=c, stop_price=c * (1 - stop_pct), take_profit_price=c * (1 + tp_pct) if tp_pct else None)

        def manage_position(self, state, position):
            seen.append((len(state.candles), state.candles[-1].t, state.as_of))
            if sell_at is not None and len(state.candles) == sell_at:
                c = state.candles[-1].c
                return Signal(symbol=state.symbol, side=Side.SELL, confidence=0.9, strategy=self.name, strategy_version=self.version,
                              reason=["probe exit"], entry_price=c)
            return None

    Probe.name, Probe.version = name, "1.0"
    base.register(Probe)
    return Probe


CLEAN = dict(fee_rate=0.0, min_half_spread=0.0001, spread_range_factor=0.0, slippage_base=0.0, slippage_range_factor=0.0, impact_k=0.0,
             max_participation=1.0, min_notional=1.0)


def cfg(name, **kw):
    sim = SimConfig(**{**CLEAN, **kw.pop("sim", {})})
    kw.setdefault("state_source", "candles_only")                    # the probes are tiny plumbing scenarios (under 60 candles)
    return ReplayConfig(strategy=name, version="1.0", symbol=SYM, timeframe=TF, sim=sim, **kw)


class RealPipeline(unittest.TestCase):
    def test_a_probe_buy_goes_through_risk_and_execution_and_fills_at_the_next_open_not_the_close_it_saw(self):
        make_probe("Probe Next Open", buy_at=5)
        candles = [bar(i) for i in range(5)] + [bar(5, o=100.5)] + [bar(i) for i in range(6, 9)]
        r = replay(dataset(candles), cfg("Probe Next Open"))
        self.assertTrue(r.simulated)
        buys = [f for f in r.fills if f.side == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].bar_time, T0 + M5 * 5)                                    # the bar after the signal bar
        self.assertEqual(buys[0].at, T0 + M5 * 5)
        self.assertAlmostEqual(buys[0].price, 100.5 * 1.0001, places=9)                    # the open of that bar plus spread, not 100
        self.assertEqual(r.ticks.get("EXECUTED"), 1)
        self.assertEqual(r.incidents, 0)

    def test_the_strategy_only_ever_sees_candles_that_are_closed_at_the_moment_it_decides(self):
        probe = make_probe("Probe Closed Only", buy_at=999)
        candles = [bar(i, o=100 + i) for i in range(8)]
        replay(dataset(candles), cfg("Probe Closed Only"))
        self.assertEqual(len(probe.seen_log), 7)                                           # one decision per bar after the first
        for n, last_t, as_of in probe.seen_log:
            self.assertEqual(last_t + M5, as_of)                                           # the newest candle closes exactly now
            self.assertEqual(last_t, T0 + M5 * (n - 1))                                    # and nothing after it is visible
        self.assertEqual([n for n, _, _ in probe.seen_log], [1, 2, 3, 4, 5, 6, 7])

    def test_the_result_says_it_is_simulated_and_records_the_data_and_settings_it_used(self):
        make_probe("Probe Record", buy_at=999)
        ds = dataset([bar(i) for i in range(6)])
        r = replay(ds, cfg("Probe Record"))
        self.assertTrue(r.simulated)
        self.assertEqual(r.dataset["content_hash"], ds.content_hash)
        self.assertEqual((r.config["strategy"], r.config["sim"]["fee_rate"], r.bars), ("Probe Record", 0.0, 6))
        self.assertEqual(r.config["quote_guard"]["max_spread_pct"], 0.5)

    def test_a_dataset_for_another_symbol_or_timeframe_is_refused(self):
        make_probe("Probe Mismatch", buy_at=999)
        ds = dataset([bar(i) for i in range(6)])
        with self.assertRaises(ValueError):
            replay(ds, ReplayConfig(strategy="Probe Mismatch", version="1.0", symbol="ETH/USDT", timeframe=TF))
        with self.assertRaises(ValueError):
            replay(ds, ReplayConfig(strategy="Probe Mismatch", version="1.0", symbol=SYM, timeframe="1h"))


class IntrabarExits(unittest.TestCase):
    """Entry fills at the open of bar 5 (price 100 plus half-spread). Stop 2 % below the 100 close, take profit 3 % above."""

    def run_case(self, name, after, **kw):
        make_probe(name, buy_at=5, **kw)
        candles = [bar(i) for i in range(5)] + [bar(5)] + after
        return replay(dataset(candles), cfg(name))

    def test_a_stop_crossed_inside_a_bar_fills_near_the_stop_not_at_the_bar_low(self):
        r = self.run_case("Probe Stop", [bar(6, o=100, h=100, l=95, c=96), bar(7, o=96), bar(8, o=96)])
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "STOP")
        self.assertAlmostEqual(t.exit_price, 98.0, places=6)                               # the stop level, paid at the bid
        self.assertGreater(t.exit_price, 95.0)
        self.assertLess(t.pnl, 0)

    def test_a_take_profit_crossed_inside_a_bar_fills_near_the_target_not_at_the_bar_high(self):
        r = self.run_case("Probe TP", [bar(6, o=100, h=104, l=100, c=103.5), bar(7, o=103.5), bar(8, o=103.5)])
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "TAKE_PROFIT")
        self.assertAlmostEqual(t.exit_price, 103.0, places=6)
        self.assertLess(t.exit_price, 104.0)
        self.assertGreater(t.pnl, 0)

    def test_when_one_bar_could_hit_both_the_stop_comes_first(self):
        r = self.run_case("Probe Both", [bar(6, o=100, h=104, l=96.5, c=100), bar(7, o=100), bar(8, o=100)])
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "STOP")                                            # the pessimistic order: low before high

    def test_a_gap_through_the_stop_fills_at_the_gapped_price_not_at_the_stop(self):
        r = self.run_case("Probe Gap", [bar(6, o=94, h=94, l=94, c=94), bar(7, o=94), bar(8, o=94)])
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "STOP")
        self.assertAlmostEqual(t.exit_price, 94.0 * (1 - 0.0001), places=6)                # the bid at the new open, 4 below the stop
        self.assertEqual(t.exit_time, T0 + M5 * 6)

    def test_a_strategy_exit_is_filled_at_the_next_open_and_labelled(self):
        r = self.run_case("Probe Strategy Exit", [bar(6), bar(7, o=100.4), bar(8, o=100.4), bar(9, o=100.4)], sell_at=7, tp_pct=0.5)
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "STRATEGY")
        self.assertEqual(t.exit_time, T0 + M5 * 7)
        self.assertAlmostEqual(t.exit_price, 100.4 * (1 - 0.0001), places=6)

    def test_a_time_stop_follows_the_virtual_clock_not_the_wall_clock(self):
        make_probe("Probe Time Stop", buy_at=5, tp_pct=0.5)
        candles = [bar(i) for i in range(5)] + [bar(i) for i in range(5, 14)]
        r = replay(dataset(candles), cfg("Probe Time Stop", max_hold_seconds=3 * 300))
        (t,) = r.trades
        self.assertEqual(t.exit_reason, "MAX_HOLD")
        self.assertEqual(t.exit_time - t.entry_time, timedelta(seconds=3 * 300))           # three simulated bars; the test took milliseconds

    def test_without_a_time_stop_a_flat_position_is_simply_still_open_at_the_end(self):
        make_probe("Probe Open End", buy_at=5, tp_pct=0.5)
        r = replay(dataset([bar(i) for i in range(10)]), cfg("Probe Open End"))
        self.assertEqual((r.trades, r.open_position_at_end), ([], True))


class RestingOrders(unittest.TestCase):
    def test_an_entry_that_the_volume_cap_splits_over_several_bars_is_applied_to_the_position_as_it_fills(self):
        import tempfile
        make_probe("Probe Split", buy_at=5, tp_pct=0.5, stop_pct=0.3)
        flat = [bar(i) for i in range(5)]
        q = [f for f in replay(dataset(flat + [bar(i) for i in range(5, 12)]), cfg("Probe Split")).fills if f.side == "BUY"][0].quantity
        thin = [bar(i, v=q * 4) for i in range(12)]                                        # the cap lets 40 % of the order through per bar
        with tempfile.TemporaryDirectory() as d:
            r = replay(dataset(thin), cfg("Probe Split", sim=dict(max_participation=0.1)), workdir=d)
            buys = [f for f in r.fills if f.side == "BUY"]
            self.assertGreaterEqual(len(buys), 3)
            self.assertEqual(len({f.bar_time for f in buys}), len(buys))                   # one part per bar
            self.assertAlmostEqual(sum(f.quantity for f in buys), q, places=4)
            db = Database(os.path.join(d, "replay.db"))
            with db.read() as c:
                held = c.execute("SELECT SUM(quantity) FROM positions WHERE status<>'CLOSED'").fetchone()[0]
            db.close()
        self.assertAlmostEqual(held, q, places=4)                                          # the books follow the exchange, part by part
        self.assertEqual(r.incidents, 0)


class MoneyAndTime(unittest.TestCase):
    def test_fees_and_costs_reduce_the_result_by_what_they_cost(self):
        make_probe("Probe Costs", buy_at=5, tp_pct=0.5, sell_at=8)
        ds = dataset([bar(i) for i in range(12)])
        free = replay(ds, cfg("Probe Costs")).trades[0]
        paid = replay(ds, cfg("Probe Costs", sim=dict(fee_rate=0.001))).trades[0]
        self.assertAlmostEqual(paid.fees, free.quantity * (free.entry_price + free.exit_price) * 0.001, places=6)
        self.assertAlmostEqual(free.pnl - paid.pnl, paid.fees, places=6)

    def test_equity_is_marked_to_market_at_the_bid_while_a_position_is_open(self):
        make_probe("Probe Equity", buy_at=5, tp_pct=0.5, stop_pct=0.3)
        candles = [bar(i) for i in range(5)] + [bar(5), bar(6), bar(7, o=90, c=90), bar(8, o=90)]
        r = replay(dataset(candles), cfg("Probe Equity"))
        eq = dict(r.equity)
        self.assertAlmostEqual(eq[T0 + M5 * 5], 10_000.0, delta=5.0)                       # nothing held yet at bar 4's close
        before, after = eq[T0 + M5 * 7], eq[T0 + M5 * 8]
        self.assertLess(after, before - 1.0)                                               # the unrealised loss shows up in equity
        (f,) = [f for f in r.fills if f.side == "BUY"]
        held = f.quantity
        self.assertAlmostEqual(after, 10_000.0 - held * f.price + held * 90.0 * (1 - 0.0001), delta=0.01)

    def test_the_marked_equity_is_what_the_risk_state_sees(self):
        import tempfile
        make_probe("Probe Risk Equity", buy_at=5, tp_pct=0.5, stop_pct=0.3)
        candles = [bar(i) for i in range(5)] + [bar(5), bar(6), bar(7, o=90, c=90), bar(8, o=90)]
        with tempfile.TemporaryDirectory() as d:
            r = replay(dataset(candles), cfg("Probe Risk Equity"), workdir=d)
            db = Database(os.path.join(d, "replay.db"))
            with db.read() as c:
                seen = c.execute("SELECT current_equity FROM risk_state WHERE scope='PAPER'").fetchone()[0]
            db.close()
        self.assertAlmostEqual(seen, r.equity[-1][1], places=6)
        self.assertLess(seen, 10_000.0 - 1.0)

    def test_every_stored_timestamp_is_simulated_time_never_the_wall_clock(self):
        import tempfile
        make_probe("Probe Stamps", buy_at=5, tp_pct=0.5, sell_at=8)
        with tempfile.TemporaryDirectory() as d:
            replay(dataset([bar(i) for i in range(12)]), cfg("Probe Stamps"), workdir=d)
            db = Database(os.path.join(d, "replay.db"))
            stamps = []
            with db.read() as c:
                tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:                                                       # EVERY text value that looks like a timestamp, in every table
                    for row in c.execute(f"SELECT * FROM {table}"):
                        stamps += [(table, v) for v in tuple(row) if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v)]
            db.close()
        self.assertGreater(len(stamps), 50)
        lo, hi = T0.isoformat(), (T0 + M5 * 13).isoformat()
        wall = [s for s in stamps if not (lo <= str(s[1])[:25] <= hi)]
        self.assertEqual(wall, [])

    def test_the_same_inputs_give_the_same_digest_and_a_changed_cost_changes_it(self):
        make_probe("Probe Repeat", buy_at=5, tp_pct=0.5, sell_at=8)
        ds = dataset([bar(i) for i in range(12)])
        a, b = replay(ds, cfg("Probe Repeat")), replay(ds, cfg("Probe Repeat"))
        self.assertEqual((a.digest, a.final_cash), (b.digest, b.final_cash))
        self.assertNotEqual(a.digest, replay(ds, cfg("Probe Repeat", sim=dict(fee_rate=0.002))).digest)
        self.assertNotEqual(a.digest, replay(dataset([bar(i, o=100.0 + (i == 7)) for i in range(12)]), cfg("Probe Repeat")).digest)


class BuiltInStrategies(unittest.TestCase):
    def test_every_registered_strategy_replays_without_an_error_tick_and_without_incidents(self):
        import math
        candles = []
        for i in range(220):                                                               # a wavy uptrend with moderate bars
            m = 100 + i * 0.05 + 3 * math.sin(i / 9)
            candles.append(Candle(t=T0 + M5 * i, o=m, h=m + 0.4, l=m - 0.4, c=m + 0.1 * math.sin(i), v=1000.0))
        ds = dataset(candles)
        for (name, version) in sorted(base._REGISTRY):
            r = replay(ds, ReplayConfig(strategy=name, version=version, symbol=SYM, timeframe=TF, lookback=120))
            with self.subTest(strategy=name, version=version):
                self.assertEqual(r.ticks.get("ERROR", 0), 0, r.notes[:3])
                self.assertEqual(r.incidents, 0)
                self.assertEqual(sum(r.ticks.values()), 219)
                self.assertEqual(r.ticks.get("WARMUP", 0), 59)
                self.assertTrue(r.simulated)


class RealMarketState(unittest.TestCase):
    """The replay must hand strategies the SAME computed state the live feed builds. (Until this test existed the replay passed bare candles, so
    volume_ratio was always 1.0 and a strategy that needs volume confirmation could never trade: results on real data were silently void.)"""

    def spy(self, name):
        got = []

        class Spy(base.Strategy):
            supported_timeframes = ["5m"]
            default_parameters = {}

            def generate_signal(self, state):
                got.append(state)
                return None
        Spy.name, Spy.version = name, "1.0"
        base.register(Spy)
        return got

    def candles(self, n=100):
        return [Candle(t=T0 + M5 * i, o=100.0 + i * 0.1, h=100.5 + i * 0.1, l=99.5 + i * 0.1, c=100.2 + i * 0.1, v=1000.0 * (5 if i == 80 else 1)) for i in range(n)]

    def test_the_strategy_sees_exactly_what_the_live_state_builder_computes_from_the_closed_candles(self):
        from dewako.market_data.state_builder import build_market_state
        got = self.spy("Spy Real")
        cs = self.candles()
        replay(dataset(cs), ReplayConfig(strategy="Spy Real", version="1.0", symbol=SYM, timeframe=TF, lookback=300))
        self.assertTrue(got)
        for st in (got[0], got[-1], got[len(got) // 2]):
            n = len(st.candles)
            want = build_market_state(SYM, cs[:n], st.as_of, TF)
            self.assertEqual((st.trend, st.momentum, st.volatility, st.volume_ratio, st.regime), (want.trend, want.momentum, want.volatility, want.volume_ratio, want.regime))
        spike = [s for s in got if s.candles[-1].t == T0 + M5 * 80][0]
        self.assertAlmostEqual(spike.volume_ratio, 5.0, places=6)                      # the volume spike is visible, not flattened to 1.0
        self.assertEqual(spike.trend, "BULLISH")                                       # a steady uptrend is labelled as one

    def test_nothing_is_shown_before_sixty_closed_candles_and_the_wait_is_counted_not_hidden(self):
        got = self.spy("Spy Warm")
        r = replay(dataset(self.candles()), ReplayConfig(strategy="Spy Warm", version="1.0", symbol=SYM, timeframe=TF))
        self.assertEqual(len(got[0].candles), 60)
        self.assertEqual(r.ticks["WARMUP"], 59)                                        # bars 1..59 had no usable state
        self.assertEqual(r.ticks.get("ERROR", 0), 0)

    def test_candles_only_mode_is_the_simplified_state_and_is_recorded_in_the_run_config(self):
        got = self.spy("Spy Bare")
        cfg_bare = ReplayConfig(strategy="Spy Bare", version="1.0", symbol=SYM, timeframe=TF, state_source="candles_only")
        replay(dataset(self.candles()), cfg_bare)
        self.assertEqual({s.volume_ratio for s in got}, {1.0})
        self.assertEqual(cfg_bare.record()["state_source"], "candles_only")
        self.assertEqual(ReplayConfig(strategy="x", version="1", symbol=SYM, timeframe=TF).record()["state_source"], "market_state")
        with self.assertRaises(ValueError):
            ReplayConfig(strategy="x", version="1", symbol=SYM, timeframe=TF, state_source="whatever")

    def test_a_volume_gated_built_in_strategy_can_now_fire_on_data_with_volume_spikes(self):
        import random
        random.seed(3)
        cs, p = [], 100.0
        for i in range(500):
            p *= 1 + 0.0015 + random.gauss(0, 0.004)
            v = 1000.0 * (3 if i % 17 == 0 else 1)
            cs.append(Candle(t=T0 + M5 * i, o=p, h=p * 1.003, l=p * 0.997, c=p * 1.001, v=v))
        r = replay(dataset(cs), ReplayConfig(strategy="Trend AI", version="2.1", symbol=SYM, timeframe=TF, lookback=300))
        self.assertGreater(sum(r.ticks.values()) - r.ticks.get("NO_SIGNAL", 0) - r.ticks.get("WARMUP", 0), 0, r.ticks)   # at least one real signal


class ConsecutiveLossLatch(unittest.TestCase):
    """After max_consecutive_losses losing trades in a row the risk engine rejects every signal and only a win resets it. A replay must say
    which of two policies it used and must show the halt, because otherwise a year-long run silently measures only the first weeks."""

    def losers(self, name):
        class Loser(base.Strategy):
            supported_timeframes = ["5m"]
            default_parameters = {}

            def generate_signal(self, state):
                c = state.candles[-1].c
                return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name, strategy_version=self.version,
                              reason=["always buy"], entry_price=c, stop_price=c * 0.999, take_profit_price=None)
        Loser.name, Loser.version = name, "1.0"
        base.register(Loser)

    def falling(self, bars=288 * 3):
        out, p = [], 100.0
        for i in range(bars):
            o, p = p, p * 0.9985                                                  # each bar falls through the 0.1% stop
            out.append(bar(i, o=o, h=o, l=p, c=p))
        return out

    def trades_per_day(self, r):
        d = {}
        for t in r.trades:
            d[t.entry_time.date()] = d.get(t.entry_time.date(), 0) + 1
        return d

    def test_by_default_the_halt_latches_for_the_rest_of_the_run_and_is_visible(self):
        self.losers("Loser Never")
        r = replay(dataset(self.falling()), cfg("Loser Never"))
        days = self.trades_per_day(r)
        self.assertEqual(len(r.trades), 4)                                         # exactly max_consecutive_losses, then nothing
        self.assertEqual(list(days), [T0.date()])
        self.assertGreater(r.ticks["RISK_REJECTED"], 500)
        self.assertTrue(any("maximum consecutive losses" in d for _t, o, d in r.notes if o == "RISK_REJECTED"))

    def test_a_daily_reset_lets_trading_resume_each_utc_day_and_is_recorded(self):
        self.losers("Loser Daily")
        c = cfg("Loser Daily", streak_reset="daily")
        r = replay(dataset(self.falling()), c)
        days = self.trades_per_day(r)
        self.assertEqual(len(days), 3)                                              # one batch of four losing trades per day
        self.assertTrue(all(n == 4 for n in days.values()), days)
        self.assertEqual(c.record()["streak_reset"], "daily")
        self.assertEqual(cfg("x").record()["streak_reset"], "never")
        with self.assertRaises(ValueError):
            cfg("x", streak_reset="hourly")

    def test_the_reset_only_zeroes_the_streak_other_limits_still_apply(self):
        self.losers("Loser Limits")
        r = replay(dataset(self.falling()), cfg("Loser Limits", streak_reset="daily"))
        losses = sum(t.pnl for t in r.trades)
        self.assertLess(losses, 0)                                                  # the account really lost money; nothing was refilled
        self.assertLess(r.final_cash, 10_000.0)


class IdleShortcutIsOnlyASpeedUp(unittest.TestCase):
    """When flat with nothing resting, the replay skips the order sync and the exit monitor. Replaying with the shortcut forced off must give the
    identical digest, fills and cash in every situation where something could still move."""

    def both(self, ds, config):
        from dewako.backtesting import replay as rp
        fast = replay(ds, config)
        rp._Run.no_shortcut = True
        try:
            slow = replay(ds, config)
        finally:
            rp._Run.no_shortcut = False
        self.assertEqual((fast.digest, fast.final_cash, len(fast.fills), len(fast.trades)), (slow.digest, slow.final_cash, len(slow.fills), len(slow.trades)))
        return fast

    def test_identical_results_for_stops_targets_gaps_strategy_exits_and_split_entries(self):
        cases = {
            "stop": [bar(i) for i in range(6)] + [bar(6, o=100, l=97.0), bar(7), bar(8)],
            "target": [bar(i) for i in range(6)] + [bar(6, o=100, h=104.0), bar(7), bar(8)],
            "gap": [bar(i) for i in range(6)] + [bar(6, o=90.0), bar(7, o=90.0), bar(8, o=90.0)],
            "both": [bar(i) for i in range(6)] + [bar(6, o=100, h=104.0, l=96.0), bar(7), bar(8)],
        }
        for k, after in cases.items():
            name = f"Probe Eq {k}"
            make_probe(name, buy_at=5)
            with self.subTest(case=k):
                r = self.both(dataset(after), cfg(name))
                self.assertTrue(r.fills)                                                   # the scenario really traded
        make_probe("Probe Eq nofill", buy_at=5, tp_pct=0.5, stop_pct=0.3)                  # no volume on the bars after the decision: the order rests with no position
        r = self.both(dataset([bar(i) for i in range(5)] + [bar(5, v=0.0), bar(6, v=0.0), bar(7, v=0.0)] + [bar(i) for i in range(8, 12)]), cfg("Probe Eq nofill"))
        self.assertTrue(r.fills)                                                           # it filled once volume returned
        make_probe("Probe Eq exit", buy_at=5, tp_pct=0.5, sell_at=8)
        self.both(dataset([bar(i) for i in range(12)]), cfg("Probe Eq exit"))
        make_probe("Probe Eq split", buy_at=5, tp_pct=0.5, stop_pct=0.3)
        q = [f for f in replay(dataset([bar(i) for i in range(12)]), cfg("Probe Eq split")).fills if f.side == "BUY"][0].quantity
        r = self.both(dataset([bar(i, v=q * 4) for i in range(12)]), cfg("Probe Eq split", sim=dict(max_participation=0.1)))
        self.assertGreaterEqual(len([f for f in r.fills if f.side == "BUY"]), 3)

    def test_identical_results_for_every_built_in_strategy(self):
        import math
        candles = []
        for i in range(220):
            m = 100 + i * 0.05 + 3 * math.sin(i / 9)
            candles.append(Candle(t=T0 + M5 * i, o=m, h=m + 0.4, l=m - 0.4, c=m + 0.1 * math.sin(i), v=1000.0))
        ds = dataset(candles)
        for (name, version) in sorted(base._REGISTRY):
            with self.subTest(strategy=name):
                self.both(ds, ReplayConfig(strategy=name, version=version, symbol=SYM, timeframe=TF, lookback=120))

    def test_an_exit_protection_incident_still_clears_after_the_position_closes_while_the_account_is_flat(self):
        """Live, the exit monitor keeps running while flat and resolves the incident after clean cycles. The shortcut used to skip it, so one
        wide-spread stop latched a halt for the rest of a multi-year replay."""
        class TwoEntries(base.Strategy):
            supported_timeframes = ["5m"]
            default_parameters = {}

            def generate_signal(self, state):
                if len(state.candles) not in (5, 20):
                    return None
                c = state.candles[-1].c
                return Signal(symbol=state.symbol, side=Side.BUY, confidence=0.9, strategy=self.name, strategy_version=self.version,
                              reason=["probe"], entry_price=c, stop_price=c * 0.98, take_profit_price=None)

            def manage_position(self, state, position):
                return None
        TwoEntries.name, TwoEntries.version = "Probe Eq incident", "1.0"
        base.register(TwoEntries)
        candles = [bar(i) for i in range(6)] + [bar(6, o=100, h=100, l=93.0, c=99.0)] + [bar(i) for i in range(7, 30)]
        r = self.both(dataset(candles), cfg("Probe Eq incident", sim=dict(spread_range_factor=0.05)))
        self.assertGreaterEqual(r.incidents, 1)                                            # the wide-spread stop really opened an incident
        self.assertEqual(len([f for f in r.fills if f.side == "BUY"]), 2)                  # and it cleared: the second entry was allowed

    def test_the_shortcut_really_skips_the_monitor_while_flat_and_never_while_a_position_is_open(self):
        from dewako.backtesting import replay as rp
        make_probe("Probe Skip", buy_at=5, tp_pct=0.5, sell_at=9)
        ds = dataset([bar(i) for i in range(14)])
        calls = []
        orig = rp._Run._idle

        def spy(self):
            v = orig(self)
            calls.append(v)
            return v
        rp._Run._idle = spy
        try:
            replay(ds, cfg("Probe Skip"))
        finally:
            rp._Run._idle = orig
        self.assertTrue(any(calls) and not all(calls))                                     # flat stretches skipped, the open stretch is not


class Resources(unittest.TestCase):
    """B9: a replay has a known cost. Bounds are deliberately loose (shared CI machines); they catch a step change, not noise."""

    def test_a_flat_stretch_costs_little_time_and_a_long_one_does_not_grow_memory_per_bar(self):
        import time
        import tracemalloc
        make_probe("Probe Idle", buy_at=10 ** 9)
        ds = dataset([bar(i) for i in range(400)])
        tracemalloc.start()
        t0 = time.perf_counter()
        r = replay(ds, cfg("Probe Idle"))
        secs = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(sum(r.ticks.values()), 399)
        self.assertLess(secs / 400, 0.1, f"{secs / 400 * 1000:.1f} ms per bar")             # measured around 10-15 ms per bar
        self.assertLess(peak / 1e6, 150, f"peak {peak / 1e6:.0f} MB")


class VirtualTimeIsContained(unittest.TestCase):
    def test_virtual_time_is_off_after_a_replay_even_when_the_replay_fails(self):
        make_probe("Probe Fail", buy_at=999)
        ds = dataset([bar(i) for i in range(6)])
        self.assertFalse(timesource.is_virtual())
        replay(ds, cfg("Probe Fail"))
        self.assertFalse(timesource.is_virtual())
        with self.assertRaises(KeyError):
            replay(ds, ReplayConfig(strategy="No Such Strategy", version="1.0", symbol=SYM, timeframe=TF))
        self.assertFalse(timesource.is_virtual())
        self.assertLess(abs((timesource.now() - datetime.now(timezone.utc)).total_seconds()), 5)

    def test_replays_cannot_nest_and_naive_virtual_time_is_refused(self):
        with timesource.virtual_time(lambda: T0):
            with self.assertRaises(RuntimeError):
                with timesource.virtual_time(lambda: T0):
                    pass
        with timesource.virtual_time(lambda: datetime(2026, 1, 1)):
            with self.assertRaises(ValueError):
                timesource.now()
        self.assertFalse(timesource.is_virtual())

    def test_the_live_application_never_imports_virtual_time_or_the_replay(self):
        root = os.path.join(os.path.dirname(__file__), "..", "dewako")
        offenders = []
        for d, _, files in os.walk(root):
            if d.endswith(os.sep + "backtesting") or "__pycache__" in d:
                continue
            for f in files:
                if f.endswith(".py") and not (d.endswith(os.sep + "core") and f == "timesource.py"):
                    text = open(os.path.join(d, f), encoding="utf-8").read()
                    if "virtual_time" in text or "backtesting.replay" in text or "backtesting.sim_adapter" in text:
                        offenders.append(os.path.join(d, f))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
