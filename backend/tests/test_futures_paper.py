"""Paper futures core (stage F1): long/short, leverage selection, ISOLATED and CROSS margin, funding, liquidation.

Every expected number below is worked out by hand in the comment next to it. PAPER ONLY: the package has no network code, and the
spot engine must not import it (checked at the bottom)."""
import ast
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from dewako.futures import (FuturesConfig, FuturesError, MarginMode, PaperFuturesAccount, entry_guard,  # noqa: E402
                            size_for_risk)

NOSLIP = dict(slippage=0.0, fee_rate=0.001)


def acct(balance=10_000.0, **cfg):
    c = dict(max_leverage=5, **NOSLIP)
    c.update(cfg)
    return PaperFuturesAccount(balance, FuturesConfig(**c))


def code_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except FuturesError as e:
        return e.code
    return None


class LeverageAndMode(unittest.TestCase):
    def test_leverage_is_selectable_from_1_to_the_cap_and_nothing_else(self):
        a = acct(max_leverage=5)
        for lev in (1, 3, 5):
            a.set_leverage("ETH", lev)
            self.assertEqual(a.leverage["ETH"], lev)
        for bad in (0, 6, -1, 2.5, "3", True, None):
            self.assertEqual(code_of(a.set_leverage, "ETH", bad), "LEVERAGE_NOT_ALLOWED", repr(bad))
        self.assertEqual(a.leverage["ETH"], 5)

    def test_the_cap_itself_cannot_exceed_the_hard_ceiling_and_env_values_are_validated(self):
        self.assertEqual(FuturesConfig(max_leverage=20).max_leverage, 20)
        for bad in (21, 0, 125, 2.0, True):
            self.assertEqual(code_of(FuturesConfig, max_leverage=bad), "BAD_CONFIG", repr(bad))
        self.assertEqual(FuturesConfig.from_env({}).max_leverage, 3)
        self.assertEqual(FuturesConfig.from_env({"DEWAKO_FUTURES_MAX_LEVERAGE": " 10 "}).max_leverage, 10)
        for bad in ("ten", "3x", "-2", "21", "0", "2.5"):
            self.assertEqual(code_of(FuturesConfig.from_env, {"DEWAKO_FUTURES_MAX_LEVERAGE": bad}), "BAD_CONFIG", bad)

    def test_defaults_are_1x_isolated_and_the_owner_fee(self):
        a = PaperFuturesAccount(10_000.0)
        self.assertEqual((a.config.fee_rate, a.config.max_leverage), (0.001, 3))
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        p = a.positions["ETH"]
        self.assertEqual((p.leverage, p.mode), (1, MarginMode.ISOLATED))

    def test_leverage_and_mode_cannot_change_while_the_position_is_open(self):
        a = acct()
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        self.assertEqual(code_of(a.set_leverage, "ETH", 3), "POSITION_OPEN")
        self.assertEqual(code_of(a.set_margin_mode, "ETH", MarginMode.CROSS), "POSITION_OPEN")
        self.assertEqual(code_of(a.set_margin_mode, "BTC", "PORTFOLIO"), "BAD_MODE")
        a.close("ETH")
        a.set_leverage("ETH", 3)
        a.set_margin_mode("ETH", "CROSS")
        self.assertEqual((a.leverage["ETH"], a.mode["ETH"]), (3, MarginMode.CROSS))


