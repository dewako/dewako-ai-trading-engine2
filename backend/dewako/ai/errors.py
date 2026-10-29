"""Normalized AI errors. `retryable` is only about re-asking a read-only inference; it has no bearing on trading."""
from __future__ import annotations


class AIError(Exception):
    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.retryable = code, detail, retryable


class PrivacyBlocked(AIError):
    def __init__(self, detail: str = ""):
        super().__init__("PRIVACY_BLOCKED", detail)
