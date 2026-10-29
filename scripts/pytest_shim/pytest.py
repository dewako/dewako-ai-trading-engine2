"""Minimal stand-in for the parts of pytest this repo's tests use, ONLY for sandboxes where pytest cannot be installed.
It is not pytest: real pytest remains the supported runner (CI). Used by scripts/run_tests_standalone.py.
Supported: raises(exc, match=...), mark.parametrize, fixture (no-op decorator), skip; monkeypatch is injected by the runner."""
import contextlib
import re


class _Info:
    value = None


@contextlib.contextmanager
def raises(exc, match=None):
    info = _Info()
    try:
        yield info
    except exc as e:
        if match is not None and not re.search(match, str(e)):
            raise AssertionError(f"raised {type(e).__name__}({e}) but it does not match {match!r}") from None
        info.value = e
        return
    raise AssertionError(f"did not raise {exc}")


class _Mark:
    @staticmethod
    def parametrize(names, values, **_):
        names = [n.strip() for n in names.split(",")] if isinstance(names, str) else list(names)

        def deco(fn):
            fn.__dict__.setdefault("_params", []).append((names, list(values)))
            return fn
        return deco


mark = _Mark()


def fixture(*a, **k):
    if a and callable(a[0]):
        return a[0]
    return lambda fn: fn


def skip(reason=""):
    import unittest
    raise unittest.SkipTest(reason)


class approx:
    """float comparison with pytest's default tolerance (rel 1e-6, abs 1e-12)."""
    def __init__(self, expected, rel=1e-6, abs=1e-12):
        self.expected, self.rel, self.abs = expected, rel, abs

    def __eq__(self, other):
        return abs(other - self.expected) <= max(self.rel * abs_(self.expected), self.abs)

    def __repr__(self):
        return f"approx({self.expected})"


abs_ = abs
