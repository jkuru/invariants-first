"""The per-rule checks: bypass paths, bypass idioms at each site, and allow entries no longer used."""
from __future__ import annotations

import collections
import dataclasses
import pathlib

from .contracts import SKIP_DIRS, Finding, Mediation, _Edge, _Site
from .indexer import _Index
from .reachability import _paths, _reach, door_only


def _canon_py(idx: _Index, p: str) -> str:
    return idx.canon(p[3:])


def _loc(idx: _Index, owner: str) -> tuple[pathlib.Path, pathlib.Path]:
    f = idx.funcs[owner]
    return f.root, f.file


def _finding(idx: _Index, rule: str, kind: str, code: str, msg: str, owner: str | None = None,
             line: int = 0, col: int = 0, path: list[str] | None = None) -> Finding:
    file = root = None
    if owner and owner in idx.funcs:
        r, f = _loc(idx, owner)
        file, root = f.relative_to(r).as_posix(), str(r)
        if not line:
            line, col = idx.funcs[owner].line, idx.funcs[owner].col
    return Finding(kind, rule, code, msg, file, line, col + 1 if file else 0, root, path or [],
                   owner if owner and owner in idx.funcs and idx.funcs[owner].node is not None else None)


@dataclasses.dataclass
class _Targets:
    """A rule's declared sinks, doors and allowed back doors that name a scanned function."""

    sinks: set[str]
    doors: set[str]
    allowed: set[str]


_MISSING = {"sink": "sink {} is not defined in the scanned code",
            "door": "door {} is not defined in the scanned code",
            "allow": "allowed back door {} is not defined in the scanned code (fixed or renamed? delete it from allow)"}


def _check_rule(idx: _Index, med: Mediation, edges: list[_Edge], sites: list[_Site],
                entries: list[str], on_paths: set[str] | None = None) -> list[Finding]:
    """The findings for one rule. on_paths, when given, collects every function on a path that reaches the
    sink without the door, from every entrypoint (findings show one path per call site)."""
    rid, out = med.rule, []
    for root, file, line, err in idx.parse_errors:
        out.append(Finding("unresolved", rid, "unresolved/parse-error", f"cannot parse: {err}",
                           file.relative_to(root).as_posix(), line, 1, str(root)))
    t = _Targets(_declared(idx, rid, med.sinks, "sink", out), _declared(idx, rid, med.doors, "door", out),
                 _declared(idx, rid, med.allow, "allow", out))
    out += _bypasses(idx, rid, edges, entries, t, on_paths)
    out += _idioms(idx, rid, edges, sites, entries, t)
    if t.allowed:
        out += _unused_allows(idx, med, edges, sites, entries, t)
    return out


def _declared(idx: _Index, rid: str, paths: list[str], role: str, out: list[Finding]) -> set[str]:
    """The scanned functions a rule's py: paths of one role name; a missing finding (into out) for each other."""
    found: set[str] = set()
    for p in paths:
        c = _canon_py(idx, p)
        if c in idx.funcs:
            found.add(c)
        else:
            out.append(Finding("missing", rid, f"missing/{role}", _MISSING[role].format(p)))
    return found


def _bypasses(idx: _Index, rid: str, edges: list[_Edge], entries: list[str], t: _Targets,
              on_paths: set[str] | None) -> list[Finding]:
    """1. paths from entrypoints to the sink with the doors removed. An allowed back door is not a door:
    only a path made of allowed functions alone (the call chains frozen when allow was written) is let
    through; a path that passes any other function (a new caller, or new code an allowed one calls) is
    a bypass, even when it reaches the sink through an allowed function."""
    sinks, doors, allowed = t.sinks, t.doors, t.allowed
    door_txt = ", ".join(sorted(doors)) or "(no door found)"
    out: list[Finding] = []
    live = sinks - doors
    if live:
        succ: dict[str, list[_Edge]] = collections.defaultdict(list)
        for e in edges:
            # a getattr of the sink name is reported at its site (forbidden/getattr-sink), except inside an
            # allowed function, where the site is excused: there the edge stays, so a path into the allowed
            # function from anything outside allow is a bypass, like a direct call
            if e.caller not in doors and e.callee not in doors and not (
                    e.via_getattr and e.callee in sinks and e.caller not in allowed):
                succ[e.caller].append(e)
        # one finding per call site that reaches the sink (file:line:col), with the shortest path from the
        # first entrypoint that reaches it; a site reached only through a name-matched call is unresolved
        reported: set[tuple[str, int, int]] = set()
        for inferred_ok in (False, True):
            for start in entries:
                if start in sinks or start in doors:
                    continue
                for names, last in _paths(start, live, succ, inferred_ok, allowed):
                    if on_paths is not None:
                        on_paths.update(names)
                    site = (last.caller, last.line, last.col)
                    if site in reported:
                        continue
                    reported.add(site)
                    kind = "unresolved" if inferred_ok else "bypass"
                    via = " (through a name-matched call on an unknown receiver)" if inferred_ok else ""
                    out.append(_finding(idx, rid, kind, "unresolved/inferred-path" if inferred_ok else "bypass",
                                        f"{' -> '.join(names)} reaches the sink without passing {door_txt}{via}",
                                        last.caller, last.line, last.col, names))
    return out


