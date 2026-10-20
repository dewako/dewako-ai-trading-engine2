"""Server-side credential vault with key versions and rotation.

Keys come from ENCRYPTION_KEYS ("1:<fernet key>,2:<fernet key>", newest last used for new writes) or, for older setups, the
single ENCRYPTION_KEY (version 1). Old versions stay readable so data can be re-encrypted ("rotated") without downtime.
Errors never include key material or plaintext.
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


def _parse(spec: str) -> dict[int, bytes]:
    keys: dict[int, bytes] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        ver, _, k = part.partition(":")
        try:
            keys[int(ver)] = k.encode()
            Fernet(k.encode())
        except (ValueError, TypeError):
            raise VaultError("ENCRYPTION_KEYS must look like '1:<fernet key>,2:<fernet key>' with valid Fernet keys") from None
    return keys


class CredentialVault:
    def __init__(self, key: str | bytes | None = None, keys: dict[int, str | bytes] | None = None):
        if keys:
            parsed = {int(v): (k.encode() if isinstance(k, str) else k) for v, k in keys.items()}
        elif key:
            parsed = {1: key.encode() if isinstance(key, str) else key}
        elif os.environ.get("ENCRYPTION_KEYS"):
            parsed = _parse(os.environ["ENCRYPTION_KEYS"])
        elif os.environ.get("ENCRYPTION_KEY"):
            parsed = {1: os.environ["ENCRYPTION_KEY"].encode()}
        else:
            raise VaultError("ENCRYPTION_KEYS (or ENCRYPTION_KEY) is not set. Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        try:
            self._f = {v: Fernet(k) for v, k in parsed.items()}
        except (ValueError, TypeError):
            raise VaultError("an encryption key is not a valid 32-byte urlsafe base64 Fernet key") from None
        if not self._f:
            raise VaultError("no encryption keys configured")

    @property
    def current_version(self) -> int:
        return max(self._f)

    def encrypt(self, payload: dict[str, str]) -> bytes:
        return self._f[self.current_version].encrypt(json.dumps(payload).encode())

    def decrypt(self, blob: bytes, version: int | None = None) -> dict[str, str]:
        order = [version] if version in self._f else sorted(self._f, reverse=True)
        for v in order:
            try:
                return json.loads(self._f[v].decrypt(blob))
            except InvalidToken:
                continue
        raise VaultError("credential blob cannot be decrypted with the configured keys")

    def rotate(self, blob: bytes, version: int | None = None) -> tuple[bytes, int]:
        """Re-encrypt under the newest key. Returns (new_blob, new_version)."""
        return self.encrypt(self.decrypt(blob, version)), self.current_version


def mask(value: str) -> str:
    """Show only the last 4 characters, e.g. '••••••••a1b2'."""
    return "\u2022" * 8 + value[-4:] if len(value) > 4 else "\u2022" * 8
