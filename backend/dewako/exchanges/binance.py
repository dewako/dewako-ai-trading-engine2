"""Binance adapter. Phase 1 implements ONLY the API-key permission check.

Everything else still raises NotConnected. The permission check calls Binance's signed
`GET /sapi/v1/account/apiRestrictions` endpoint. It has NOT been run against the live
exchange yet; the signing and response parsing are unit-tested with a fake transport.
Verify with a read-only key before relying on it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from dewako.exchanges.base import ExchangeError, NotConnected
from dewako.exchanges.stubs import _StubAdapter

BASE = "https://api.binance.com"


def _urllib_transport(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ExchangeError(f"Binance rejected the request (HTTP {e.code}). Check the key, secret and IP allowlist") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ExchangeError(f"could not reach Binance: {e}") from e


class BinanceAdapter(_StubAdapter):
    name = "binance"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 transport: Callable[[str, dict], dict] | None = None, clock: Callable[[], float] = time.time, **_):
        self._key, self._secret = api_key, api_secret
        self._transport = transport or _urllib_transport
        self._clock = clock

    def capabilities(self):
        from dewako.exchanges.base import AdapterCapabilities
        # Only the API-key permission read is implemented (and has never run against Binance). Orders, fills, balances: NOT_IMPLEMENTED.
        return AdapterCapabilities(permissions_check=True, testnet_available=True)

    async def connect(self) -> None:
        if not self._key or not self._secret:
            raise NotConnected("Binance API key and secret are required")

    def signed_url(self, path: str) -> str:
        query = urllib.parse.urlencode({"timestamp": int(self._clock() * 1000), "recvWindow": 5000})
        sig = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return f"{BASE}{path}?{query}&signature={sig}"

    async def get_api_permissions(self) -> dict[str, bool]:
        if not self._key or not self._secret:
            raise NotConnected("Binance API key and secret are required")
        url = self.signed_url("/sapi/v1/account/apiRestrictions")
        data = await asyncio.to_thread(self._transport, url, {"X-MBX-APIKEY": self._key})
        if "enableWithdrawals" not in data:  # unexpected shape: do not guess
            raise ExchangeError("unexpected response from Binance while reading key permissions")
        return {"read": bool(data.get("enableReading")),
                "trade": bool(data.get("enableSpotAndMarginTrading")),
                "withdraw": bool(data.get("enableWithdrawals"))}