def _idioms(idx: _Index, rid: str, edges: list[_Edge], sites: list[_Site], entries: list[str],
            t: _Targets) -> list[Finding]:
    """2. idioms, per site. Inside an allowed back door, the ways it reaches the sink are frozen with it;
    unwrapping a guard, monkeypatching and reflection are still reported there."""
    sinks, doors, allowed = t.sinks, t.doors, t.allowed
    trusted = sinks | doors | allowed
    dominated = door_only(idx, edges, sites, entries, trusted)
    sink_names = {s.rsplit(".", 1)[1] for s in sinks}
    guarded_names = sink_names | {d.rsplit(".", 1)[1] for d in doors}
    unwraps: dict[str, bool] = {}

    def unwraps_here(fn: str) -> bool:
        """An unwrap of fn bears on this rule: fn is unknown, is a sink, door or allowed function, or reaches
        a sink without passing a door (its own guard may be what stands between them)."""
        if fn not in idx.funcs or fn in trusted:
            return True
        if fn not in unwraps:
            unwraps[fn] = bool(_reach([fn], edges, doors) & sinks)
        return unwraps[fn]

    out: list[Finding] = []
    for s in sites:
        if s.owner in allowed and (s.what in ("ref", "attrcall")
                                   or s.what == "getattr" and s.detail.partition("|")[2]):
            continue                                   # a getattr with a computed name is still reported
        if s.owner in trusted and s.what in ("ref", "attrcall"):
            continue
        if s.owner in dominated and s.what == "attrcall":
            continue                                   # reached only through a door or sink
        obj, _, lit = s.detail.partition("|")
        if s.what == "getattr" and obj in ("?attrgetter", "?methodcaller"):
            lit = _looked_up(obj, lit, sink_names, guarded_names)
        if s.what == "getattr" and lit in guarded_names:
            what = "sink" if lit in sink_names else "door"
            out.append(_finding(idx, rid, "forbidden", f"forbidden/getattr-{what}",
                                f"getattr-style lookup of the {what} name {lit!r} hides the call from the scan",
                                s.owner, s.line, s.col))
        elif s.what in ("getattr", "setattr", "delattr", "hasattr") and lit == "__wrapped__":
            if not unwraps_here(obj):
                continue
            out.append(_finding(idx, rid, "forbidden", "forbidden/unwrap",
                                "reaches __wrapped__, which calls a guarded function without its guard",
                                s.owner, s.line, s.col))
        elif s.what in ("setattr", "delattr") and lit in guarded_names:
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"{s.what} on {lit!r} replaces the sink or door at run time", s.owner, s.line, s.col))
        elif s.what in ("getattr", "setattr", "delattr") and not lit and s.what != "hasattr":
            how = f"operator.{obj[1:]}" if obj in ("?attrgetter", "?methodcaller") else s.what
            recv = "" if obj in ("?attrgetter", "?methodcaller") else f" on {obj}"
            out.append(_finding(idx, rid, "unresolved", "unresolved/computed-attribute",
                                f"{how} with a computed name{recv}: the scan cannot tell whether it reaches "
                                f"the sink", s.owner, s.line, s.col))
        elif s.what == "patch" and s.detail in trusted:
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"assigns over {s.detail}, replacing the sink or door at run time",
                                s.owner, s.line, s.col))
        elif s.what == "patch" and _holds(idx, s.detail, trusted):
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"assigns over {s.detail}, which holds the sink or door, replacing it at run time",
                                s.owner, s.line, s.col))
        elif s.what in ("setattr", "delattr") and lit and obj and not obj.startswith(("self:", "?")) \
                and _holds(idx, idx.canon(f"{obj}.{lit}"), trusted):
            out.append(_finding(idx, rid, "forbidden", "forbidden/monkeypatch",
                                f"{s.what} on {lit!r} replaces {idx.canon(f'{obj}.{lit}')}, which holds the sink "
                                f"or door, at run time", s.owner, s.line, s.col))
        elif s.what == "wrapped":
            shown, _, fn = s.detail.partition("|")
            if not unwraps_here(fn):
                continue
            out.append(_finding(idx, rid, "forbidden", "forbidden/unwrap",
                                f"{shown or 'an object'}.__wrapped__ calls a guarded function without its guard",
                                s.owner, s.line, s.col))
        elif s.what == "ref" and s.detail in sinks:
            out.append(_finding(idx, rid, "unresolved", "unresolved/sink-escapes",
                                f"the sink {s.detail} is used as a value, so calls through it cannot be traced",
                                s.owner, s.line, s.col))
        elif s.what == "attrcall" and s.detail in sink_names:
            out.append(_finding(idx, rid, "unresolved", "unresolved/unknown-receiver",
                                f".{s.detail}() on a receiver the scan cannot resolve may be the sink",
                                s.owner, s.line, s.col))
        elif s.what == "excluded" and s.owner not in trusted and s.owner not in dominated:
            out.append(_excluded_finding(idx, rid, s))
        elif s.what in ("reflection", "dynimport"):
            out.append(_finding(idx, rid, "unresolved", "unresolved/reflection",
                                f"{s.detail} runs or looks up code the scan cannot follow", s.owner, s.line, s.col))
    return out