class IsolatedLongAndShort(unittest.TestCase):
    def test_isolated_long_open_then_close_at_a_profit(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        f = a.market_order("ETH", "BUY", 1)
        # notional 2000, margin 2000/5 = 400, fee 2 -> wallet 10000 - 400 - 2 = 9598
        self.assertEqual((f.price, f.fee, a.wallet, a.positions["ETH"].isolated_margin), (2000.0, 2.0, 9598.0, 400.0))
        a.set_mark("ETH", 2100)
        self.assertAlmostEqual(a.equity(), 9598 + 400 + 100)
        f = a.close("ETH")
        # pnl +100, fee 2.1 -> wallet 9598 + 400 + 100 - 2.1 = 10095.9
        self.assertAlmostEqual(f.realized_pnl, 100.0)
        self.assertAlmostEqual(a.wallet, 10095.9)
        self.assertEqual(a.positions, {})
        a.check()

    def test_isolated_short_profits_when_the_price_falls_and_loses_when_it_rises(self):
        a = acct()
        a.set_leverage("ETH", 2)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "SELL", 1)                      # margin 1000, fee 2 -> wallet 8998
        self.assertEqual((a.positions["ETH"].qty, a.positions["ETH"].side, a.wallet), (-1.0, "SHORT", 8998.0))
        a.set_mark("ETH", 1900)
        self.assertAlmostEqual(a.upnl(a.positions["ETH"]), 100.0)
        a.set_mark("ETH", 2100)
        self.assertAlmostEqual(a.upnl(a.positions["ETH"]), -100.0)
        f = a.close("ETH")                                    # pnl -100, fee 2.1 -> 8998 + 1000 - 100 - 2.1 = 9895.9
        self.assertAlmostEqual(f.realized_pnl, -100.0)
        self.assertAlmostEqual(a.wallet, 9895.9)
        a.check()

    def test_slippage_always_hurts_buy_higher_sell_lower(self):
        a = PaperFuturesAccount(10_000.0, FuturesConfig(slippage=0.0005, fee_rate=0.001))
        a.set_mark("ETH", 2000)
        self.assertAlmostEqual(a.market_order("ETH", "BUY", 1).price, 2001.0)
        self.assertAlmostEqual(a.close("ETH").price, 1999.0)
        a.check()
        self.assertLess(a.wallet, 10_000.0)

    def test_adding_to_a_position_averages_the_entry_and_partial_close_releases_margin_pro_rata(self):
        a = acct()
        a.set_leverage("ETH", 4)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)                       # margin 500
        a.set_mark("ETH", 2200)
        a.market_order("ETH", "BUY", 1)                       # margin 550
        p = a.positions["ETH"]
        self.assertAlmostEqual(p.entry, 2100.0)
        self.assertAlmostEqual(p.isolated_margin, 1050.0)
        w = a.wallet
        f = a.market_order("ETH", "SELL", 0.5)                # quarter of 2: releases 262.5, pnl 0.5*100 = 50, fee 1.1
        self.assertTrue(f.reduce)
        self.assertAlmostEqual(a.wallet, w + 262.5 + 50 - 1.1)
        self.assertAlmostEqual(p.isolated_margin, 787.5)
        self.assertAlmostEqual(p.qty, 1.5)
        a.check()


class OrdersRefused(unittest.TestCase):
    def test_an_order_never_flips_a_position_and_reduce_only_never_opens(self):
        a = acct()
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        self.assertEqual(code_of(a.market_order, "ETH", "SELL", 1.5), "WOULD_FLIP")
        self.assertAlmostEqual(a.positions["ETH"].qty, 1.0)
        f = a.market_order("ETH", "SELL", 5, reduce_only=True)          # capped at the position size
        self.assertEqual(f.qty, 1.0)
        self.assertEqual(a.positions, {})
        self.assertEqual(code_of(a.market_order, "ETH", "SELL", 1, reduce_only=True), "REDUCE_ONLY")
        a.market_order("ETH", "SELL", 1)
        self.assertEqual(code_of(a.market_order, "ETH", "SELL", 1, reduce_only=True), "REDUCE_ONLY")   # same side: would add
        self.assertEqual(code_of(a.close, "BTC"), "NO_POSITION")

    def test_bad_orders_are_refused_and_change_nothing(self):
        a = acct()
        self.assertEqual(code_of(a.market_order, "ETH", "BUY", 1), "NO_PRICE")
        a.set_mark("ETH", 2000)
        for args, code in ((("ETH", "HOLD", 1), "BAD_SIDE"), (("ETH", "BUY", 0), "BAD_QTY"), (("ETH", "BUY", -1), "BAD_QTY"),
                           (("ETH", "BUY", float("nan")), "BAD_QTY"), (("ETH", "BUY", 0.0004), "BAD_QTY"),
                           (("ETH", "BUY", 0.002), "MIN_NOTIONAL")):
            self.assertEqual(code_of(a.market_order, *args), code, args)
        for bad in (0, -5, float("inf"), float("nan"), "2000"):
            self.assertEqual(code_of(a.set_mark, "ETH", bad), "BAD_PRICE", repr(bad))
        self.assertEqual((a.wallet, a.positions, a.ledger), (10_000.0, {}, []))

    def test_margin_limits_position_size_to_balance_times_leverage(self):
        a = acct(1_000.0)
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        # 2.5 ETH = 5000 notional needs margin 1000 + fee 5 > 1000
        self.assertEqual(code_of(a.market_order, "ETH", "BUY", 2.5), "INSUFFICIENT_MARGIN")
        a.market_order("ETH", "BUY", 2.4)                     # 4800: margin 960 + fee 4.8 = 964.8
        self.assertAlmostEqual(a.wallet, 35.2)
        a.check()

    def test_quantity_is_rounded_down_to_the_step(self):
        a = acct()
        a.set_mark("ETH", 2000)
        self.assertAlmostEqual(a.market_order("ETH", "BUY", 1.2349).qty, 1.234)


