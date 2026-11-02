"""Entry sizing arithmetic that belongs to the Risk Engine (Stage 2 WP1). Pure functions: no I/O, no clock, no database.

The Risk Engine approves a quantity from the signal's REFERENCE price: approved loss at the stop = quantity x (reference - stop). An order
can fill at the executable ask, so the real loss at the stop is quantity x (executable - stop). `executable_entry_quantity` scales the
approved quantity down (never up) so the loss at the stop cannot exceed what was approved. Every invalid input raises SizingError and
the caller must treat that as a refusal; nothing is guessed.
"""
from __future__ import annotations

import math

REL_TOL = 1e-9          # equal up to floating-point noise: 1e-9 of the risk budget


class SizingError(ValueError):
    """The entry cannot be sized safely. The order must not be sent."""


def _positive(label: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise SizingError(f"{label} is missing, non-finite or not positive; entry risk cannot be verified")
    return float(value)


def executable_entry_quantity(quantity: float, *, reference_price: float, executable_price: float, stop_price: float) -> float:
    q = _positive("approved quantity", quantity)
    ref = _positive("reference price", reference_price)
    ex = _positive("executable entry price", executable_price)
    stop = _positive("stop price", stop_price)
    if stop >= ex:
        raise SizingError(f"stop {stop:g} is at or above the executable entry price {ex:g}; entry blocked")
    reference_distance = ref - stop
    if reference_distance <= 0:
        raise SizingError(f"stop {stop:g} is at or above the reference price {ref:g}; entry blocked")
    executable_distance = ex - stop
    if executable_distance <= reference_distance * (1 + REL_TOL):
        return q
    return q * reference_distance / executable_distance


def loss_at_stop(quantity: float, price: float, stop_price: float) -> float:
    return float(quantity) * (float(price) - float(stop_price))
