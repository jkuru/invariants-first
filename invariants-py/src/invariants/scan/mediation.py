"""Reading the rule file: each rule's mediation fields (sink, door, allow, allow_unresolved);
scan.mediations() is the entry."""
from __future__ import annotations

import re
from typing import Any

from .. import _scan_hooks
from .contracts import Accept, Mediation, ScanConfigError

_AT = re.compile(r"^(?P<file>[^:]+):(?P<line>[1-9][0-9]*)(?::(?P<col>[1-9][0-9]*))?$")
_SHA = re.compile(r"^[0-9a-f]{16}$")
ACCEPT_FIELDS = frozenset({"at", "code", "sha", "why"})


def _str_list(v: Any, where: str) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x for x in items):
        raise ScanConfigError(f"{where}: expected a string or a non-empty list of strings")
    return list(items)


def _not_scanned(rid: str, raw: dict[str, Any], med: Any) -> str | None:
    """Why the rule is not scanned (retired, ablated, no sink), or None; raises on malformed fields."""
    if not isinstance(med, dict):
        raise ScanConfigError(f"{rid}: mediation must be a mapping")
    if raw.get("status") == "retired":
        return "retired"
    # rules turned off for an ablation run: INV_ABLATE (when armed) and in-process ablated_everywhere
    if rid in _scan_hooks.ablated():
        _scan_hooks.reached(rid)
        return ("ablated (INV_ABLATE); an enforce rule, so the scan cannot pass"
                if raw.get("status") == "enforce" else "ablated (INV_ABLATE)")
    if "sink" not in med:
        if "door" in med or "allow" in med or "allow_unresolved" in med:
            raise ScanConfigError(f"{rid}: mediation.door, allow and allow_unresolved need mediation.sink")
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
    accept = _accepts(rid, med["allow_unresolved"]) if "allow_unresolved" in med else None
    return Mediation(rid, sinks, doors, allow, accept)


def _accepts(rid: str, raw: Any) -> list[Accept]:
    """mediation.allow_unresolved: reviewed unresolved findings, each {at, code, sha, why}."""
    where = f"{rid}: mediation.allow_unresolved"
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScanConfigError(f"{where} must be a list of entries {{at, code, sha, why}}")
    out = []
    for i, e in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(e, dict):
            raise ScanConfigError(f"{at} must be a mapping {{at, code, sha, why}}")
        extra = sorted(set(e) - ACCEPT_FIELDS)
        if extra:
            raise ScanConfigError(f"{at}: unknown field(s) {', '.join(map(str, extra))} "
                                  f"(an entry has at, code, sha, why)")
        missing = sorted(ACCEPT_FIELDS - set(e))
        if missing:
            raise ScanConfigError(f"{at}: missing {', '.join(missing)}; the scan's JSON output gives at, code and sha "
                                  f"for each unresolved finding, and why is the reviewer's reason")
        if not all(isinstance(e[k], str) for k in ACCEPT_FIELDS):
            raise ScanConfigError(f"{at}: at, code, sha and why must be strings")
        m = _AT.match(e["at"])
        if m is None:
            raise ScanConfigError(f"{at}: at must be path:line or path:line:col (the file relative to its source "
                                  f"root, as the scan reports it), got {e['at']!r}")
        code = e["code"]
        if not code.startswith("unresolved/"):
            raise ScanConfigError(f"{at}: only an unresolved finding can be accepted, not {code!r}: a bypass or a "
                                  f"forbidden idiom is fixed, or a known back door is frozen in allow")
        if not _SHA.match(e["sha"]):
            old = (": a 10-digit sha is from a version that hashed one line; review the site again and "
                   "write the sha the scan gives it now" if re.fullmatch(r"[0-9a-f]{10}", e["sha"]) else "")
            raise ScanConfigError(f"{at}: sha must be the 16 hex digits the scan gives the finding (inv scan "
                                  f"--format json), got {e['sha']!r}{old}")
        if not e["why"].strip():
            raise ScanConfigError(f"{at}: why must say why the reviewed site cannot reach the sink")
        out.append(Accept(e["at"], m["file"], int(m["line"]), int(m["col"]) if m["col"] else None, code,
                          e["sha"], e["why"]))
    return out
