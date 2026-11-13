"""Shape checks on raw records: known fields, removed fields and values, enum values, root paths."""
from __future__ import annotations

from typing import Any

from ..errors import LoadError
from ..model import ENUMS, MEDIATION_FIELDS, PATH_ROOTS
from ..registry import Registry

# Below-the-API fields that 0.x accepted and this version removed, with what to do instead.
REMOVED = {
    "consistency": "provider consistency levels were removed: a provider is a plain read at the enforcer's "
                   "timing; delete the field",
    "transaction": "transactional targets were removed: rules live on API boundaries; check the service "
                   "method's arguments (pre_effect) and delete the field",
    "tables": "tables (raw SQL write checks) were removed: a door rule checks which API calls the "
              "repository; delete the field",
}
REMOVED_VALUES = {
    ("effects", "transactional"): "effects: transactional was removed (rules stay on the API boundary); "
                                  "use effects: external, or none for a pure function",
    ("point", "storage"): "storage enforcers were removed: the database's own constraints are the "
                          "implementation's business; delete this enforcer or make it a pre_effect "
                          "decorator check",
}


def _check_fields(obj: Any, allowed: frozenset[str] | set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise LoadError(f"{where}: expected a mapping")
    unknown = set(obj) - allowed
    for k in sorted(unknown & set(REMOVED), key=str):
        raise LoadError(f"{where}: {k}: {REMOVED[k]}")
    for k, v in obj.items():
        if isinstance(v, str) and (k, v) in REMOVED_VALUES:
            raise LoadError(f"{where}: {REMOVED_VALUES[(k, v)]}")
    if unknown:
        raise LoadError(f"{where}: unknown field(s) {sorted(unknown, key=str)}")
    for k, v in obj.items():
        if k in ENUMS and (not isinstance(v, str) or v not in ENUMS[k]):
            hint = " (YAML reads a bare off/on as a boolean; quote it)" if isinstance(v, bool) else ""
            raise LoadError(f"{where}: {k}={v!r} not in {sorted(ENUMS[k])}{hint}")


def _check_path(rid: str, what: str, path: Any, reg: Registry, roots: Any = PATH_ROOTS) -> None:
    if not isinstance(path, str):
        raise LoadError(f"{rid}: {what}: path must be a string, got {path!r}")
    head, *rest = path.split(".")
    if head not in roots:
        raise LoadError(f"{rid}: {what}: bad root in {path!r}")
    if head == "provider" and (len(rest) != 1 or rest[0] not in reg.providers):
        raise LoadError(f"{rid}: {what}: {path!r} names no declared provider")


def _strings(v: Any, prefix: str = "") -> bool:
    """A string, or a non-empty list of strings, each starting with prefix."""
    items = [v] if isinstance(v, str) else v if isinstance(v, list) and v else None
    return items is not None and all(isinstance(x, str) and x.startswith(prefix) for x in items)


def _check_mediation(rid: str, raw: dict[str, Any]) -> None:
    """Shapes of mediation (chokepoint, residual_paths, and the scan's sink, door, allow) and applies_to."""
    if "applies_to" in raw and not _strings(raw["applies_to"]):
        raise LoadError(f"{rid}: applies_to must be a pattern or a non-empty list of patterns")
    med = raw.get("mediation")
    if med is None:
        return
    _check_fields(med, MEDIATION_FIELDS, f"{rid}.mediation")
    if "chokepoint" in med and not isinstance(med["chokepoint"], str):
        raise LoadError(f"{rid}: mediation.chokepoint must be a string")
    rp = med.get("residual_paths", [])
    if not isinstance(rp, list) or not all(isinstance(x, str) for x in rp):
        raise LoadError(f"{rid}: mediation.residual_paths must be a list of strings")
    for k in ("sink", "door", "allow"):
        if k in med and not _strings(med[k], "py:"):
            raise LoadError(f"{rid}: mediation.{k} must be a 'py:' path or a non-empty list of them")
