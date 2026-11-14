"""The per-rule checks: bypass paths, bypass idioms at each site, and allow entries no longer used."""
from __future__ import annotations

import collections
import dataclasses
import itertools
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
                entries: list[str], on_paths: set[str] | None = None, every_escape: bool = False) -> list[Finding]:
    """The findings for one rule. on_paths, when given, collects every function on a path that reaches the
    sink without the door, from every entrypoint (findings show one path per call site). every_escape:
    report a function used as a value even when the path search already reports its paths."""
    rid, out = med.rule, []
    for root, file, line, err in idx.parse_errors:
        out.append(Finding("unresolved", rid, "unresolved/parse-error", f"cannot parse: {err}",
                           file.relative_to(root).as_posix(), line, 1, str(root)))
    t = _Targets(_declared(idx, rid, med.sinks, "sink", out), _declared(idx, rid, med.doors, "door", out),
                 _declared(idx, rid, med.allow, "allow", out))
    paths = _bypasses(idx, rid, edges, entries, t, on_paths) + _escapes(idx, rid, edges, sites, entries, t, on_paths,
                                                                     every_escape)
    out += paths + _one_per_site(paths, _Idioms(idx, rid, edges, sites, entries, t).findings(sites))
    if t.allowed:
        out += _unused_allows(idx, med, edges, sites, entries, t)
    return out


def _one_per_site(paths: list[Finding], idioms: list[Finding]) -> list[Finding]:
    """The idiom findings, less a name-matched call that a path finding already reports at the same call
    site: `repo.save(x)` on an unknown receiver is one finding (the path, which names the method it
    matched), not two."""
    at = {(f.file, f.line, f.col) for f in paths if f.code == "unresolved/inferred-path"}
    return [f for f in idioms if not (f.code == "unresolved/unknown-receiver" and (f.file, f.line, f.col) in at)]


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
    live = t.sinks - t.doors
    if not live:
        return []
    door_txt = ", ".join(sorted(t.doors)) or "(no door found)"
    succ = _door_free(edges, t)
    out: list[Finding] = []
    # one finding per call site that reaches the sink (file:line:col), with the shortest path from the
    # first entrypoint that reaches it; a site reached only through a name-matched call is unresolved
    reported: set[tuple[str, int, int]] = set()
    for inferred_ok, start in itertools.product((False, True), entries):
        if start in t.sinks or start in t.doors:
            continue
        for names, last in _paths(start, live, succ, inferred_ok, t.allowed):
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


def _escapes(idx: _Index, rid: str, edges: list[_Edge], sites: list[_Site], entries: list[str], t: _Targets,
             on_paths: set[str] | None, every_escape: bool = False) -> list[Finding]:
    """1b. a function used as a value is an entrypoint: whoever holds the value can call it from anywhere. A
    function taken as a value (a thread target, a callback, a dict entry, an alias, a bound method) where
    the value is made on a path that has not passed a door, outside the sinks, doors and allowed
    functions, that reaches the sink without passing a door when called, is reported where it escapes.
    That counts a door's own helper (refund -> _send -> sink): called on its own, _send skips the door. An
    allowed back door taken as a value outside allow is a new caller: a bypass. (The sink itself as a value
    is reported at the site, as unresolved/sink-escapes; a door as a value passes the door when called.)"""
    live = t.sinks - t.doors
    trusted = t.sinks | t.doors | t.allowed
    refs = [s for s in sites if s.what == "ref" and s.owner not in trusted and s.detail in idx.funcs
            and idx.funcs[s.detail].node is not None and s.detail not in t.sinks and s.detail not in t.doors]
    if not live or not refs:
        return []
    succ = _door_free(edges, t)
    # where a value can be made without passing a door: reached from an entrypoint over calls (name-matched
    # ones too) and over other escapes (`def get(): return _send`, itself taken as a value)
    reach_succ: dict[str, list[_Edge]] = collections.defaultdict(list, {k: list(v) for k, v in succ.items()})
    for s in refs:
        reach_succ[s.owner].append(_Edge(s.owner, s.detail, s.line, s.col, False))
    free = _reach([e for e in entries if e not in t.sinks and e not in t.doors],
                  [e for es in reach_succ.values() for e in es], t.sinks | t.doors)
    # a function already reached over extracted calls from an entrypoint outside allow, without a door:
    # every path it has to the sink is reported by the path search already (as a bypass)
    covered = set() if every_escape else _reach(
        [e for e in entries if e not in trusted], [e for es in succ.values() for e in es if not e.inferred],
        t.sinks | t.doors)
    door_txt = ", ".join(sorted(t.doors)) or "(no door found)"
    out: list[Finding] = []
    seen: set[tuple[str, int, int]] = set()
    for s in refs:
        if s.owner not in free or s.detail in covered or (s.owner, s.line, s.col) in seen:
            continue
        found = None
        for inferred_ok in (False, True):
            found = next(_paths(s.detail, live, succ, inferred_ok), None)
            if found is not None:
                break
        if found is None:
            continue
        seen.add((s.owner, s.line, s.col))
        names = [s.owner, *found[0]]
        if on_paths is not None:
            on_paths.update(names)
        via = " (through a name-matched call on an unknown receiver)" if inferred_ok else ""
        if s.detail in t.allowed:
            out.append(_finding(idx, rid, "bypass", "bypass",
                                f"takes the allowed back door {s.detail} as a value outside allow, a new caller: "
                                f"{' -> '.join(names)} reaches the sink without passing {door_txt}{via}",
                                s.owner, s.line, s.col, names))
        else:
            out.append(_finding(idx, rid, "unresolved", "unresolved/sink-escapes",
                                f"{s.detail} is used as a value here, and called it reaches the sink without "
                                f"passing {door_txt} ({' -> '.join(names[1:])}{via}): whoever holds the value "
                                f"can call it, and the scan cannot trace those calls; call it through the "
                                f"door, or keep it inside the door", s.owner, s.line, s.col, names))
    return out


