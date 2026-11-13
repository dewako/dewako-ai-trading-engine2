"""Send the engine's own log lines (loggers under "dewako") to stderr, next to the web server's lines.

Without this the application never configured logging: Python then drops INFO lines and prints WARNING and above with no
time stamp, so a quiet engine and a stuck engine looked the same in the log file. Level: DEWAKO_LOG_LEVEL (default INFO).
Idempotent; never logs settings or secrets."""
from __future__ import annotations

import logging
import os
import sys

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_MARK = "_dewako_handler"


def configure_logging(env=None) -> logging.Logger:
    e = os.environ if env is None else env
    level = (e.get("DEWAKO_LOG_LEVEL") or "INFO").strip().upper()
    if level not in _LEVELS:
        raise ValueError(f"DEWAKO_LOG_LEVEL must be one of {', '.join(_LEVELS)}")
    logger = logging.getLogger("dewako")
    logger.setLevel(level)
    if not any(getattr(h, _MARK, False) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        setattr(h, _MARK, True)
        logger.addHandler(h)
    return logger
