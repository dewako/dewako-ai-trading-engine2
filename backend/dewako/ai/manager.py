"""AI provider manager: modes, fallback policy, privacy, validation, circuit breaking.

Hard rules (each has a test): LOCAL_ONLY never consults a cloud provider; private data never goes to the cloud; every output is schema
validated and returned as an advisory suggestion; `ask` never raises, so an AI outage cannot interrupt anything else; nothing in
execution, risk, safety, exchanges or reconciliation imports this package, and this package imports none of them, so a model has no path to
an order, a limit or a switch.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dewako.ai.breaker import CircuitBreaker, RateLimiter
from dewako.ai.errors import AIError, PrivacyBlocked
from dewako.ai.prompts import build_prompt
from dewako.ai.providers import AIProvider
from dewako.ai.redaction import prepare
from dewako.ai.schemas import SCHEMAS, schema_hint, validate_output

MODES = ("LOCAL_ONLY", "CLOUD_ONLY", "HYBRID")


@dataclass
class AIConfig:
    mode: str = "LOCAL_ONLY"                 # default: nothing leaves the machine
    hybrid_primary: str = "local"            # which kind HYBRID tries first
    allow_cloud_fallback: bool = False       # HYBRID only: may a failed local call fall back to the cloud? explicit opt-in
    max_retries: int = 1                     # extra attempts for timeouts / unavailable only
    timeout: float = 20.0
    max_tokens: int = 800
    rate_per_minute: int = 30
    breaker_threshold: int = 3
    breaker_cooldown: float = 60.0

    def validate(self) -> "AIConfig":
        if self.mode not in MODES or self.hybrid_primary not in ("local", "cloud"):
            raise AIError("MISCONFIGURED", "unknown mode")
        if not (0 <= self.max_retries <= 3 and 0.1 <= self.timeout <= 120 and 16 <= self.max_tokens <= 4000 and 1 <= self.rate_per_minute <= 600):
            raise AIError("MISCONFIGURED", "limits out of range")
        return self


@dataclass
class AIResult:
    status: str                              # OK | UNAVAILABLE
    advisory: bool = True                    # always: a suggestion for a human or a validator, never an instruction
    provider: Optional[str] = None
    used_fallback: bool = False
    suggestion: Optional[dict] = None
    suspicious_input: bool = False
    attempts: list = field(default_factory=list)


class AIProviderManager:
    def __init__(self, config: AIConfig, providers: list[AIProvider], *, audit=None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.config = config.validate()
        self.providers = list(providers)
        self.audit, self.clock, self._sleep = audit, clock, sleep
        self._breakers = {p.name: CircuitBreaker(config.breaker_threshold, config.breaker_cooldown, clock) for p in providers}
        self._limiters = {p.name: RateLimiter(config.rate_per_minute, clock) for p in providers}

    def _order(self) -> list[tuple[AIProvider, bool]]:
        """(provider, is_fallback) in the order they may be tried. LOCAL_ONLY returns no cloud provider at all."""
        loc = [p for p in self.providers if p.kind == "local"]
        cld = [p for p in self.providers if p.kind == "cloud"]
        m = self.config.mode
        if m == "LOCAL_ONLY":
            return [(p, False) for p in loc]
        if m == "CLOUD_ONLY":
            return [(p, False) for p in cld]
        first, second = (loc, cld) if self.config.hybrid_primary == "local" else (cld, loc)
        out = [(p, False) for p in first]
        if self.config.allow_cloud_fallback or second is loc:        # falling back TO local needs no opt-in; falling back to the cloud does
            out += [(p, True) for p in second]
        return out

    def status(self) -> dict:
        return {"mode": self.config.mode, "providers": [{"name": p.name, "kind": p.kind, "breaker": self._breakers[p.name].state} for p in self.providers]}

    async def ask(self, task: str, schema: str, version: int, data: dict, *, correlation_id: str = "") -> AIResult:
        res = AIResult(status="UNAVAILABLE")
        try:
            if (schema, version) not in SCHEMAS:
                raise AIError("MISCONFIGURED", "unknown schema")
            prompt_cache: dict[str, tuple[str, bool]] = {}
            for provider, is_fb in self._order():
                dest = "cloud" if provider.kind == "cloud" else "local"
                try:
                    if dest not in prompt_cache:
                        clean, _ = prepare(data, dest)                     # raises PrivacyBlocked: no call is made
                        prompt_cache[dest] = build_prompt(task, schema_hint(schema, version), clean)
                    prompt, suspicious = prompt_cache[dest]
                    res.suspicious_input = res.suspicious_input or suspicious
                    out = await self._call(provider, prompt, schema, version)
                except AIError as e:
                    res.attempts.append({"provider": provider.name, "outcome": e.code})
                    continue
                res.status, res.provider, res.used_fallback, res.suggestion = "OK", provider.name, is_fb, out
                res.attempts.append({"provider": provider.name, "outcome": "OK"})
                break
        except Exception as e:                                             # never propagate: an AI problem must not become a trading problem
            res.attempts.append({"provider": None, "outcome": type(e).__name__ if not isinstance(e, AIError) else e.code})
        self._audit(res, schema, correlation_id)
        return res

    async def _call(self, provider: AIProvider, prompt: str, schema: str, version: int) -> dict:
        br, rl = self._breakers[provider.name], self._limiters[provider.name]
        if not br.allow():
            raise AIError("CIRCUIT_OPEN", provider.name)
        if not rl.allow():
            br.release()
            raise AIError("RATE_LIMITED", provider.name)
        last: Optional[AIError] = None
        for attempt in range(self.config.max_retries + 1):
            try:
                raw = await provider.complete(prompt, timeout=self.config.timeout, max_tokens=self.config.max_tokens)
                data = validate_output(schema, version, raw)               # invalid output is a failure, never retried: same prompt, same model
                br.success()
                return data
            except AIError as e:
                last = e
                if not e.retryable or attempt >= self.config.max_retries:
                    break
                await self._sleep(min(2.0 ** attempt * 0.25, 2.0))
            except Exception as e:                                         # a provider bug is a provider failure
                last = AIError("UNAVAILABLE", type(e).__name__)
                break
        br.failure()
        raise last or AIError("UNAVAILABLE")

    def _audit(self, res: AIResult, schema: str, cid: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("AI_REQUEST", res.status, cid, resource=f"ai:{schema}",
                              metadata={"provider": res.provider, "used_fallback": res.used_fallback, "attempts": res.attempts, "suspicious_input": res.suspicious_input,
                                        "mode": self.config.mode})       # never the prompt or the response text
        except Exception:
            pass