class IsolatedLiquidation(unittest.TestCase):
    def test_liquidation_price_formula_long_and_short(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        # long: (entry*qty - margin) / (qty*(1 - mmr)) = (2000 - 400) / 0.995 = 1608.0402
        self.assertAlmostEqual(a.liquidation_price("ETH"), 1600 / 0.995, places=6)
        b = acct()
        b.set_leverage("ETH", 5)
        b.set_mark("ETH", 2000)
        b.market_order("ETH", "SELL", 1)
        # short: (entry*qty + margin) / (qty*(1 + mmr)) = 2400 / 1.005 = 2388.0597
        self.assertAlmostEqual(b.liquidation_price("ETH"), 2400 / 1.005, places=6)
        self.assertIsNone(b.liquidation_price("BTC"))

    def test_long_is_liquidated_at_its_liquidation_price_not_before_and_loses_only_its_margin(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        liq = a.liquidation_price("ETH")
        self.assertEqual(a.set_mark("ETH", liq + 0.01), [])
        events = a.set_mark("ETH", liq - 0.01)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e.symbol, e.side, e.mode, e.qty, e.loss), ("ETH", "LONG", MarginMode.ISOLATED, 1.0, 400.0))
        self.assertEqual(a.positions, {})
        self.assertAlmostEqual(a.wallet, 9598.0)              # the rest of the account is untouched
        a.check()
        self.assertEqual(a.set_mark("ETH", 100), [])          # nothing left to liquidate

    def test_short_is_liquidated_when_the_price_rises(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "SELL", 1)
        liq = a.liquidation_price("ETH")
        self.assertEqual(a.set_mark("ETH", liq - 0.01), [])
        self.assertEqual([e.side for e in a.set_mark("ETH", liq + 0.01)], ["SHORT"])
        self.assertAlmostEqual(a.wallet, 9598.0)
        a.check()

    def test_higher_leverage_puts_liquidation_closer(self):
        dist = {}
        for lev in (1, 2, 5, 10, 20):
            a = acct(max_leverage=20)
            a.set_leverage("ETH", lev)
            a.set_mark("ETH", 2000)
            a.market_order("ETH", "BUY", 1)
            liq = a.liquidation_price("ETH")
            dist[lev] = None if liq is None else 2000 - liq
        self.assertLess(dist[20], dist[10])
        self.assertLess(dist[10], dist[5])
        self.assertLess(dist[5], dist[2])
        self.assertAlmostEqual(dist[2], 2000 - 1000 / 0.995)
        self.assertIsNone(dist[1])                            # 1x long: (2000 - 2000)/0.995 = 0 -> no price above zero liquidates it

    def test_an_entry_whose_margin_is_below_maintenance_is_liquidated_at_once(self):
        a = acct(max_leverage=20, maintenance_tiers=((math.inf, 0.10),))      # margin 5% of notional < maintenance 10%
        a.set_leverage("ETH", 20)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        self.assertEqual(a.positions, {})
        self.assertEqual([(e.symbol, e.loss) for e in a.liquidations], [("ETH", 100.0)])
        self.assertAlmostEqual(a.wallet, 10_000 - 100 - 2)
        a.check()

    def test_a_gap_through_the_liquidation_price_still_loses_only_the_isolated_margin(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        a.set_mark("ETH", 1000)                               # far past liquidation (uPnL would be -1000)
        self.assertAlmostEqual(a.wallet, 9598.0)
        self.assertAlmostEqual(a.equity(), 9598.0)
        a.check()


class CrossMargin(unittest.TestCase):
    def _two(self):
        a = acct()
        for s, px in (("ETH", 2000.0), ("BTC", 50_000.0)):
            a.set_margin_mode(s, MarginMode.CROSS)
            a.set_leverage(s, 5)
            a.set_mark(s, px)
        return a

    def test_cross_keeps_margin_in_the_wallet_and_shares_it(self):
        a = self._two()
        a.market_order("ETH", "BUY", 2)                       # 4000 notional, fee 4; no margin leaves the wallet
        self.assertAlmostEqual(a.wallet, 9996.0)
        self.assertEqual(a.positions["ETH"].isolated_margin, 0.0)
        self.assertAlmostEqual(a.available(), 9996 - 800)     # initial margin 4000/5 reserved
        a.set_mark("ETH", 1900)                               # uPnL -200
        self.assertAlmostEqual(a.available(), 9996 - 200 - 760)
        a.set_mark("ETH", 2100)                               # unrealized profit is not spendable
        self.assertAlmostEqual(a.available(), 9996 - 840)
        a.check()

    def test_cross_liquidation_price_uses_the_whole_wallet(self):
        a = self._two()
        a.market_order("ETH", "BUY", 2)
        # long: (qty*entry - wallet) / (qty*(1 - mmr)) = (4000 - 9996) / 1.99 < 0 -> cannot be liquidated by ETH alone
        self.assertIsNone(a.liquidation_price("ETH"))
        b = acct(1_000.0)
        b.set_margin_mode("ETH", "CROSS")
        b.set_leverage("ETH", 5)
        b.set_mark("ETH", 2000)
        b.market_order("ETH", "BUY", 2)                       # wallet 996
        self.assertAlmostEqual(b.liquidation_price("ETH"), (4000 - 996) / 1.99, places=6)     # 1509.5477

    def test_cross_liquidation_closes_every_cross_position_and_takes_the_whole_wallet_but_not_isolated_margin(self):
        a = acct(2_000.0)
        a.set_mark("SOL", 100.0)
        a.set_leverage("SOL", 2)
        a.market_order("SOL", "BUY", 2)                       # ISOLATED: margin 100, fee 0.2 -> wallet 1899.8
        for s, px in (("ETH", 2000.0), ("BTC", 50_000.0)):
            a.set_margin_mode(s, "CROSS")
            a.set_leverage(s, 5)
            a.set_mark(s, px)
        a.market_order("ETH", "BUY", 2)                       # fee 4
        a.market_order("BTC", "BUY", 0.05)                    # 2500 notional, fee 2.5 -> wallet 1893.3
        self.assertAlmostEqual(a.wallet, 1893.3)
        self.assertEqual(a.set_mark("ETH", 1500), [])         # uPnL -1000: equity 893.3 > maintenance 15 + 12.5
        events = a.set_mark("ETH", 1060)                      # uPnL -1880: equity 13.3 <= maintenance 10.6 + 12.5
        self.assertEqual(sorted(e.symbol for e in events), ["BTC", "ETH"])
        self.assertTrue(all(e.mode is MarginMode.CROSS for e in events))
        self.assertAlmostEqual(sum(e.loss for e in events), 1893.3)
        self.assertEqual(a.wallet, 0.0)
        self.assertEqual(list(a.positions), ["SOL"])          # the isolated position and its margin survive
        self.assertAlmostEqual(a.positions["SOL"].isolated_margin, 100.0)
        a.check()

    def test_a_cross_profit_on_one_symbol_holds_up_a_loss_on_another(self):
        a = acct(1_000.0)
        for s, px in (("ETH", 2000.0), ("BTC", 50_000.0)):
            a.set_margin_mode(s, "CROSS")
            a.set_leverage(s, 5)
            a.set_mark(s, px)
        a.market_order("ETH", "BUY", 1)                       # fee 2
        a.market_order("BTC", "SELL", 0.04)                   # 2000 notional, fee 2 -> wallet 996
        a.set_mark("BTC", 40_000)                             # short profit +400
        self.assertEqual(a.set_mark("ETH", 700), [])          # long loss -1300: equity 96 > maintenance 3.5 + 8
        self.assertEqual(len(a.set_mark("BTC", 50_000)), 2)   # the profit goes: equity -304
        self.assertEqual(a.wallet, 0.0)
        a.check()


class Funding(unittest.TestCase):
    def test_positive_funding_longs_pay_shorts_receive(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        a.apply_funding("ETH", 0.0001)                        # 2000 * 0.0001 = 0.2 from the isolated margin
        self.assertAlmostEqual(a.positions["ETH"].isolated_margin, 399.8)
        b = acct()
        b.set_margin_mode("ETH", "CROSS")
        b.set_mark("ETH", 2000)
        b.market_order("ETH", "SELL", 1)                      # wallet 9998
        b.apply_funding("ETH", 0.0001)                        # the short receives 0.2
        self.assertAlmostEqual(b.wallet, 9998.2)
        b.apply_funding("ETH", -0.0001)                       # negative rate: the short pays
        self.assertAlmostEqual(b.wallet, 9998.0)
        self.assertEqual(b.apply_funding("BTC", 0.0001), [])
        for bad in (0.05, float("nan"), "0.0001"):
            self.assertEqual(code_of(b.apply_funding, "ETH", bad), "BAD_FUNDING", repr(bad))
        a.check()
        b.check()

    def test_funding_can_push_an_isolated_position_into_liquidation(self):
        a = acct()
        a.set_leverage("ETH", 5)
        a.set_mark("ETH", 2000)
        a.market_order("ETH", "BUY", 1)
        liq = a.liquidation_price("ETH")
        self.assertEqual(a.set_mark("ETH", liq + 5), [])
        events = a.apply_funding("ETH", 0.01)                 # pays ~16: margin falls, liquidation price rises above the mark
        self.assertEqual(len(events), 1)
        self.assertEqual(a.positions, {})
        a.check()


class EntryRules(unittest.TestCase):
    def test_size_for_risk_ignores_leverage(self):
        # 1% of 10000 = 100 at risk; stop 50 away -> 2 ETH
        self.assertAlmostEqual(size_for_risk(10_000, 0.01, 2000, 1950), 2.0)
        self.assertAlmostEqual(size_for_risk(10_000, 0.01, 2000, 2050), 2.0)          # short
        for bad in ((10_000, 0.06, 2000, 1950), (10_000, 0.01, 2000, 2000), (0, 0.01, 2000, 1950), (10_000, 0, 2000, 1950)):
            self.assertEqual(code_of(size_for_risk, *bad), "BAD_RISK", bad)

    def test_entry_guard_requires_liquidation_far_beyond_the_stop(self):
        self.assertEqual(entry_guard("LONG", 2000, 1900, 1608.04, 5, 5), [])          # liq 392 away >= 2 x 100
        self.assertEqual(len(entry_guard("LONG", 2000, 1900, 1810.0, 10, 20)), 1)     # liq 190 away < 200
        self.assertEqual(entry_guard("SHORT", 2000, 2100, 2388.06, 5, 5), [])
        self.assertEqual(len(entry_guard("SHORT", 2000, 2100, 2150.0, 5, 5)), 1)
        self.assertEqual(entry_guard("LONG", 2000, 1900, None, 1, 5), [])             # cannot be liquidated at all
        self.assertIn("above the cap", entry_guard("LONG", 2000, 1900, 1608.04, 6, 5)[0])
        self.assertIn("losing side", entry_guard("LONG", 2000, 2100, 1608.04, 5, 5)[0])
        self.assertIn("losing side", entry_guard("SHORT", 2000, 1900, 2388.06, 5, 5)[0])
        self.assertEqual(entry_guard("BOTH", 2000, 1900, None, 1, 5), ["side must be LONG or SHORT"])


class Invariants(unittest.TestCase):
    def test_money_is_conserved_and_never_negative_over_random_sessions(self):
        for seed in range(40):
            rng = random.Random(seed)
            a = PaperFuturesAccount(10_000.0, FuturesConfig(max_leverage=10))
            px = {"ETH": 2000.0, "BTC": 50_000.0, "SOL": 100.0}
            for s, p in px.items():
                a.set_mark(s, p)
            for _ in range(300):
                s = rng.choice(list(px))
                r = rng.random()
                try:
                    if r < 0.35:
                        px[s] *= math.exp(rng.gauss(0, 0.03))
                        a.set_mark(s, px[s])
                    elif r < 0.45:
                        a.apply_funding(s, rng.uniform(-0.001, 0.001))
                    elif r < 0.55 and s not in a.positions:
                        a.set_leverage(s, rng.randint(1, 10))
                        a.set_margin_mode(s, rng.choice(list(MarginMode)))
                    elif r < 0.85:
                        a.market_order(s, rng.choice(["BUY", "SELL"]), rng.uniform(0.001, 3000 / px[s]),
                                       reduce_only=rng.random() < 0.3)
                    elif s in a.positions:
                        a.close(s)
                except FuturesError:
                    pass
                a.check()
                self.assertGreaterEqual(a.wallet, -1e-6, f"seed {seed}: wallet {a.wallet} with {list(a.positions)}")
                for p in a.positions.values():
                    self.assertNotEqual(p.qty, 0.0)
                    if p.mode is MarginMode.ISOLATED:        # an open isolated position is always above maintenance
                        self.assertGreater(p.isolated_margin + a.upnl(p), a._maint(p))
            for s in list(a.positions):
                a.close(s)
            a.check()
            self.assertAlmostEqual(a.wallet, a.equity())
            self.assertAlmostEqual(a.wallet, 10_000.0 + sum(x for _, _, x in a.ledger), places=4)


class PaperOnly(unittest.TestCase):
    def test_the_package_has_no_network_or_exchange_code_and_the_spot_engine_does_not_import_it(self):
        root = os.path.join(os.path.dirname(__file__), "..", "dewako")
        banned = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websockets", "ccxt", "binance",
                  "subprocess", "os"}
        files = [f for f in os.listdir(os.path.join(root, "futures")) if f.endswith(".py")]
        self.assertIn("paper.py", files)
        for f in files:
            tree = ast.parse(open(os.path.join(root, "futures", f), encoding="utf-8").read())
            for node in ast.walk(tree):
                mods = [n.name for n in node.names] if isinstance(node, ast.Import) else \
                       [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for m in mods:
                    self.assertNotIn(m.split(".")[0], banned, f"{f} imports {m}")
                    self.assertFalse(m.startswith("dewako.") and not m.startswith("dewako.futures"), f"{f} imports {m}")
        users = []
        for d, _, names in os.walk(root):
            if os.path.basename(d) in ("futures", "__pycache__"):
                continue
            for n in names:
                if n.endswith(".py") and "dewako.futures" in open(os.path.join(d, n), encoding="utf-8").read():
                    users.append(os.path.join(d, n))
        self.assertEqual(users, [], "the spot engine must not use the futures package until its stage is approved")


if __name__ == "__main__":
    unittest.main()
