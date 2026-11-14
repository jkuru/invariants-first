"""Resolve 'py:module.attr' target strings to objects."""
from __future__ import annotations

from typing import Any

from .confinement import import_module
from .errors import LoadError


def resolve(target: str) -> Any:
    """'py:pkg.mod.Class.method' -> the object, or LoadError."""
    if not str(target).startswith("py:"):
        raise LoadError(f"unsupported target {target!r}")
    parts = target[3:].split(".")
    if not all(p.isidentifier() for p in parts):
        raise LoadError(f"target {target!r}: not a dotted Python name (py:package.module.function)")
    for i in range(len(parts), 0, -1):
        try:
            obj = import_module(".".join(parts[:i]), f"target {target}")
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