def _door_free(edges: list[_Edge], t: _Targets) -> dict[str, list[_Edge]]:
    """The call graph with the doors removed, as each caller's edges."""
    sinks, doors, allowed = t.sinks, t.doors, t.allowed
    succ: dict[str, list[_Edge]] = collections.defaultdict(list)
    for e in edges:
        # a getattr of the sink name is reported at its site (forbidden/getattr-sink), except inside an
        # allowed function, where the site is excused: there the edge stays, so a path into the allowed
        # function from anything outside allow is a bypass, like a direct call
        if e.caller not in doors and e.callee not in doors and not (
                e.via_getattr and e.callee in sinks and e.caller not in allowed):
            succ[e.caller].append(e)
    return succ


class _Idioms:
    """2. idioms, per site. Inside an allowed back door, the ways it reaches the sink are frozen with it;
    unwrapping a guard, monkeypatching and reflection are still reported there. Each site gets the first
    idiom it matches; the idioms of different site kinds are tried in their own order."""

    def __init__(self, idx: _Index, rid: str, edges: list[_Edge], sites: list[_Site], entries: list[str],
                 t: _Targets) -> None:
        self.idx, self.rid, self.edges, self.t = idx, rid, edges, t
        self.trusted = t.sinks | t.doors | t.allowed
        self.dominated = door_only(idx, edges, sites, entries, self.trusted)
        self.sink_names = {s.rsplit(".", 1)[1] for s in t.sinks}
        self.guarded_names = self.sink_names | {d.rsplit(".", 1)[1] for d in t.doors}
        self._unwraps: dict[str, bool] = {}

    def findings(self, sites: list[_Site]) -> list[Finding]:
        return [f for s in sites if (f := self._at(s)) is not None]

    def _at(self, s: _Site) -> Finding | None:
        if s.owner in self.trusted and s.what in ("ref", "attrcall", "attrref"):
            return None
        if s.owner in self.dominated and s.what in ("attrcall", "attrref"):
            return None                                # reached only through a door or sink
        if s.what in ("getattr", "setattr", "delattr", "hasattr"):
            return self._named(s)
        if s.what == "patch":
            return self._patch(s)
        return self._other(s)

    def _unwraps_here(self, fn: str) -> bool:
        """An unwrap of fn bears on this rule: fn is unknown, is a sink, door or allowed function, or reaches
        a sink without passing a door (its own guard may be what stands between them)."""
        if fn not in self.idx.funcs or fn in self.trusted:
            return True
        if fn not in self._unwraps:
            self._unwraps[fn] = bool(_reach([fn], self.edges, self.t.doors) & self.t.sinks)
        return self._unwraps[fn]

    def _found(self, kind: str, code: str, msg: str, s: _Site) -> Finding:
        return _finding(self.idx, self.rid, kind, code, msg, s.owner, s.line, s.col)

    def _named(self, s: _Site) -> Finding | None:
        """getattr/setattr/delattr/hasattr and the getattr-style lookups (attrgetter, ns.__dict__[k], ...)."""
        obj, _, lit = s.detail.partition("|")
        if s.owner in self.t.allowed and s.what == "getattr" and lit:
            if "__wrapped__" not in _names(obj, lit):
                return None                            # a computed name is still reported, and an unwrap:
            lit = "__wrapped__"                        # inside allow, a lookup of __wrapped__ is one
        elif s.what == "getattr" and obj in ("?attrgetter", "?methodcaller"):
            lit = _looked_up(obj, lit, self.sink_names, self.guarded_names)
        if s.what == "getattr" and lit in self.guarded_names:
            what = "sink" if lit in self.sink_names else "door"
            return self._found("forbidden", f"forbidden/getattr-{what}",
                               f"getattr-style lookup of the {what} name {lit!r} hides the call from the scan", s)
        if lit == "__wrapped__":
            return self._found("forbidden", "forbidden/unwrap",
                               "reaches __wrapped__, which calls a guarded function without its guard",
                               s) if self._unwraps_here(obj) else None
        if s.what in ("setattr", "delattr") and lit in self.guarded_names:
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"{s.what} on {lit!r} replaces the sink or door at run time", s)
        if not lit and s.what != "hasattr":
            how = f"operator.{obj[1:]}" if obj in ("?attrgetter", "?methodcaller") else s.what
            recv = "" if obj in ("?attrgetter", "?methodcaller") else f" on {obj}"
            return self._found("unresolved", "unresolved/computed-attribute",
                               f"{how} with a computed name{recv}: the scan cannot tell whether it reaches "
                               f"the sink", s)
        if s.what in ("setattr", "delattr") and lit and obj and not obj.startswith(("self:", "?")) \
                and _holds(self.idx, self.idx.canon(f"{obj}.{lit}"), self.trusted):
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"{s.what} on {lit!r} replaces {self.idx.canon(f'{obj}.{lit}')}, which holds the "
                               f"sink or door, at run time", s)
        return None

    def _patch(self, s: _Site) -> Finding | None:
        """An assignment over a scanned name (`pkg.repo.save = fake`)."""
        if s.detail in self.trusted:
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"assigns over {s.detail}, replacing the sink or door at run time", s)
        if _holds(self.idx, s.detail, self.trusted):
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"assigns over {s.detail}, which holds the sink or door, replacing it at run time", s)
        fn, _, attr = s.detail.rpartition(".")
        if fn in self.trusted and fn in self.idx.funcs and attr.startswith("__"):
            return self._found("forbidden", "forbidden/monkeypatch",   # door.__code__ = other.__code__
                               f"assigns over {s.detail}, replacing what the sink or door runs at run time", s)
        return None

    def _other(self, s: _Site) -> Finding | None:
        if s.what == "wrapped":
            shown, _, rest = s.detail.partition("|")
            fn, _, how = rest.partition("|")             # how: inspect.unwrap(f), f.__closure__, f.__dict__
            return self._found("forbidden", "forbidden/unwrap",
                               f"{how or (shown or 'an object') + '.__wrapped__'} calls a guarded function "
                               f"without its guard", s) if self._unwraps_here(fn) else None
        if s.what == "ref" and s.detail in self.t.sinks:
            return self._found("unresolved", "unresolved/sink-escapes",
                               f"the sink {s.detail} is used as a value, so calls through it cannot be traced", s)
        if s.what == "attrpatch" and s.detail in self.sink_names:
            return self._found("unresolved", "unresolved/unknown-receiver",
                               f"assigns or deletes .{s.detail} on a receiver the scan cannot resolve, which may "
                               f"replace the sink at run time", s)
        if s.what == "attrref" and s.detail in self.sink_names:
            return self._found("unresolved", "unresolved/sink-escapes",
                               f".{s.detail} on a receiver the scan cannot resolve is used as a value and may be "
                               f"the sink, so calls through it cannot be traced", s)
        if s.what == "attrcall" and s.detail in self.sink_names:
            return self._found("unresolved", "unresolved/unknown-receiver",
                               f".{s.detail}() on a receiver the scan cannot resolve may be the sink", s)
        if s.what == "excluded" and s.owner not in self.trusted and s.owner not in self.dominated:
            return _excluded_finding(self.idx, self.rid, s)
        if s.what in ("reflection", "dynimport"):
            return self._found("unresolved", "unresolved/reflection",
                               f"{s.detail} runs or looks up code the scan cannot follow", s)
        return None


