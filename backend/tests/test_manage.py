"""Tests for adding exchanges and strategies manually."""
import asyncio
import hashlib
import hmac
import os
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from dewako.core.types import Candle, MarketState, Side
from dewako.exchanges.base import ExchangeAdapter, ExchangeError
from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.manager import ExchangeManager, ExchangeRejected
from dewako.exchanges.stubs import _StubAdapter
from dewako.services.credentials import CredentialVault, VaultError, mask
from dewako.strategies import loader
from dewako.strategies.rule_builder import Condition, RuleSpec, build_strategy_class

KEY = Fernet.generate_key()


class FakeAdapter(_StubAdapter):
    name = "binance"

    def __init__(self, perms=None, fail=None, **_):
        self.perms, self.fail = perms, fail

    async def connect(self):
        return None

    async def get_api_permissions(self):
        if self.fail:
            raise ExchangeError(self.fail)
        return self.perms


def mgr(perms=None, fail=None):
    return ManagerWith(perms, fail)


def ManagerWith(perms, fail):
    return ExchangeManager(CredentialVault(KEY), factory=lambda ex, creds: FakeAdapter(perms=perms, fail=fail))


def add(m, **kw):
    args = dict(exchange="binance", label="main", api_key="AKIA12345678", api_secret="s3cr3t-value-xyz")
    args.update(kw)
    return asyncio.run(m.add_exchange(**args))


# ---- vault
def test_vault_roundtrip_and_wrong_key():
    v = CredentialVault(KEY)
    blob = v.encrypt({"api_key": "abc"})
    assert b"abc" not in blob and v.decrypt(blob) == {"api_key": "abc"}
    with pytest.raises(VaultError):
        CredentialVault(Fernet.generate_key()).decrypt(blob)


def test_vault_requires_key():
    os.environ.pop("ENCRYPTION_KEY", None)
    with pytest.raises(VaultError):
        CredentialVault()


def test_mask_hides_secret():
    assert mask("AKIA12345678") == "•" * 8 + "5678"


# ---- exchange manager
def test_add_exchange_with_safe_key_stores_encrypted_and_masks():
    m = mgr({"read": True, "trade": True, "withdraw": False})
    rec = add(m)
    pub = m.list()[0]
    assert pub["permissions"] == {"read": True, "trade": True, "withdraw": False}
    assert "s3cr3t" not in str(pub) and pub["api_key"].endswith("5678") and "AKIA" not in pub["api_key"]
    assert b"s3cr3t" not in rec.blob
    assert m.credentials_for_adapter(rec.id)["api_secret"] == "s3cr3t-value-xyz"


def test_key_with_withdrawal_is_rejected_and_not_stored():
    m = mgr({"read": True, "trade": True, "withdraw": True})
    with pytest.raises(ExchangeRejected):
        add(m)
    assert m.list() == [] and m.audit[-1]["event"] == "rejected_withdraw_enabled"


def test_missing_withdraw_flag_counts_as_enabled():
    m = mgr({"read": True, "trade": True})
    with pytest.raises(ExchangeRejected):
        add(m)


def test_unverifiable_key_is_rejected():
    m = mgr(fail="stub")
    with pytest.raises(ExchangeRejected):
        add(m)
    assert m.list() == []


def test_unsupported_exchange_and_missing_fields_rejected():
    m = mgr({"read": True, "trade": True, "withdraw": False})
    with pytest.raises(ExchangeRejected):
        add(m, exchange="nope")
    with pytest.raises(ExchangeRejected):
        add(m, api_key="")
    with pytest.raises(ExchangeRejected):
        add(m, exchange="okx")  # passphrase required


def test_default_factory_with_stub_exchange_refuses():
    m = ExchangeManager(CredentialVault(KEY))
    with pytest.raises(ExchangeRejected):
        add(m, exchange="bybit")  # adapter is a stub, cannot verify permissions


def test_remove_exchange():
    m = mgr({"read": True, "trade": False, "withdraw": False})
    rec = add(m)
    m.remove(rec.id)
    assert m.list() == []
    with pytest.raises(KeyError):
        m.remove(rec.id)


