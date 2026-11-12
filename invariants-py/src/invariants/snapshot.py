"""old_self: copy only the paths a transition rule reads, and only plain data."""
from __future__ import annotations

import copy
import dataclasses
from typing import Any

from .cel import CelError
from .errors import LoadError
from .model import Rule
from .providers import walk
from .registry import Registry, current


def is_plain(v: Any, depth: int = 0) -> bool:
    """Numbers, strings, None, lists, maps and dataclasses of those."""
    if depth > 64:
        return False
    if v is None or isinstance(v, (bool, int, float, str)):
        return True
    if isinstance(v, (list, tuple)):
        return all(is_plain(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return all(isinstance(k, (str, int)) and is_plain(x, depth + 1) for k, x in v.items())
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return all(is_plain(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v))
    return False


def take_snapshot(rule: Rule, obj: Any) -> dict[str, Any]:
    """Deep-copy the referenced old_self paths; never touch anything else on self."""
    snap: dict[str, Any] = {}
    for path in rule.snapshot:
        v = walk(obj, path)
        if not is_plain(v):
            raise CelError(f"old_self.{'.'.join(path)} is not plain data ({type(v).__name__})")
        d = snap
        for p in path[:-1]:
            d = d.setdefault(p, {})
        d[path[-1]] = copy.deepcopy(v)
    return snap


def check_snapshot(key: str, instance: Any, reg: Registry | None = None) -> list[str]:
    """Test-time check with a real instance: every old_self path is plain data, else LoadError."""
    checked = []
    for rule in (reg or current()).rules_for(key):
        for path in rule.snapshot:
            try:
                v = walk(instance, path)
            except CelError as e:
                raise LoadError(f"{rule.id}: old_self.{'.'.join(path)}: {e}") from e
            if not is_plain(v):
                raise LoadError(f"{rule.id}: old_self.{'.'.join(path)} is not plain data on a real "
                                f"{type(instance).__name__} (it is {type(v).__name__})")
            checked.append(f"{rule.id}:old_self.{'.'.join(path)}")
    return checked