def _looked_up(obj: str, lit: str, sink_names: set[str], guarded_names: set[str]) -> str:
    """The name an attrgetter/methodcaller site stands for: every name an attrgetter looks up ("repo.save"
    looks up repo, then save), as one site; a sink name first, then a door name, then __wrapped__."""
    parts = _names(obj, lit)
    return next((p for p in parts if p in sink_names), None) or next(
        (p for p in parts if p in guarded_names), None) or (
        "__wrapped__" if "__wrapped__" in parts else parts[0] if lit else "")


def _names(obj: str, lit: str) -> list[str]:
    """Every name a getattr-style site looks up: its literal names, each dotted one split for attrgetter."""
    return [p for x in lit.split("|") for p in (x.split(".") if obj == "?attrgetter" else [x])]


def _excluded_finding(idx: _Index, rid: str, s: _Site) -> Finding:
    """A reference from scanned code into a module the scan does not read (excluded or always skipped)."""
    fq, mod, how = s.detail.split("|")
    path, glob = idx.excluded[mod]
    verb = {"call": "calls", "ref": "refers to", "star": "star-imports", "import": "imports"}[how]
    why = (glob[1:-1] if glob.startswith("<") else      # fix round 23: a module with no source
           f"in a {glob!r} directory, which the scan always skips" if glob in SKIP_DIRS or
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
    # every escape counts: an allowed function that takes another as a value is in use (deleting it from
    # allow would make that value a new caller)
    bare = _check_rule(idx, Mediation(med.rule, med.sinks, med.doors), edges, sites, entries, on, every_escape=True)
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
