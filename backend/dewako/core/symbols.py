"""Symbol normalization. Strategies only ever use BASE/QUOTE (e.g. BTC/USDT)."""
from __future__ import annotations

KNOWN_QUOTES = ("USDT", "USDC", "BUSD", "USD", "EUR", "BTC", "ETH")


def normalize(native: str, exchange: str) -> str:
    """Convert an exchange-native symbol to BASE/QUOTE."""
    s = native.strip().upper()
    if "/" in s:
        return s
    if exchange.lower() == "okx" or "-" in s:
        base, _, quote = s.partition("-")
        if base and quote:
            return f"{base}/{quote}"
    for q in KNOWN_QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return f"{s[: -len(q)]}/{q}"
    raise ValueError(f"cannot normalize symbol {native!r} for {exchange}")


def to_native(symbol: str, exchange: str) -> str:
    """Convert BASE/QUOTE to the exchange's own format."""
    base, sep, quote = symbol.upper().partition("/")
    if not sep or not base or not quote:
        raise ValueError(f"expected BASE/QUOTE, got {symbol!r}")
    return f"{base}-{quote}" if exchange.lower() == "okx" else f"{base}{quote}"
