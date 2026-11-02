"""Pre-trade exchange validation (symbol, filters, notional, balance). Fail closed: anything unknown blocks the order.

Quantity is only ever rounded DOWN to the lot step, so validation can reduce an approved risk size but never increase it.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any


@dataclass
class Validated:
    quantity: float = 0.0
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QuoteGuardConfig:
    """Top-of-book limits; percentages are percent units (0.25 means 0.25%)."""

    max_spread_pct: float
    max_price_deviation_pct: float
    max_quote_age_seconds: float
    clock_skew_tolerance_seconds: float = 2.0
    enforce_spread: bool = True          # False ONLY for a triggered stop-loss exit (see ExecutionEngine.execute); freshness/validity are still enforced

    def __post_init__(self) -> None:
        for name in ("max_spread_pct", "max_price_deviation_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > 100:
                raise ValueError(f"{name} must be finite and in (0, 100]")
        if (isinstance(self.max_quote_age_seconds, bool) or not isinstance(self.max_quote_age_seconds, (int, float))
                or not math.isfinite(self.max_quote_age_seconds) or not 0 < self.max_quote_age_seconds <= 3600):
            raise ValueError("max_quote_age_seconds must be finite and in (0, 3600]")
        if (isinstance(self.clock_skew_tolerance_seconds, bool)
                or not isinstance(self.clock_skew_tolerance_seconds, (int, float))
                or not math.isfinite(self.clock_skew_tolerance_seconds)
                or not 0 <= self.clock_skew_tolerance_seconds <= 300):
            raise ValueError("clock_skew_tolerance_seconds must be finite and in [0, 300]")

    @classmethod
    def from_env(cls) -> "QuoteGuardConfig | None":
        names = {
            "max_spread_pct": "DEWAKO_MAX_SPREAD_PCT",
            "max_price_deviation_pct": "DEWAKO_MAX_PRICE_DEVIATION_PCT",
            "max_quote_age_seconds": "DEWAKO_MAX_QUOTE_AGE_SECONDS",
        }
        present = {field: os.environ.get(key, "").strip() for field, key in names.items()}
        configured = [value != "" for value in present.values()]
        if not any(configured):
            return None
        if not all(configured):
            missing = [names[field] for field, value in present.items() if not value]
            raise ValueError("quote guard configuration is incomplete; missing " + ", ".join(missing))
        skew = os.environ.get("DEWAKO_QUOTE_CLOCK_SKEW_SECONDS", "2")
        try:
            return cls(**{field: float(value) for field, value in present.items()},
                       clock_skew_tolerance_seconds=float(skew))
        except ValueError as exc:
            raise ValueError("quote guard configuration contains invalid numeric values") from exc


@dataclass(frozen=True)
class QuoteCheck:
    executable_price: float
    spread_pct: float
    deviation_pct: float
    timestamp: datetime


class QuoteGuardError(ValueError):
    pass


class WideSpreadError(QuoteGuardError):
    """The quote is otherwise valid (fresh, positive, uncrossed) but its spread exceeds the configured maximum.

    Carries the quote's own figures so a caller that has an explicit, bounded policy for it (the exit monitor's emergency exit) can
    evaluate stop/target triggers on the bid without re-parsing the quote. Everyone else treats it as the QuoteGuardError it is."""

    def __init__(self, message: str, *, bid: float, ask: float, spread_pct: float, timestamp: datetime):
        super().__init__(message)
        self.bid, self.ask, self.spread_pct, self.timestamp = bid, ask, spread_pct, timestamp


def validate_executable_quote(ticker: dict[str, Any], *, side: str, reference_price: float, now: datetime,
                              config: QuoteGuardConfig) -> QuoteCheck:
    """Validate a normalized top-of-book quote and price it at ask for BUY / bid for SELL."""
    if not isinstance(ticker, dict):
        raise QuoteGuardError("ticker response is missing or invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise QuoteGuardError("quote validation clock must be timezone-aware")
    if isinstance(reference_price, bool) or not isinstance(reference_price, (int, float)):
        raise QuoteGuardError("approved reference price is missing or invalid")
    if not math.isfinite(reference_price) or reference_price <= 0:
        raise QuoteGuardError("approved reference price must be positive and finite")

    def number(field_name: str) -> float:
        raw = ticker.get(field_name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise QuoteGuardError(f"quote {field_name} is missing or invalid")
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise QuoteGuardError(f"quote {field_name} must be positive and finite")
        return value

    bid, ask = number("bid"), number("ask")
    if ask < bid:
        raise QuoteGuardError("quote is crossed (ask is below bid)")
    raw_timestamp = ticker.get("timestamp")
    if isinstance(raw_timestamp, str):
        try:
            raw_timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            raw_timestamp = None
    if not isinstance(raw_timestamp, datetime) or raw_timestamp.tzinfo is None or raw_timestamp.utcoffset() is None:
        raise QuoteGuardError("quote timestamp is missing, invalid, or timezone-naive")
    timestamp = raw_timestamp.astimezone(timezone.utc)
    age = now.astimezone(timezone.utc) - timestamp
    if age < -timedelta(seconds=config.clock_skew_tolerance_seconds):
        raise QuoteGuardError("quote timestamp is future-dated beyond clock-skew tolerance")
    if age.total_seconds() > config.max_quote_age_seconds:
        raise QuoteGuardError("quote is stale")

    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    if config.enforce_spread and spread_pct > config.max_spread_pct:
        raise WideSpreadError(
            f"spread {spread_pct:.6g}% exceeds configured maximum {config.max_spread_pct:.6g}%",
            bid=bid, ask=ask, spread_pct=spread_pct, timestamp=timestamp)
    if side == "BUY":
        executable = ask
    elif side == "SELL":
        executable = bid
    else:
        raise QuoteGuardError("order side is invalid")
    deviation_pct = abs(executable - reference_price) / reference_price * 100
    if deviation_pct > config.max_price_deviation_pct:
        raise QuoteGuardError(
            f"price deviation {deviation_pct:.6g}% exceeds configured maximum "
            f"{config.max_price_deviation_pct:.6g}%"
        )
    return QuoteCheck(executable, spread_pct, deviation_pct, timestamp)


def conservative_risk_price(side: str, reference_price: float, check: QuoteCheck) -> float:
    """The single price basis for pre-trade notional, minimum-notional, available-balance, exposure and reservation arithmetic.

    BUY: the HIGHER of the approved reference and the executable ask (never understate what the order can cost).
    SELL: the LOWER of the approved reference and the executable bid (never overstate what the order is worth).
    Anything missing, non-numeric, non-finite or non-positive raises QuoteGuardError: nothing is guessed and the caller blocks the order.
    """
    if not isinstance(check, QuoteCheck):
        raise QuoteGuardError("executable quote check is missing")
    for label, value in (("reference price", reference_price), ("executable price", check.executable_price)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise QuoteGuardError(f"{label} is missing, non-finite or not positive")
    if side == "BUY":
        return max(float(reference_price), float(check.executable_price))
    if side == "SELL":
        return min(float(reference_price), float(check.executable_price))
    raise QuoteGuardError("order side is invalid")


def executable_risk_quantity(quantity: float, *, reference_price: float, risk_price: float, stop_price: float) -> float:
    """Compatibility wrapper: the arithmetic now lives in the Risk Engine (`dewako.risk.sizing`). A sizing refusal is reported as a
    QuoteGuardError so existing callers keep blocking the order."""
    from dewako.risk.sizing import SizingError, executable_entry_quantity
    try:
        return executable_entry_quantity(quantity, reference_price=reference_price, executable_price=risk_price, stop_price=stop_price)
    except SizingError as e:
        raise QuoteGuardError(str(e)) from None


def validate_order(markets: list[dict], balances: dict, *, symbol: str, side: str, quantity: float, price: float | None,
                   order_type: str = "MARKET") -> Validated:
    v = Validated()
    if not isinstance(markets, list) or not isinstance(balances, dict):
        v.problems.append("exchange markets or balances response is invalid")
        return v
    m = next((x for x in markets if x.get("symbol") == symbol), None)
    if m is None:
        v.problems.append(f"symbol {symbol} is not listed by the exchange")
        return v
    if m.get("enabled") is False:
        v.problems.append(f"market {symbol} is not enabled for trading")
    if m.get("order_types") and order_type not in m["order_types"]:
        v.problems.append(f"order type {order_type} is not allowed on {symbol}")
    if "step_size" in m:
        step = m["step_size"]
    else:
        precision = m.get("qty_precision")
        if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 12:
            step = None
        else:
            step = 10 ** -precision
    if (isinstance(step, bool) or not isinstance(step, (int, float))
            or not math.isfinite(step) or step <= 0):
        v.problems.append("exchange did not report a quantity step; refusing to guess")
        return v
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
        v.problems.append("quantity is invalid or non-finite")
        return v
    try:
        quantity_decimal, step_decimal = Decimal(str(quantity)), Decimal(str(step))
        q = float((quantity_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal)
    except (InvalidOperation, ValueError, OverflowError):
        v.problems.append("quantity or exchange lot size cannot be represented safely")
        return v
    if q <= 0:
        v.problems.append("quantity rounds down to zero at the exchange's lot size")
        return v
    min_qty, max_qty = m.get("min_qty"), m.get("max_qty")
    for label, value in (("minimum quantity", min_qty), ("maximum quantity", max_qty)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            v.problems.append(f"exchange {label} is invalid")
    if min_qty is not None and isinstance(min_qty, (int, float)) and math.isfinite(min_qty) and q < min_qty:
        v.problems.append(f"quantity {q} below the exchange minimum {min_qty}")
    if max_qty is not None and isinstance(max_qty, (int, float)) and math.isfinite(max_qty) and q > max_qty:
        v.problems.append(f"quantity {q} above the exchange maximum {max_qty}")
    if order_type != "MARKET":
        tick = m.get("tick_size")
        if (isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick) or tick <= 0
                or price is None or isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or abs(round(price / tick) * tick - price) > tick * 1e-6):
            v.problems.append("price is missing or not a multiple of the tick size")
    if (price is None or isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(price) or price <= 0):
        v.problems.append("a reference price is required to check notional and balance")
        return v
    notional = q * price
    min_notional = m.get("min_notional")
    if min_notional is not None and (isinstance(min_notional, bool) or not isinstance(min_notional, (int, float))
                                     or not math.isfinite(min_notional) or min_notional < 0):
        v.problems.append("exchange minimum notional is invalid")
    elif min_notional is not None and notional < min_notional:
        v.problems.append(f"notional {notional:.4f} below the exchange minimum {min_notional}")
    if side == "BUY":
        quote = symbol.split("/")[1]
        avail = balances.get(quote) if isinstance(balances, dict) else None
        fee = m.get("fee", 0.001)
        if (isinstance(fee, bool) or not isinstance(fee, (int, float))
                or not math.isfinite(fee) or fee < 0):
            v.problems.append("exchange fee rate is invalid")
        if (avail is None or isinstance(avail, bool) or not isinstance(avail, (int, float))
                or not math.isfinite(avail) or avail < 0):
            v.problems.append(f"no {quote} balance reported; refusing to guess")
        elif isinstance(fee, (int, float)) and math.isfinite(fee) and fee >= 0 and avail < notional * (1 + fee):
            v.problems.append(f"available {quote} {avail:.4f} is below the order cost")
    if not v.problems:
        v.quantity = q
    return v