# ---- Binance permission check
def test_binance_permission_parsing_and_signature():
    seen = {}

    def transport(url, headers):
        seen["url"], seen["headers"] = url, headers
        return {"enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False}

    a = BinanceAdapter("KEY12345", "SECRET123", transport=transport, clock=lambda: 1_700_000_000.0)
    perms = asyncio.run(a.get_api_permissions())
    assert perms == {"read": True, "trade": True, "withdraw": False}
    assert seen["headers"] == {"X-MBX-APIKEY": "KEY12345"}
    base, _, sig = seen["url"].rpartition("&signature=")
    query = urllib.parse.urlparse(base).query
    assert query == "timestamp=1700000000000&recvWindow=5000"
    assert sig == hmac.new(b"SECRET123", query.encode(), hashlib.sha256).hexdigest()


def test_binance_withdraw_enabled_is_reported():
    a = BinanceAdapter("KEY12345", "SECRET123", transport=lambda u, h: {"enableReading": True, "enableWithdrawals": True})
    assert asyncio.run(a.get_api_permissions())["withdraw"] is True


def test_binance_unexpected_response_is_an_error_not_a_guess():
    a = BinanceAdapter("KEY12345", "SECRET123", transport=lambda u, h: {"hello": 1})
    with pytest.raises(ExchangeError):
        asyncio.run(a.get_api_permissions())


# ---- rule builder
def candles(n=120, step=0.5):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out, p = [], 100.0
    for i in range(n):
        c = p + step
        out.append(Candle(t=t0 + timedelta(minutes=15 * i), o=p, h=c + .2, l=p - .2, c=c, v=1.0))
        p = c
    return out


def state(cs, vr=2.0):
    return MarketState(symbol="BTC/USDT", as_of=datetime.now(timezone.utc), candles=cs, volume_ratio=vr)


def spec(**kw):
    base = dict(name="Test Rules", version="1.0",
                entry_long=[Condition(type="ema_fast_above_slow", length=10, slow_length=30),
                            Condition(type="volume_ratio_above", value=1.5)])
    base.update(kw)
    return RuleSpec(**base)


def test_rule_strategy_generates_signal_with_reasons_and_stop():
    cls = build_strategy_class(spec(name="Rules Signal"))
    s = cls().generate_signal(state(candles()))
    assert s and s.side is Side.BUY and s.stop_price < s.entry_price
    assert s.strategy == "Rules Signal" and any("EMA 10 above EMA 30" in r for r in s.reason)


def test_rule_strategy_silent_when_a_condition_fails():
    cls = build_strategy_class(spec(name="Rules Silent"))
    assert cls().generate_signal(state(candles(), vr=0.5)) is None


def test_rule_strategy_short_side_and_take_profit():
    cls = build_strategy_class(spec(name="Rules Short", entry_long=[], take_profit_rr=2,
                                    entry_short=[Condition(type="ema_fast_below_slow", length=10, slow_length=30)]))
    s = cls().generate_signal(state(candles(step=-0.5)))
    assert s and s.side is Side.SELL and s.stop_price > s.entry_price and s.take_profit_price < s.entry_price


def test_rule_versions_are_immutable():
    build_strategy_class(spec(name="Rules Dup"))
    with pytest.raises(ValueError):
        build_strategy_class(spec(name="Rules Dup"))
    build_strategy_class(spec(name="Rules Dup", version="1.1"))  # new version is fine


def test_rule_spec_validation():
    with pytest.raises(Exception):
        RuleSpec(name="x", entry_long=[Condition(type="rsi_below", value=30)])  # name too short
    with pytest.raises(Exception):
        RuleSpec(name="No Rules Here")
    with pytest.raises(Exception):
        Condition(type="ema_fast_above_slow", length=50, slow_length=20)
    with pytest.raises(Exception):
        Condition(type="rsi_below", value=0)
    with pytest.raises(Exception):
        Condition(type="__import__('os')", value=1)  # not in whitelist
    with pytest.raises(Exception):
        spec(name="Bad Stop", atr_stop=50)


# ---- code plugin loader
def test_code_plugins_disabled_by_default():
    os.environ.pop("DEWAKO_ALLOW_CODE_PLUGINS", None)
    with pytest.raises(loader.PluginsDisabled):
        loader.load_code_plugins(tempfile.gettempdir())


def test_code_plugins_load_when_enabled():
    d = Path(tempfile.mkdtemp())
    (d / "my_plugin.py").write_text(
        "from dewako.strategies.base import Strategy, register\n"
        "@register\nclass P(Strategy):\n    name='Plugin Strat'\n    version='9.9'\n"
        "    def generate_signal(self, state): return None\n")
    (d / "_skip.py").write_text("raise RuntimeError('should not load')\n")
    os.environ["DEWAKO_ALLOW_CODE_PLUGINS"] = "1"
    try:
        assert loader.load_code_plugins(d) == ["my_plugin.py"]
    finally:
        os.environ.pop("DEWAKO_ALLOW_CODE_PLUGINS")
