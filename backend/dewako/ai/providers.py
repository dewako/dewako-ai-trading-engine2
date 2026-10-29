"""Provider-neutral model interface and adapters.

A provider turns a prompt string into a response string. That is the whole surface: no tools, no function calling, no access to anything
else in the system. Local adapters speak the Ollama and LM Studio (OpenAI-compatible) HTTP APIs; the cloud adapter speaks a generic
OpenAI-compatible API. None of them has been run against a real server in this build (no network); they are exercised through an
injected transport.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlparse

from dewako.ai.errors import AIError

MAX_RESPONSE_BYTES = 64 * 1024
Transport = Callable[[str, dict, dict, float], tuple[int, bytes]]       # (url, json_body, headers, timeout) -> (status, body)


def validate_endpoint(kind: str, url: str) -> str:
    """Local endpoints must be loopback or private literals (no DNS involved, so 'local-only' cannot be redirected to the internet by a name).
    Cloud endpoints must be https with no embedded credentials and must not be private."""
    u = urlparse(url)
    if u.username or u.password or not u.hostname or u.scheme not in ("http", "https"):
        raise AIError("MISCONFIGURED", "endpoint must be an http(s) URL without embedded credentials")
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
        private = ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        ip, private = None, host == "localhost"
    if kind == "local" and not private:
        raise AIError("MISCONFIGURED", "a local endpoint must be localhost or a private/loopback IP address")
    if kind == "cloud":
        if u.scheme != "https":
            raise AIError("MISCONFIGURED", "a cloud endpoint must use https")
        if private or host.endswith(".local") or host.endswith(".internal"):
            raise AIError("MISCONFIGURED", "a cloud endpoint must not be a private address")
    return url.rstrip("/")


def urllib_transport(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:                       # noqa: S310 (scheme validated above)
            return r.status, r.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2048)
    except TimeoutError:
        raise
    except (urllib.error.URLError, OSError) as e:
        if "timed out" in str(e):
            raise TimeoutError("timed out")
        raise ConnectionError("connection failed")


class AIProvider:
    kind = "local"
    name = "base"

    async def complete(self, prompt: str, *, timeout: float, max_tokens: int = 800) -> str:
        raise NotImplementedError


class _HTTPProvider(AIProvider):
    def __init__(self, name: str, endpoint: str, model: str, transport: Optional[Transport] = None, key_supplier: Optional[Callable[[], str]] = None):
        self.name, self.model = name, model
        self.endpoint = validate_endpoint(self.kind, endpoint)
        self._transport = transport or urllib_transport
        self._key = key_supplier                         # the key is fetched at call time and never stored on the object or logged

    def _request(self, prompt: str, max_tokens: int) -> tuple[str, dict]:
        raise NotImplementedError

    def _parse(self, body: dict) -> str:
        raise NotImplementedError

    def __repr__(self):
        return f"<{type(self).__name__} {self.name} {self.endpoint} model={self.model}>"

    async def complete(self, prompt: str, *, timeout: float, max_tokens: int = 800) -> str:
        url, body = self._request(prompt, max_tokens)
        headers = {}
        if self._key is not None:
            k = self._key()
            if not k:
                raise AIError("MISCONFIGURED", "no credential stored for this provider")
            headers["Authorization"] = f"Bearer {k}"
        try:
            status, raw = await asyncio.wait_for(asyncio.to_thread(self._transport, url, body, headers, timeout), timeout + 1.0)
        except (TimeoutError, asyncio.TimeoutError):
            raise AIError("TIMEOUT", "provider timed out", retryable=True)
        except (ConnectionError, OSError):
            raise AIError("UNAVAILABLE", "provider unreachable", retryable=True)
        if status == 429 or status >= 500:
            raise AIError("UNAVAILABLE", f"provider status {status}", retryable=True)
        if status != 200:
            raise AIError("MISCONFIGURED", f"provider status {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AIError("OVERSIZE", "provider response too large")
        try:
            return self._parse(json.loads(raw))
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError("INVALID_OUTPUT", "provider returned an unexpected envelope")


class OllamaProvider(_HTTPProvider):
    kind = "local"

    def _request(self, prompt, max_tokens):
        return self.endpoint + "/api/chat", {"model": self.model, "stream": False, "messages": [{"role": "user", "content": prompt}],
                                              "options": {"num_predict": max_tokens, "temperature": 0}}

    def _parse(self, body):
        return str(body["message"]["content"])


class LMStudioProvider(_HTTPProvider):
    kind = "local"

    def _request(self, prompt, max_tokens):
        return self.endpoint + "/v1/chat/completions", {"model": self.model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0}

    def _parse(self, body):
        return str(body["choices"][0]["message"]["content"])


class OpenAICompatCloudProvider(LMStudioProvider):
    """Generic cloud provider using the widespread chat-completions shape. Requires a key from the vault."""
    kind = "cloud"


class MockProvider(AIProvider):
    """Deterministic provider for tests. `script` is a list of responses or exceptions consumed in order (the last repeats)."""

    def __init__(self, name: str, kind: str, script):
        self.name, self.kind, self.script, self.calls, self.prompts = name, kind, list(script), 0, []

    async def complete(self, prompt, *, timeout, max_tokens=800):
        self.prompts.append(prompt)
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item
