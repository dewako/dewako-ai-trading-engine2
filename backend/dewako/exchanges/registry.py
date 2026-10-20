from __future__ import annotations

from dewako.exchanges.base import ExchangeAdapter
from dewako.exchanges.binance import BinanceAdapter
from dewako.exchanges.stubs import BybitAdapter, OKXAdapter

REGISTRY: dict[str, type[ExchangeAdapter]] = {
    "binance": BinanceAdapter,
    "bybit": BybitAdapter,
    "okx": OKXAdapter,
}