def _looked_up(obj: str, lit: str, sink_names: set[str], guarded_names: set[str]) -> str:
    """The name an attrgetter/methodcaller site stands for: every name an attrgetter looks up ("repo.save"
    looks up repo, then save), as one site; a sink name first, then a door name, then __wrapped__."""
    parts = [p for x in lit.split("|") for p in (x.split(".") if obj == "?attrgetter" else [x])]
    return next((p for p in parts if p in sink_names), None) or next(
        (p for p in parts if p in guarded_names), None) or (
        "__wrapped__" if "__wrapped__" in parts else parts[0] if lit else "")


def _excluded_finding(idx: _Index, rid: str, s: _Site) -> Finding:
    """A reference from scanned code into a module the scan does not read (excluded or always skipped)."""
    fq, mod, how = s.detail.split("|")
    path, glob = idx.excluded[mod]
    verb = {"call": "calls", "ref": "refers to", "star": "star-imports", "import": "imports"}[how]
    why = (f"in a {glob!r} directory, which the scan always skips" if glob in SKIP_DIRS or
           glob.startswith(".") else f"excluded by {glob!r}")
    what = ("which runs code at import time that the scan does not read" if how == "import"
            else "which the scan does not read")
    return _finding(idx, rid, "unresolved", "unresolved/excluded-callee",
                        f"{verb} {fq} in {path}, {what} ({why}): "
                        f"it may reach the sink; scan it (narrow the exclude) or move it out of the "
                        f"excluded path", s.owner, s.line, s.col)


def _unused_allows(idx: _Index, med: Mediation, edges: list[_Edge], sites: list[_Site], entries: list[str],
                   t: _Targets) -> list[Finding]:
    """3. an allowed back door that is no longer one: its entry would let a regression through unseen.
    An entry is in use when the scan without allow reports something in it or on a path through it."""
    on: set[str] = set()
    bare = _check_rule(idx, Mediation(med.rule, med.sinks, med.doors), edges, sites, entries, on)
    used = {f.owner for f in bare if f.owner} | {n for f in bare for n in f.path} | on
    return [_finding(idx, med.rule, "missing", "missing/allow-unused",
                     f"allowed back door {a} no longer reaches the sink without the door; delete it from "
                     f"allow (a tightening), so a regression there is caught",
                     a)
            for a in sorted(t.allowed - t.sinks - t.doors - used)]


def _holds(idx: _Index, name: str, trusted: set[str]) -> bool:
    """name is a scanned module or class that holds a sink, door or allowed function: rebinding it
    (`pkg.repo.Repo = Fake`) replaces them all."""
    return (name in idx.modules or name in idx.classes) and any(t.startswith(name + ".") for t in trusted)
