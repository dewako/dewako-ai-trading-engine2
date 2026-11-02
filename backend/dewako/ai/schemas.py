"""Versioned structured output schemas and deterministic validation. Everything a model returns is an untrusted suggestion: it is parsed as
JSON, checked against a strict schema (unknown fields are errors), bounded in size, and only ever returned as advisory data."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dewako.ai.errors import AIError
from dewako.strategies.rule_builder import RuleSpec

MAX_RAW_BYTES = 16 * 1024
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(v: str) -> str:
    if _CTRL.search(v):
        raise ValueError("control characters are not allowed")
    return v


class MarketCommentaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    summary: str = Field(max_length=500)
    bias: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=1)
    risks: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("summary")
    @classmethod
    def _s(cls, v):
        return _text(v)

    @field_validator("risks")
    @classmethod
    def _r(cls, v):
        for x in v:
            if len(x) > 200:
                raise ValueError("a risk is longer than 200 characters")
            _text(x)
        return v


class StrategyIdeaV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    name: str = Field(max_length=40)
    rationale: str = Field(max_length=500)
    rule_spec: dict          # must validate as a declarative RuleSpec; it is a draft for a human to review, never registered automatically

    @field_validator("name", "rationale")
    @classmethod
    def _s(cls, v):
        return _text(v)


SCHEMAS: dict[tuple[str, int], type[BaseModel]] = {("market_commentary", 1): MarketCommentaryV1, ("strategy_idea", 1): StrategyIdeaV1}


def schema_hint(name: str, version: int) -> dict:
    return {"name": name, "version": version, "json_schema": SCHEMAS[(name, version)].model_json_schema()}


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def _nan(c):
    raise ValueError("non-finite number")


def validate_output(name: str, version: int, raw: str) -> dict:
    """Return the validated suggestion as a plain dict, or raise AIError(INVALID_OUTPUT)."""
    model = SCHEMAS.get((name, version))
    if model is None:
        raise AIError("MISCONFIGURED", f"unknown schema {name} v{version}")
    if not isinstance(raw, str) or len(raw.encode("utf-8", "replace")) > MAX_RAW_BYTES:
        raise AIError("INVALID_OUTPUT", "response missing or too large")
    text = raw.strip()
    m = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)     # one fenced block is tolerated; prose around JSON is not
    if m:
        text = m.group(1)
    try:
        obj = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_nan)
    except (ValueError, RecursionError):
        raise AIError("INVALID_OUTPUT", "not a single valid JSON object")
    if not isinstance(obj, dict):
        raise AIError("INVALID_OUTPUT", "top level must be an object")
    try:
        out = model.model_validate(obj)
    except ValidationError as e:
        raise AIError("INVALID_OUTPUT", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:300])
    data = out.model_dump()
    if isinstance(out, StrategyIdeaV1):
        try:
            data["rule_spec"] = RuleSpec.model_validate({**out.rule_spec, "name": out.name}).model_dump(mode="json")
        except ValidationError as e:
            raise AIError("INVALID_OUTPUT", "rule_spec: " + "; ".join(x["msg"] for x in e.errors())[:250])
    return data
