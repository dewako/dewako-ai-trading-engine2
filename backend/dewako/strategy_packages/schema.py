"""Package manifest schema, version 1.

A package is DATA: a manifest, a declarative rule spec (the same RuleSpec the no-code builder uses), parameter overrides, metadata and
optional test vectors. There is no code runtime. `runtime` must be "declarative-rules"; any other value (python, pine, wasm, ...) is refused
because this build has no sandbox, and a Python subprocess is not one.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1
SUPPORTED_RUNTIMES = ("declarative-rules",)
# Documented future contract only. Nothing in this build runs these.
FUTURE_RUNTIMES = {"python": "needs strong isolation (not available)", "pine": "Pine Script does not run natively; see docs/STRATEGY_PACKAGES.md (Dewako Algo Studio contract)",
                   "wasm": "needs a reviewed sandbox runtime"}
TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")
INSTRUMENT_RE = re.compile(r"^[A-Z0-9]{2,12}(/[A-Z0-9]{2,12})?$")      # BTC/USDT, XAUUSD
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{2,39}$")
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_PARAMETER_KEYS = ("atr_len", "atr_stop", "take_profit_rr")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")      # an unknown field (install_script, hooks, entrypoint args...) is an error, not ignored
    schema_version: Literal[1]
    name: str
    version: str
    runtime: str
    entry: Literal["strategy/rules.json"] = "strategy/rules.json"
    description: str = Field("", max_length=300)
    author: str = Field("", max_length=80)
    instruments: list[str] = Field(min_length=1, max_length=10)
    timeframes: list[str] = Field(min_length=1, max_length=8)
    dependencies: list[str] = Field(default_factory=list, max_length=0)             # allowlist is empty: no dependency is installable
    requested_capabilities: list[str] = Field(default_factory=list, max_length=0)   # network / credentials / filesystem: never granted
    files: dict[str, str]                                                           # path -> sha256 of every file except manifest.json

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("name must be 3-40 characters: letters, digits, spaces, - or _")
        return v

    @field_validator("version")
    @classmethod
    def _v(cls, v):
        if not VERSION_RE.match(v):
            raise ValueError("version must look like 1.0")
        return v

    @field_validator("instruments")
    @classmethod
    def _i(cls, v):
        for s in v:
            if not INSTRUMENT_RE.match(s):
                raise ValueError(f"invalid instrument {s!r}")
        return v

    @field_validator("timeframes")
    @classmethod
    def _t(cls, v):
        for t in v:
            if t not in TIMEFRAMES:
                raise ValueError(f"unsupported timeframe {t!r}")
        return v

    @field_validator("files")
    @classmethod
    def _f(cls, v):
        if len(v) > 40:
            raise ValueError("too many files listed")
        for p, h in v.items():
            if not SHA256_RE.match(h):
                raise ValueError(f"bad sha256 for {p}")
        return v
