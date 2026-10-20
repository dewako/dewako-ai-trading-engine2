"""Strategy version lock.

A (name, version) pair is a promise that the behaviour will never change, because backtests and
audit records refer to it. The lock file stores a fingerprint of each version the first time it is
seen. If the code or default parameters later differ under the same version, verification fails and
the change must ship as a new version number.

The fingerprint hashes the class source, so even a reformat counts as a change. That is deliberate:
it is cheaper to bump a version than to wonder whether an old backtest still means what it did.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

from dewako.strategies.base import Strategy


class StrategyVersionConflict(Exception):
    pass


def fingerprint(cls: type[Strategy]) -> str:
    spec = getattr(cls, "spec", None)  # rule-builder strategies are defined by their spec, not by source code
    if spec is not None:
        body = spec.model_dump_json()
    else:
        try:
            body = inspect.getsource(cls)
        except (OSError, TypeError):
            body = f"{cls.__module__}.{cls.__qualname__}"
    payload = json.dumps({"name": cls.name, "version": cls.version, "params": cls.default_parameters,
                          "ranges": {k: list(v) for k, v in cls.parameter_ranges.items()}, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class StrategyVersionLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def verify(self, strategies: list[type[Strategy]]) -> list[str]:
        """Record unseen versions and raise if a known version no longer matches. Returns newly recorded keys."""
        known, new, bad = self._load(), [], []
        for cls in strategies:
            key, fp = f"{cls.name} v{cls.version}", fingerprint(cls)
            if key not in known:
                known[key] = fp
                new.append(key)
            elif known[key] != fp:
                bad.append(key)
        if bad:
            raise StrategyVersionConflict(
                "behaviour changed under an existing version: " + ", ".join(bad) + ". Publish the change as a new version.")
        if new:
            self.path.write_text(json.dumps(known, indent=2, sort_keys=True))
        return new
