"""Reading the rule file: each rule's mediation fields (sink, door, allow); scan.mediations() is the entry."""
from __future__ import annotations

from typing import Any

from .. import _scan_hooks
from .contracts import Mediation, ScanConfigError


def _str_list(v: Any, where: str) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x for x in items):
        raise ScanConfigError(f"{where}: expected a string or a non-empty list of strings")
    return list(items)


def _not_scanned(rid: str, raw: dict[str, Any], med: Any) -> str | None:
    """Why the rule is not scanned (retired, ablated, no sink), or None; raises on removed or malformed fields."""
    if not isinstance(med, dict):
        raise ScanConfigError(f"{rid}: mediation must be a mapping")
    if "tables" in med:
        raise ScanConfigError(f"{rid}: mediation.tables was removed (raw SQL is below the API): a door rule "
                              f"checks which API calls the repository; delete it")
    if raw.get("status") == "retired":
        return "retired"
    # rules turned off for an ablation run: INV_ABLATE (when armed) and in-process ablated_everywhere
    if rid in _scan_hooks.ablated():
        _scan_hooks.reached(rid)
        return ("ablated (INV_ABLATE); an enforce rule, so the scan cannot pass"
                if raw.get("status") == "enforce" else "ablated (INV_ABLATE)")
    if "sink" not in med:
        if "door" in med or "allow" in med:
            raise ScanConfigError(f"{rid}: mediation.door and allow need mediation.sink")
        return "mediation declares no sink"
    return None


def _mediation(rid: str, raw: dict[str, Any], med: dict[str, Any]) -> Mediation:
    """The sink, door (subject.target by default) and allow fields of a rule that declares a sink."""
    sinks = _str_list(med["sink"], f"{rid}: mediation.sink")
    default_door = (raw.get("subject") or {}).get("target")
    if "door" in med:
        doors = _str_list(med["door"], f"{rid}: mediation.door")
    elif isinstance(default_door, str):
        doors = [default_door]
    else:
        raise ScanConfigError(f"{rid}: mediation.sink needs mediation.door or a subject.target")
    allow = _str_list(med["allow"], f"{rid}: mediation.allow") if "allow" in med else []
    for p in sinks + doors + allow:
        if not p.startswith("py:"):
            raise ScanConfigError(f"{rid}: {p!r} must be a 'py:module.name' path")
    return Mediation(rid, sinks, doors, allow)
