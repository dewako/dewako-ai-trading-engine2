"""Code plugin loader. OFF by default.

A code plugin is an arbitrary Python file that runs with the full privileges of the server
process (it could read the credential vault's key from the environment). Enable it only on
a machine you control, for files you wrote or fully reviewed. Never expose it to uploads
from other users.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


class PluginsDisabled(Exception):
    pass


def code_plugins_enabled() -> bool:
    return os.environ.get("DEWAKO_ALLOW_CODE_PLUGINS") == "1"


def load_code_plugins(directory: str | Path) -> list[str]:
    """Import every non-underscore .py file in `directory`. Strategies register themselves."""
    if not code_plugins_enabled():
        raise PluginsDisabled("code plugins are disabled. Set DEWAKO_ALLOW_CODE_PLUGINS=1 on a trusted machine")
    loaded = []
    for path in sorted(Path(directory).glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"dewako_plugin_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # raises if the plugin registers a duplicate name+version
        loaded.append(path.name)
    return loaded
