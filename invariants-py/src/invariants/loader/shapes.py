"""Shape checks on raw records: known fields, enum values, root paths."""
from __future__ import annotations

from typing import Any

from ..errors import LoadError
from ..model import ENUMS, MEDIATION_FIELDS, PATH_ROOTS
from ..registry import Registry
from ..resolve import close_matches

def _check_fields(obj: Any, allowed: frozenset[str] | set[str], where: str,
                  also: frozenset[str] | set[str] = frozenset()) -> None:
    """Known fields only (a near miss of an allowed field, or of `also`, is named) and enum values."""
    if not isinstance(obj, dict):
        raise LoadError(f"{where}: expected a mapping")
    unknown = set(obj) - allowed
    if unknown:
        shown = sorted(unknown, key=str)
        names = sorted(set(allowed) | set(also))
        close = {u: close_matches(str(u), names, n=1) for u in shown}
        hints = [f"{c[0]!r}" + (f" for {u!r}" if len(shown) > 1 else "") for u, c in close.items() if c]
        raise LoadError(f"{where}: unknown field(s) {shown}"
                        + (f" (did you mean {', '.join(hints)}?)" if hints else ""))
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


def _check_pairs(rid: str, sink: Any, where: str = "mediation.sink") -> None:
    """A sink mapping's shape (scan.mediation.pairs_of), as a LoadError."""
    from ..scan import ScanConfigError
    from ..scan.mediation import pairs_of
    try:
        pairs_of(rid, sink, where)
    except ScanConfigError as e:
        raise LoadError(str(e)) from None


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
    if isinstance(med.get("sink"), dict):              # each sink mapped to the doors that may reach it
        _check_pairs(rid, med["sink"])
    for k in ("sink", "door", "allow"):
        if k in med and not (k == "sink" and isinstance(med[k], dict)) and not _strings(med[k], "py:"):
            raise LoadError(f"{rid}: mediation.{k} must be a 'py:' path or a non-empty list of them")
    au = med.get("allow_unresolved")                   # reviewed unresolved sites; `inv scan` checks each entry
    if au is not None and not (isinstance(au, list) and all(isinstance(e, dict) for e in au)):
        raise LoadError(f"{rid}: mediation.allow_unresolved must be a list of entries {{at, code, sha, why}}")
