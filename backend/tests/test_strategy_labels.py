"""Strategy market labels: what each strategy can signal, and what the platform can execute for it on SPOT and FUTURES today."""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import dewako.strategies.library  # noqa: E402,F401  (registers the built-ins)
from dewako.strategies import base  # noqa: E402
from dewako.strategies.labels import BUILTIN_DIRECTIONS, directions_of  # noqa: E402
from dewako.strategies.rule_builder import RuleSpec, build_strategy_class  # noqa: E402


def builtins():
    return [c for c in base.all_strategies() if getattr(c, "spec", None) is None and c.__module__.startswith("dewako.strategies.")]


def test_every_built_in_strategy_declares_which_sides_it_signals():
    assert builtins()
    for c in builtins():
        assert directions_of(c), f"{c.name} v{c.version} has no declared directions (strategies/labels.py)"
        assert c.name in BUILTIN_DIRECTIONS


def test_the_declared_sides_match_the_entries_written_in_each_strategys_source():
    """Static check against drift: a strategy that writes both Side.BUY and Side.SELL entries must be labelled LONG+SHORT, one that writes
    only BUY must not claim SHORT. (It reads the source; it does not prove the data will ever trigger a short.)"""
    for c in builtins():
        src = "".join(inspect.getsource(inspect.getmodule(k)) for k in c.__mro__ if k is not base.Strategy and issubclass(k, base.Strategy))
        want = tuple(d for d, tok in (("LONG", "Side.BUY"), ("SHORT", "Side.SELL")) if tok in src)
        assert directions_of(c) == want, (c.name, c.version, directions_of(c), want)


def test_labels_are_not_part_of_a_strategys_source_so_no_released_fingerprint_changes():
    for c in builtins():
        assert "directions" not in c.__dict__, f"{c.name}: a label inside the class changes its fingerprint and invalidates released versions"


def test_spot_executes_only_the_long_side_and_says_so():
    for c in builtins():
        m = c.market_support()["SPOT"]
        assert m["status"] == "SUPPORTED" and m["trades"] == ["LONG"]
        if "SHORT" in directions_of(c):
            assert "short entries are refused" in m["note"], c.name
        else:
            assert m["note"] == "long-only", c.name


def test_futures_is_not_implemented_for_any_strategy_and_lists_what_is_missing():
    for c in base.all_strategies():
        f = c.market_support()["FUTURES"]
        assert f["status"] == "NOT_IMPLEMENTED" and f["trades"] == []
        assert {"short positions", "margin and liquidation", "funding"} <= set(f["needs"])


def test_the_futures_label_matches_what_the_execution_engine_really_refuses():
    from dewako.execution import engine
    assert 'NOT_IMPLEMENTED: {market_type} execution' in inspect.getsource(engine.ExecutionEngine._refusals)


def test_a_rule_built_strategy_is_labelled_from_its_spec():
    cond = {"type": "rsi_below", "length": 14, "value": 30}
    both = build_strategy_class(RuleSpec(name="Label Both", entry_long=[cond], entry_short=[{"type": "rsi_above", "length": 14, "value": 70}]), do_register=False)
    long_only = build_strategy_class(RuleSpec(name="Label Long", entry_long=[cond]), do_register=False)
    short_only = build_strategy_class(RuleSpec(name="Label Short", entry_short=[{"type": "rsi_above", "length": 14, "value": 70}]), do_register=False)
    assert directions_of(both) == ("LONG", "SHORT") and directions_of(long_only) == ("LONG",) and directions_of(short_only) == ("SHORT",)
    assert "long-only" in long_only.market_support()["SPOT"]["note"]
    assert "a short side (this strategy is long-only)" in long_only.market_support()["FUTURES"]["needs"]
    assert short_only.market_support()["SPOT"]["status"] == "NOTHING_TO_TRADE" and short_only.market_support()["SPOT"]["trades"] == []


def test_an_unlabelled_strategy_is_reported_as_undeclared_not_assumed_long_only():
    class Mystery(base.Strategy):
        name, version = "Mystery", "1.0"

        def generate_signal(self, state):
            return None
    m = Mystery.market_support()["SPOT"]
    assert m["status"] == "UNDECLARED" and m["trades"] == []


def test_describe_carries_the_labels_to_the_api():
    d = base.get("Trend AI", "2.2")().describe()
    assert d["directions"] == ["LONG", "SHORT"] and d["market_support"]["SPOT"]["status"] == "SUPPORTED"
