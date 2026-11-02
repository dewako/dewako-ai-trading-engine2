"""Circuit breaker and token-bucket rate limiter with injectable clocks (deterministic tests)."""
from __future__ import annotations

import time
from typing import Callable


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures, self.opened_at, self._probing = 0, None, False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self.clock() - self.opened_at >= self.cooldown else "OPEN"

    def allow(self) -> bool:
        s = self.state
        if s == "CLOSED":
            return True
        if s == "HALF_OPEN" and not self._probing:
            self._probing = True                  # exactly one trial request while half open
            return True
        return False

    def release(self) -> None:
        """The permitted probe was not actually sent (e.g. rate limited): free the slot."""
        self._probing = False

    def success(self) -> None:
        self.failures, self.opened_at, self._probing = 0, None, False

    def failure(self) -> None:
        self.failures += 1
        self._probing = False
        if self.failures >= self.threshold or self.opened_at is not None:
            self.opened_at = self.clock()


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.capacity, self.clock = max(1, per_minute), clock
        self.tokens, self.last = float(self.capacity), clock()

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.capacity / 60.0)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
