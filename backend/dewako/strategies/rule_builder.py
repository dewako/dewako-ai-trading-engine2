"""No-code strategy builder.

A user describes entry rules as data. Only whitelisted condition types exist, so no user
text is ever executed: there is no eval, exec or import involved.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from dewako.core.types import MarketState, Side, Signal
from dewako.strategies.base import Strategy, register
from dewako.strategies.indicators import atr, ema, rsi

ConditionType = Literal[
    "price_above_ema", "price_below_ema", "ema_fast_above_slow", "ema_fast_below_slow",
    "rsi_below", "rsi_above", "volume_ratio_above",
]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{2,39}$")
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}$")


class Condition(BaseModel):
    type: ConditionType
    length: int = Field(20, ge=2, le=400)       # EMA / RSI length, or the fast EMA length
    slow_length: int = Field(50, ge=3, le=800)  # only for ema_fast_* conditions
    value: float = Field(0.0, ge=0, le=100)     # RSI threshold or volume multiple

    @model_validator(mode="after")
    def _check(self):
        if self.type.startswith("ema_fast") and self.length >= self.slow_length:
            raise ValueError("fast length must be smaller than slow length")
        if self.type in ("rsi_below", "rsi_above") and not 1 <= self.value <= 99:
            raise ValueError("RSI threshold must be between 1 and 99")
        if self.type == "volume_ratio_above" and self.value <= 0:
            raise ValueError("volume multiple must be positive")
        return self

    def describe(self) -> str:
        t = self.type
        if t == "price_above_ema": return f"Price above EMA {self.length}"
        if t == "price_below_ema": return f"Price below EMA {self.length}"
        if t == "ema_fast_above_slow": return f"EMA {self.length} above EMA {self.slow_length}"
        if t == "ema_fast_below_slow": return f"EMA {self.length} below EMA {self.slow_length}"
        if t == "rsi_below": return f"RSI {self.length} below {self.value:g}"
        if t == "rsi_above": return f"RSI {self.length} above {self.value:g}"
        return f"Volume above {self.value:g}x average"

    def holds(self, state: MarketState) -> bool:
        closes = [c.c for c in state.candles]
        t = self.type
        need = max(self.length, self.slow_length if t.startswith("ema_fast") else 0) + 2
        if len(closes) < need:
            return False
        if t == "price_above_ema": return closes[-1] > ema(closes, self.length)[-1]
        if t == "price_below_ema": return closes[-1] < ema(closes, self.length)[-1]
        if t == "ema_fast_above_slow": return ema(closes, self.length)[-1] > ema(closes, self.slow_length)[-1]
        if t == "ema_fast_below_slow": return ema(closes, self.length)[-1] < ema(closes, self.slow_length)[-1]
        if t == "rsi_below": return rsi(closes, self.length) < self.value
        if t == "rsi_above": return rsi(closes, self.length) > self.value
        return state.volume_ratio > self.value


class RuleSpec(BaseModel):
    name: str
    version: str = "1.0"
    description: str = Field("", max_length=300)
    risk_profile: Literal["Low", "Medium", "Medium-High", "High"] = "Medium"
    entry_long: list[Condition] = Field(default_factory=list, max_length=5)
    entry_short: list[Condition] = Field(default_factory=list, max_length=5)
    atr_len: int = Field(14, ge=2, le=100)
    atr_stop: float = Field(2.0, ge=0.5, le=10)
    take_profit_rr: Optional[float] = Field(None, gt=0, le=10)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("name must be 3-40 characters: letters, digits, spaces, - or _")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v):
        if not VERSION_RE.match(v):
            raise ValueError("version must look like 1.0")
        return v

    @model_validator(mode="after")
    def _has_rules(self):
        if not self.entry_long and not self.entry_short:
            raise ValueError("add at least one entry condition for long or short")
        return self

    @property
    def parameter_count(self) -> int:
        return 2 + sum(3 for _ in self.entry_long + self.entry_short)  # atr_len, atr_stop + per-condition numbers


def build_strategy_class(spec: RuleSpec, do_register: bool = True) -> type[Strategy]:
    longs, shorts = list(spec.entry_long), list(spec.entry_short)

    def generate_signal(self, state: MarketState):
        side, conds = None, []
        if longs and all(c.holds(state) for c in longs):
            side, conds = Side.BUY, longs
        elif shorts and all(c.holds(state) for c in shorts):
            side, conds = Side.SELL, shorts
        if side is None:
            return None
        price = state.candles[-1].c
        dist = self.parameters["atr_stop"] * atr(state.candles, self.parameters["atr_len"])
        stop = price - dist if side is Side.BUY else price + dist
        tp = None
        if spec.take_profit_rr:
            tp = price + dist * spec.take_profit_rr * (1 if side is Side.BUY else -1)
        return Signal(symbol=state.symbol, side=side, confidence=min(0.9, 0.5 + 0.08 * len(conds)),
                      strategy=self.name, strategy_version=self.version, reason=[c.describe() for c in conds],
                      entry_price=price, stop_price=stop, take_profit_price=tp)

    cls = type(
        re.sub(r"\W", "", spec.name) or "RuleStrategy",
        (Strategy,),
        {"name": spec.name, "version": spec.version, "description": spec.description or "User-built rule strategy",
         "risk_profile": spec.risk_profile, "default_parameters": {"atr_len": spec.atr_len, "atr_stop": spec.atr_stop},
         "spec": spec, "generate_signal": generate_signal,
         "directions": tuple(d for d, on in (("LONG", longs), ("SHORT", shorts)) if on)},
    )
    return register(cls) if do_register else cls
