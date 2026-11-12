"""Resolve 'py:module.attr' target strings to objects."""
from __future__ import annotations

import importlib
from typing import Any

from .errors import LoadError


def resolve(target: str) -> Any:
    """'py:pkg.mod.Class.method' -> the object, or LoadError."""
    if not str(target).startswith("py:"):
        raise LoadError(f"unsupported target {target!r}")
    parts = target[3:].split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
            break
        except ModuleNotFoundError:
            continue
    else:
        raise LoadError(f"target {target}: no importable module")
    for p in parts[i:]:
        if not hasattr(obj, p):
            raise LoadError(f"target {target}: {p!r} does not exist")
        obj = getattr(obj, p)
    return obj
