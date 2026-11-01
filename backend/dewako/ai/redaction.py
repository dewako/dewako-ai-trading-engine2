"""Data classification and redaction.

Classes: PUBLIC (market data, indicator values, strategy rule text) may go anywhere. PRIVATE (balances, positions, order ids, account or user
identifiers) may go to a LOCAL model only. SECRET (keys, tokens, passwords, anything shaped like one) is removed everywhere and never
reaches any model. For a CLOUD destination PRIVATE data is refused outright rather than silently stripped, so a caller cannot think it sent
more than it did.
"""
from __future__ import annotations

import re
from typing import Any

from dewako.ai.errors import PrivacyBlocked

SECRET_KEY = re.compile(r"(secret|password|passwd|token|api_?key|passphrase|authorization|private_?key|credential|seed|mnemonic|cookie|session)", re.I)
PRIVATE_KEY = re.compile(r"(balance|equity|account|position|order_?id|client_order|username|user_?id|email|wallet|address|exchange_order|pnl|holdings)", re.I)
SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}|[A-Fa-f0-9]{40,}|[A-Za-z0-9+/_\-]{40,}={0,2}|"
                          r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MASK = "[redacted]"
MAX_DEPTH = 6


def _walk(o: Any, depth: int, secret_found: list, private_found: list, path: str = "") -> Any:
    if depth > MAX_DEPTH:
        return "[truncated]"
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            ks = str(k)
            if SECRET_KEY.search(ks):
                secret_found.append(path + ks)
                out[ks] = MASK
                continue
            if PRIVATE_KEY.search(ks):
                private_found.append(path + ks)
            out[ks] = _walk(v, depth + 1, secret_found, private_found, path + ks + ".")
        return out
    if isinstance(o, (list, tuple)):
        return [_walk(v, depth + 1, secret_found, private_found, path) for v in o[:200]]
    if isinstance(o, str):
        s = o
        if SECRET_VALUE.search(s):
            secret_found.append(path or "value")
            s = SECRET_VALUE.sub(MASK, s)
        if EMAIL.search(s):
            private_found.append(path or "value")
        return s
    if o is None or isinstance(o, (int, float, bool)):
        return o
    return str(o)


def prepare(payload: dict, destination: str) -> tuple[dict, dict]:
    """Return (clean_payload, report). destination is 'local' or 'cloud'. Raises PrivacyBlocked for private data bound for the cloud."""
    if destination not in ("local", "cloud"):
        raise ValueError("destination must be local or cloud")
    secret: list = []
    private: list = []
    clean = _walk(payload, 0, secret, private)
    report = {"secrets_removed": len(secret), "private_fields": sorted(set(private))[:20], "destination": destination}
    if destination == "cloud" and private:
        raise PrivacyBlocked(f"private fields cannot be sent to a cloud provider: {report['private_fields'][:5]}")
    if destination == "local" and private:
        clean = _mask_emails(clean)
    return clean, report


def _mask_emails(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _mask_emails(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask_emails(v) for v in o]
    return EMAIL.sub(MASK, o) if isinstance(o, str) else o
