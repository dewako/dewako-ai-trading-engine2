"""Explicit order state machine. Any transition not listed here is refused."""
from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"


S = OrderState
TERMINAL = {S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED}
# FAILED means "confirmed not accepted by the exchange" or "never sent". It is only left through a retry (see store).
UNRESOLVED = {S.SUBMITTING, S.UNKNOWN, S.RECONCILING}

TRANSITIONS: dict[OrderState, set[OrderState]] = {
    S.CREATED: {S.SUBMITTING, S.FAILED},
    S.SUBMITTING: {S.SUBMITTED, S.REJECTED, S.UNKNOWN, S.FAILED},
    S.SUBMITTED: {S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.CANCEL_REQUESTED, S.EXPIRED, S.UNKNOWN},
    S.OPEN: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    S.CANCEL_REQUESTED: {S.CANCELLED, S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.UNKNOWN},
    S.UNKNOWN: {S.RECONCILING},
    S.RECONCILING: {S.OPEN, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.FAILED, S.UNKNOWN, S.RECONCILING},
    S.FAILED: {S.SUBMITTING},  # retry; allowed only after the latest event is CONFIRMED_ABSENT (enforced in the store)
    S.FILLED: set(), S.CANCELLED: set(), S.REJECTED: set(), S.EXPIRED: set(),
}


class IllegalTransition(Exception):
    pass


def check(prev: OrderState, new: OrderState) -> None:
    if new not in TRANSITIONS[prev]:
        raise IllegalTransition(f"{prev.value} -> {new.value} is not allowed")
