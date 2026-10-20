"""Shared JSON-over-HTTPS transport. Providers take a transport so tests never touch the network."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from dewako.market_data.provider import ProviderError, ProviderRateLimited

Transport = Callable[[str], Any]  # url -> parsed JSON


def urllib_transport(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "dewako/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (418, 429):
            raise ProviderRateLimited(f"rate limited by {url.split('/')[2]} (HTTP {e.code})") from e
        raise ProviderError(f"HTTP {e.code} from {url.split('/')[2]}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ProviderError(f"could not fetch from {url.split('/')[2]}: {e}") from e
