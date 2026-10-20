"""Pre-trade exchange validation (symbol, filters, notional, balance). Fail closed: anything unknown blocks the order.

Quantity is only ever rounded DOWN to the lot step, so validation can reduce an approved risk size but never increase it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Validated:
    quantity: float = 0.0
    problems: list[str] = field(default_factory=list)


def validate_order(markets: list[dict], balances: dict, *, symbol: str, side: str, quantity: float, price: float | None,
                   order_type: str = "MARKET") -> Validated:
    v = Validated()
    m = next((x for x in markets if x.get("symbol") == symbol), None)
    if m is None:
        v.problems.append(f"symbol {symbol} is not listed by the exchange")
        return v
    if m.get("enabled") is False:
        v.problems.append(f"market {symbol} is not enabled for trading")
    if m.get("order_types") and order_type not in m["order_types"]:
        v.problems.append(f"order type {order_type} is not allowed on {symbol}")
    step = m.get("step_size") or (10 ** -int(m["qty_precision"]) if m.get("qty_precision") is not None else None)
    if not step:
        v.problems.append("exchange did not report a quantity step; refusing to guess")
        return v
    q = math.floor(quantity / step + 1e-9) * step
    q = round(q, 12)
    if q <= 0:
        v.problems.append("quantity rounds down to zero at the exchange's lot size")
        return v
    if m.get("min_qty") and q < m["min_qty"]:
        v.problems.append(f"quantity {q} below the exchange minimum {m['min_qty']}")
    if m.get("max_qty") and q > m["max_qty"]:
        v.problems.append(f"quantity {q} above the exchange maximum {m['max_qty']}")
    if order_type != "MARKET":
        tick = m.get("tick_size")
        if price is None or not tick or abs(round(price / tick) * tick - price) > tick * 1e-6:
            v.problems.append("price is missing or not a multiple of the tick size")
    if price is None or not math.isfinite(price) or price <= 0:
        v.problems.append("a reference price is required to check notional and balance")
        return v
    notional = q * price
    if m.get("min_notional") and notional < m["min_notional"]:
        v.problems.append(f"notional {notional:.4f} below the exchange minimum {m['min_notional']}")
    if side == "BUY":
        quote = symbol.split("/")[1]
        avail = balances.get(quote) if isinstance(balances, dict) else None
        if avail is None:
            v.problems.append(f"no {quote} balance reported; refusing to guess")
        elif avail < notional * (1 + float(m.get("fee", 0.001))):
            v.problems.append(f"available {quote} {avail:.4f} is below the order cost")
    v.quantity = q
    return v
