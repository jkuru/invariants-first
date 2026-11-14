"""Reading the rule file: each rule's mediation fields (sink, door, allow, allow_unresolved);
scan.mediations() is the entry."""
from __future__ import annotations

import re
from typing import Any

from . import hooks
from .contracts import Accept, Mediation, ScanConfigError

_AT = re.compile(r"^(?P<file>[^:]+):(?P<line>[1-9][0-9]*)(?::(?P<col>[1-9][0-9]*))?$")
_SHA = re.compile(r"^[0-9a-f]{16}$")
ACCEPT_FIELDS = frozenset({"at", "code", "sha", "why"})       # required; a shared entry also has rules


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
    if rid in hooks.ablated():
        hooks.reached(rid)
        return ("ablated (INV_ABLATE); an enforce rule, so the scan cannot pass"
                if raw.get("status") == "enforce" else "ablated (INV_ABLATE)")
    if "sink" not in med:
        if "door" in med or "allow" in med or "allow_unresolved" in med:
            raise ScanConfigError(f"{rid}: mediation.door, allow and allow_unresolved need mediation.sink")
        return "mediation declares no sink"
    return None


def _mediation(rid: str, raw: dict[str, Any], med: dict[str, Any]) -> Mediation:
    """The sink, door (subject.target by default) and allow fields of a rule that declares a sink."""
    if isinstance(med["sink"], dict):
        return _paired(rid, med)
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
    own = sorted(set(sinks) & set(doors))
    if own:                                            # fix round 31: it would mediate itself, so pass always
        raise ScanConfigError(f"{rid}: {', '.join(own)} is both a sink and a door: a sink is never its own door "
                              f"(every call to it would pass); the door is the function that must be passed first")
    accept = _accepts(rid, med["allow_unresolved"]) if "allow_unresolved" in med else None
    return Mediation(rid, sinks, doors, allow, accept)


def pairs_of(rid: str, sink: Any, where: str = "mediation.sink") -> dict[str, list[str]]:
    """A sink mapping (each sink -> the door or doors that may reach it), its doors as lists; raises
    ScanConfigError on a malformed one."""
    if not isinstance(sink, dict) or not sink:
        raise ScanConfigError(f"{rid}: {where} as a mapping names at least one sink, each with the door or doors "
                              f"that may reach it ({{py:store.write: py:service.door}})")
    out: dict[str, list[str]] = {}
    for s, ds in sink.items():
        doors = [ds] if isinstance(ds, str) else ds
        if not (isinstance(s, str) and s.startswith("py:") and isinstance(doors, list) and doors
                and all(isinstance(d, str) and d.startswith("py:") for d in doors)):
            raise ScanConfigError(f"{rid}: {where}: each sink is a 'py:' path mapped to a 'py:' path or a non-empty "
                                  f"list of them (the doors that may reach it), got {s!r}: {ds!r}")
        if s in doors:                                 # fix round 31: it would mediate itself, so pass always
            raise ScanConfigError(f"{rid}: {where}: {s} maps to itself: a sink is never its own door (every call to "
                                  f"it would pass); map it to the function that must be passed first")
        out[s] = list(dict.fromkeys(doors))
    return out


def _paired(rid: str, med: dict[str, Any]) -> Mediation:
    """mediation.sink as a mapping: each sink with the doors that may reach it. mediation.door, when given,
    names exactly the doors the mapping lists (it adds none: a door listed for one sink only is no door for
    another); a subject.target is no default door here."""
    pairs = pairs_of(rid, med["sink"])
    doors = list(dict.fromkeys(d for ds in pairs.values() for d in ds))
    if "door" in med and set(_str_list(med["door"], f"{rid}: mediation.door")) != set(doors):
        raise ScanConfigError(f"{rid}: mediation.door must be left out or name exactly the doors the sink mapping "
                              f"lists ({', '.join(doors)}): with a mapping, each sink's doors are the ones it maps to")
    allow_to = allow_map_of(rid, med["allow"], list(pairs)) if "allow" in med else {}
    accept = _accepts(rid, med["allow_unresolved"]) if "allow_unresolved" in med else None
    return Mediation(rid, list(pairs), doors, list(allow_to), accept, pairs, allow_to)


def allow_map_of(rid: str, allow: Any, sinks: list[str], where: str = "mediation.allow") -> dict[str, list[str]]:
    """allow in a rule whose sink is a mapping (round 32): each allowed function -> the sink or sinks of the
    mapping it may reach without a door, the sinks as lists; raises ScanConfigError on another shape. An
    entry excuses only the sinks it lists: a back door frozen for one write is no back door for another."""
    if not isinstance(allow, dict):
        raise ScanConfigError(f"{rid}: {where}: this rule's sink is a mapping (each sink with its doors), so allow "
                              f"maps each allowed function to the sink(s) it may reach without a door: "
                              f"allow: {{py:admin.fix: [py:store.record_refund]}}")
    out: dict[str, list[str]] = {}
    for a, ss in allow.items():
        listed = [ss] if isinstance(ss, str) else ss
        if not (isinstance(a, str) and a.startswith("py:") and isinstance(listed, list) and listed
                and all(isinstance(s, str) and s.startswith("py:") for s in listed)):
            raise ScanConfigError(f"{rid}: {where}: each allowed function is a 'py:' path mapped to a sink of the "
                                  f"mapping or a non-empty list of them (the sinks it may reach without a door), "
                                  f"got {a!r}: {ss!r}")
        other = [s for s in listed if s not in sinks]
        if other:
            raise ScanConfigError(f"{rid}: {where}: {a}: {', '.join(other)} is not a sink of this rule's mapping "
                                  f"(its sinks: {', '.join(sinks)})")
        out[a] = list(dict.fromkeys(listed))
    return out


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
        extra = sorted(set(e) - ACCEPT_FIELDS - {"rules"})
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
        # an entry of the file's top-level allow_unresolved, given to each rule it lists (loader.short_forms)
        shared = e.get("rules", [])
        if "rules" in e and not (isinstance(shared, list) and rid in shared and all(isinstance(r, str) for r in shared)):
            raise ScanConfigError(f"{at}: rules lists the rules a shared entry accepts its site under (this rule, "
                                  f"{rid}, among them); write a shared entry in the file's top-level allow_unresolved")
        out.append(Accept(e["at"], m["file"], int(m["line"]), int(m["col"]) if m["col"] else None, code,
                          e["sha"], e["why"], tuple(shared)))
    return out
