"""Strategy plugin contract. Strategies see normalized MarketState only."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from dewako.core.types import MarketState, Signal


class StrategyParameterError(ValueError):
    pass


class Strategy(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str] = ""
    supported_markets: ClassVar[list[str]] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    supported_timeframes: ClassVar[list[str]] = ["15m", "1h", "4h"]
    risk_profile: ClassVar[str] = "Medium"
    default_parameters: ClassVar[dict[str, Any]] = {}
    # Allowed (min, max) for numeric parameters. Anything outside is refused before the strategy can run.
    parameter_ranges: ClassVar[dict[str, tuple[float, float]]] = {}
    experimental: ClassVar[bool] = False  # experimental strategies can never be activated live

    def __init__(self, **overrides: Any):
        unknown = set(overrides) - set(self.default_parameters)
        if unknown:
            raise StrategyParameterError(f"unknown parameters: {sorted(unknown)}")
        self.parameters = {**self.default_parameters, **overrides}
        for key, (lo, hi) in self.parameter_ranges.items():
            v = self.parameters.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
                raise StrategyParameterError(f"{key} must be a number between {lo} and {hi}, got {v!r}")
        self.validate_parameters(self.parameters)

    def validate_parameters(self, params: dict[str, Any]) -> None:  # override as needed
        return None

    @abstractmethod
    def generate_signal(self, state: MarketState) -> Optional[Signal]:
        """Return a Signal or None. Must be a pure function of `state` and parameters."""

    def manage_position(self, state: MarketState, position: dict[str, Any]) -> Optional[Signal]:
        """Optionally return an exit/adjust signal for an open position."""
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "supported_markets": self.supported_markets,
            "supported_timeframes": self.supported_timeframes,
            "parameters": self.parameters,
            "risk_profile": self.risk_profile,
            "experimental": self.experimental,
            "parameter_ranges": {k: list(v) for k, v in self.parameter_ranges.items()},
        }


_REGISTRY: dict[tuple[str, str], type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    key = (cls.name, cls.version)
    if key in _REGISTRY:
        raise ValueError(f"strategy {cls.name} v{cls.version} already registered; versions are immutable")
    _REGISTRY[key] = cls
    return cls


def get(name: str, version: str) -> type[Strategy]:
    return _REGISTRY[(name, version)]


def all_strategies() -> list[type[Strategy]]:
    return list(_REGISTRY.values())


def _vkey(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def versions(name: str) -> list[str]:
    """All registered versions of a strategy, oldest first."""
    return sorted((v for (n, v) in _REGISTRY if n == name), key=_vkey)


def latest(name: str) -> type[Strategy]:
    vs = versions(name)
    if not vs:
        raise KeyError(name)
    return _REGISTRY[(name, vs[-1])]


def names() -> list[str]:
    return sorted({n for (n, _) in _REGISTRY})
