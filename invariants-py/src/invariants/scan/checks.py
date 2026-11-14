"""The per-rule checks: bypass paths, bypass idioms at each site, and allow entries no longer used."""
from __future__ import annotations

import ast
import builtins
import collections
import dataclasses
import pathlib

from .astutil import CODE_ATTRS, _dotted
from .contracts import SKIP_DIRS, Finding, Mediation, _Edge, _Fan, _OnPaths, _Site
from .indexer import _Index
from .reachability import _Call, _can_reach, _Pass, _reach, _search_order, door_only


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
    _succ: dict[str, list[_Call]] | None = None
    _passes: list[_Pass] | None = None

    def door_free(self, edges: list[_Call]) -> dict[str, list[_Call]]:
        """The call graph with the doors removed (made once per rule)."""
        if self._succ is None:
            self._succ = _door_free(edges, self)
        return self._succ

    def passes(self, edges: list[_Call]) -> list[_Pass]:
        """The two passes of the path search into the live sinks: extracted calls, then name-matched too."""
        if self._passes is None:
            live = self.sinks - self.doors
            self._passes = [_Pass(live, self.door_free(edges), ok) for ok in (False, True)]
        return self._passes


_MISSING = {"sink": "sink {} is not defined in the scanned code",
            "door": "door {} is not defined in the scanned code",
            "allow": "allowed back door {} is not defined in the scanned code (fixed or renamed? delete it from allow)"}


def _check_rule(idx: _Index, med: Mediation, edges: list[_Call], sites: list[_Site],
                entries: list[str], on_paths: _OnPaths | None = None, every_escape: bool = False,
                guarded: frozenset[str] = frozenset()) -> list[Finding]:
    """The findings for one rule. on_paths, when given, collects every function on a path that reaches the
    sink without the door, from every entrypoint (findings show one path per call site). every_escape:
    report a function used as a value even when the path search already reports its paths. guarded: the
    functions that carry a guard (scan._guarded), for unwrapping."""
    rid, out = med.rule, []
    for root, file, line, err in idx.parse_errors:
        out.append(Finding("unresolved", rid, "unresolved/parse-error", f"cannot parse: {err}",
                           file.relative_to(root).as_posix(), line, 1, str(root)))
    t = _Targets(_declared(idx, rid, med.sinks, "sink", out), _declared(idx, rid, med.doors, "door", out),
                 _declared(idx, rid, med.allow, "allow", out))
    paths = _bypasses(idx, rid, edges, entries, t, on_paths) + _escapes(idx, rid, edges, sites, entries, t, on_paths,
                                                                     every_escape)
    out += paths + _one_per_site(paths, _Idioms(idx, rid, edges, sites, entries, t, guarded).findings(sites))
    if t.allowed:
        out += _unused_allows(idx, med, edges, sites, entries, t, guarded)
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


def _bypasses(idx: _Index, rid: str, edges: list[_Call], entries: list[str], t: _Targets,
              on_paths: _OnPaths | None) -> list[Finding]:
    """1. paths from entrypoints to the sink with the doors removed. An allowed back door is not a door:
    only a path made of allowed functions alone (the call chains frozen when allow was written) is let
    through; a path that passes any other function (a new caller, or new code an allowed one calls) is
    a bypass, even when it reaches the sink through an allowed function."""
    live = t.sinks - t.doors
    if not live:
        return []
    door_txt = ", ".join(sorted(t.doors)) or "(no door found)"
    passes = t.passes(edges)
    out: list[Finding] = []
    # one finding per call site that reaches the sink (file:line:col), with the shortest path from the
    # first entrypoint that reaches it; a site reached only through a name-matched call is unresolved
    starts = [s for s in entries if s not in t.sinks and s not in t.doors]
    if on_paths is not None:
        on_paths.defer(passes, starts, t.allowed)
    for k, names, last in _search_order(passes, starts, t.allowed):
        if on_paths is not None:
            on_paths.update(names)
        inferred_ok = k == 1
        kind = "unresolved" if inferred_ok else "bypass"
        via = " (through a name-matched call on an unknown receiver)" if inferred_ok else ""
        out.append(_finding(idx, rid, kind, "unresolved/inferred-path" if inferred_ok else "bypass",
                            f"{' -> '.join(names)} reaches the sink without passing {door_txt}{via}",
                            last.caller, last.line, last.col, names))
    return out


def _escapes(idx: _Index, rid: str, edges: list[_Call], sites: list[_Site], entries: list[str], t: _Targets,
             on_paths: _OnPaths | None, every_escape: bool = False) -> list[Finding]:
    """1b. a function used as a value is an entrypoint: whoever holds the value can call it from anywhere. A
    function taken as a value (a thread target, a callback, a dict entry, an alias, a bound method) where
    the value is made on a path that has not passed a door, outside the sinks, doors and allowed
    functions, that reaches the sink without passing a door when called, is reported where it escapes.
    That counts a door's own helper (refund -> _send -> sink): called on its own, _send skips the door. An
    allowed back door taken as a value outside allow is a new caller: a bypass. (The sink itself as a value
    is reported at the site, as unresolved/sink-escapes; a door as a value passes the door when called.)"""
    live = t.sinks - t.doors
    trusted = t.sinks | t.doors | t.allowed
    # a function used as a value by its own definition (a decorator that may keep it, a dunder name) is an
    # entrypoint; an allowed back door is frozen as one, so that is no new caller
    # an allowed module body (`pkg.mod.<module>`) excuses only its values of allowed functions (fix round 28)
    refs = [s for s in sites if s.what == "ref" and (s.owner not in trusted or (
                s.owner.endswith(".<module>") and s.owner not in t.sinks | t.doors and s.detail not in t.allowed))
            and s.detail in idx.funcs
            and idx.funcs[s.detail].node is not None and s.detail not in t.sinks and s.detail not in t.doors
            and not (s.recv == "own" and s.detail in t.allowed)]
    if not live or not refs:
        return []
    succ = t.door_free(edges)
    passes = t.passes(edges)
    # where a value can be made without passing a door: reached from an entrypoint over calls (name-matched
    # ones too) and over other escapes (`def get(): return _send`, itself taken as a value)
    reach_succ: dict[str, list[_Call]] = collections.defaultdict(list, {k: list(v) for k, v in succ.items()})
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
        for inferred_ok, ps in zip((False, True), passes):
            found = next(ps.paths(s.detail), None)
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
                                f"{_as_value(s)} is used as a value here, and called it reaches the sink without "
                                f"passing {door_txt} ({' -> '.join(names[1:])}{via}): whoever holds the value "
                                f"can call it, and the scan cannot trace those calls; call it through the "
                                f"door, or keep it inside the door", s.owner, s.line, s.col, names))
    return out


def _as_value(s: _Site) -> str:
    """What an escape site takes as a value: the function, or the module or class holding it (fix round 26),
    or the function as its own decorator's or a dunder's argument."""
    if s.recv == "own":
        return f"{s.detail} (handed to a decorator that may keep it, or a dunder Python calls implicitly)"
    if s.recv.startswith(("module:", "class:")):
        kind, _, name = s.recv.partition(":")
        return f"the {kind} {name}, which holds {s.detail},"
    return s.detail


def _door_free(edges: list[_Call], t: _Targets) -> dict[str, list[_Call]]:
    """The call graph with the doors removed, as each caller's edges."""
    sinks, doors, allowed = t.sinks, t.doors, t.allowed
    succ: dict[str, list[_Call]] = collections.defaultdict(list)
    cut: dict[tuple[int, bool], tuple[str, ...]] = {}
    for e in edges:
        if e.caller in doors:
            continue
        # a getattr of the sink name is reported at its site (forbidden/getattr-sink), except inside an
        # allowed function, where the site is excused: there the edge stays, so a path into the allowed
        # function from anything outside allow is a bypass, like a direct call
        if type(e) is _Fan:                            # each of its edges, as for a plain one
            no_sink = e.via_getattr and e.caller not in allowed
            key = (id(e.callees), no_sink)
            if key not in cut:
                got = tuple(c for c in e.callees if c not in doors and not (no_sink and c in sinks))
                cut[key] = e.callees if len(got) == len(e.callees) else got
            cs = cut[key]
            if cs:
                succ[e.caller].append(e if cs is e.callees else _Fan(e.caller, cs, e.line, e.col, e.via_getattr))
        elif e.callee not in doors and not (  # type: ignore[union-attr]
                e.via_getattr and e.callee in sinks and e.caller not in allowed):  # type: ignore[union-attr]
            succ[e.caller].append(e)
    return succ


class _Idioms:
    """2. idioms, per site. Inside an allowed back door, the ways it reaches the sink are frozen with it;
    unwrapping a guard, monkeypatching and reflection are still reported there. Each site gets the first
    idiom it matches; the idioms of different site kinds are tried in their own order."""

    def __init__(self, idx: _Index, rid: str, edges: list[_Call], sites: list[_Site], entries: list[str],
                 t: _Targets, guarded: frozenset[str] = frozenset()) -> None:
        self.idx, self.rid, self.edges, self.t, self.sites = idx, rid, edges, t, sites
        self.trusted = t.sinks | t.doors | t.allowed
        self.dominated = door_only(idx, edges, sites, entries, self.trusted)
        self.sink_names = {s.rsplit(".", 1)[1] for s in t.sinks}
        self.guarded_names = self.sink_names | {d.rsplit(".", 1)[1] for d in t.doors}
        self.guarded = guarded
        # the guarded functions with a decorator: only those have a __wrapped__ (or a wrapper's closure) to
        # lift, so only they can be what an unwrap of an object the scan cannot name reaches
        self.liftable = sorted(f for f in guarded if getattr(idx.funcs[f].node, "decorator_list", None))
        self._unwraps: set[str] | None = None
        self._safe: dict[tuple[str, bool], bool] = {}
        self._hidden: bool | None = None
        self._decorated_fns: list[str] | None = None

    def findings(self, sites: list[_Site]) -> list[Finding]:
        return [f for s in sites if (f := self._at(s)) is not None]

    def _at(self, s: _Site) -> Finding | None:
        if s.owner in self.trusted and s.what in ("ref", "attrcall", "attrref") and not (
                s.owner.endswith(".<module>") and s.owner not in self.t.sinks | self.t.doors):
            return None                                # (an allowed module body's sink value is reported)
        if s.owner in self.dominated and s.what in ("attrcall", "attrref"):
            return None                                # reached only through a door or sink
        if s.what in ("getattr", "setattr", "delattr", "hasattr"):
            return self._named(s)
        if s.what == "patch":
            return self._patch(s)
        return self._other(s)

    def _reaches(self) -> set[str]:
        """Every function with a path to a sink that passes no door (the sinks too), found once."""
        if self._unwraps is None:                      # every f with _reach([f], edges, doors) & sinks
            self._unwraps = _can_reach(self.t.sinks, self.edges, self.t.doors)
        return self._unwraps

    def _unwrap(self, s: _Site, fn: str, what: str, outside: bool) -> Finding | None:
        """An unwrap (x.__wrapped__, inspect.unwrap(x), x.__closure__, x.__dict__["__wrapped__"]) of fn (a
        scanned function, or "" when the scan cannot tell which object x is), under this rule when what it
        lifts bears on the rule: a sink, door or allowed function of the rule, or a function with a path to
        its sink that passes no door. It calls a guarded function without its guard (forbidden) only when
        x can be a guarded door: a door or target of a rule, or a function with an inv guard (scan._guarded);
        an object the scan cannot name can be one only when such a function here has a decorator (one with
        none has no __wrapped__, and a module function or method no closure). Any other unwrap that bears
        on the rule makes a call the scan cannot follow: unresolved/unwrap. An object outside the scanned
        code, or a function that does not bear on the rule, is neither."""
        if outside:
            return None
        if fn in self.idx.funcs:
            if fn not in self.trusted and fn not in self._reaches():
                return None                            # nothing it lifts reaches this rule's sink
            if fn in self.guarded:
                return self._found("forbidden", "forbidden/unwrap",
                                   f"{what} calls the guarded function {fn} without its guard", s)
            return self._found("unresolved", "unresolved/unwrap",
                               f"{what} reaches the function {fn} wraps, which can reach the sink without a "
                               f"door: the scan cannot follow that call", s)
        liftable = [f for f in self.liftable if f in self.trusted or f in self._reaches()]
        # fix round 28: reading the cells of an object the scan cannot name (`fn.__closure__`) is inspection
        # until a cell's contents are called, which the scan cannot follow: unresolved, never forbidden
        if liftable and "__closure__" not in what:
            shown = ", ".join(liftable[:3]) + (", ..." if len(liftable) > 3 else "")
            return self._found("forbidden", "forbidden/unwrap",
                               f"{what} may call a guarded function ({shown}) without its guard: the scan cannot "
                               f"tell which object it unwraps", s)
        if not self._hidden_values() and not any(
                f in self.trusted or f in self._reaches() for f in self._decorated()):
            # fix round 28: only a decorated function has a __wrapped__ (or a wrapper whose closure holds
            # it), and a closure cell holds a value taken somewhere (an escape the checks see, unless taken
            # inside a door, sink or allowed function: _hidden_values). No decorated function here bears on
            # the rule, so whatever this unwraps cannot reach the sink
            return None
        if liftable:
            shown = ", ".join(liftable[:3]) + (", ..." if len(liftable) > 3 else "")
            return self._found("unresolved", "unresolved/unwrap",
                               f"{what}: the scan cannot tell whose cells it reads; calling what one holds may run "
                               f"a guarded function ({shown}) without its guard, a call the scan cannot follow", s)
        return self._found("unresolved", "unresolved/unwrap",
                           f"{what}: the scan cannot tell which function it unwraps, so it cannot follow the call "
                           f"(a decorated function here can reach the sink without a door)", s)

    def _decorated(self) -> list[str]:
        """Every scanned function with a decorator (what has a __wrapped__ to lift), and every method
        named __wrapped__ (what `x.__wrapped__()` may call), found once."""
        if self._decorated_fns is None:
            self._decorated_fns = [f for f, fn in self.idx.funcs.items()
                                   if getattr(fn.node, "decorator_list", None) or f.endswith(".__wrapped__")]
        return self._decorated_fns

    # ---- fix round 25: a computed getattr that provably cannot reach the sink -------------------------
    def _narrowed(self, s: _Site, obj: str) -> bool:
        """getattr(x, <computed>) cannot reach this rule's sink: x is an instance (or the class) of a class
        whose family the scan sees whole, none of whose attributes is named like a sink or door or can
        reach a sink without a door; or x is an object outside the scanned code. Either way, no value that
        can reach the sink is held where the checks do not look (see _hidden_values). docs/scan.md,
        "Computed getattr on a known receiver", gives the argument."""
        if self._hidden_values():
            return False
        if s.recv:
            return self._class_safe(s.recv.rstrip("!"), s.recv.endswith("!"))
        # fix round 26: never on an object outside the scanned code. A module's attributes include every module
        # it imports (`logging.sys`, `logging.os`, `logging.config` once anything imports it), its
        # `__builtins__` and `__loader__`, and a package may re-export anything: getattr(m, name) can hand back
        # `eval`, `sys.modules` or `importlib`, which the scan reports only when it can name them
        return False

    def _hidden_values(self) -> bool:
        """A function that can reach the sink without a door (or the sink) is taken as a value inside a door,
        sink or allowed function, or code reached only through them: the escape check trusts such a value,
        so an attribute holding it could be anything a computed getattr returns."""
        if self._hidden is None:
            reach = self._reaches()
            self._hidden = any(x.what == "ref" and x.detail in reach and (
                x.owner in self.trusted or x.owner in self.dominated) for x in self.sites)
        return self._hidden

    def _class_safe(self, cls: str, exact: bool) -> bool:
        key = (cls, exact)
        if key not in self._safe:
            self._safe[key] = _family_safe(self.idx, cls, exact, self.guarded_names, self._reaches())
        return self._safe[key]

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
        if lit == "__wrapped__" and s.what in ("setattr", "delattr") and obj in self.trusted:
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"{s.what} on '__wrapped__' of {obj} replaces what unwrapping the sink or door gives", s)
        if lit == "__wrapped__":
            if s.what == "hasattr":
                return None                            # it only asks: no function comes out of it
            fn = obj if obj in self.idx.funcs else ""
            return self._unwrap(s, fn, f"{s.what}({obj if not obj.startswith('?') else 'an object'}, "
                                       f"'__wrapped__')", _outside(self.idx, obj))
        if s.what in ("setattr", "delattr") and lit in self.guarded_names:
            return self._found("forbidden", "forbidden/monkeypatch",
                               f"{s.what} on {lit!r} replaces the sink or door at run time", s)
        if s.what in ("setattr", "delattr") and lit.startswith("__") and lit not in _DESCRIPTIVE \
                and obj in self.trusted and obj in self.idx.funcs:
            return self._found("forbidden", "forbidden/monkeypatch",    # setattr(door, "__code__", ...)
                               f"{s.what} on {lit!r} of {obj} replaces what the sink or door runs at run time", s)
        if s.what in ("setattr", "delattr") and lit in CODE_ATTRS and obj.startswith("?"):
            return self._found("unresolved", "unresolved/unknown-receiver",
                               f"{s.what} on {lit!r} of a function the scan cannot name may replace what the "
                               f"sink or a door runs at run time", s)
        if not lit and s.what == "getattr" and obj not in ("?attrgetter", "?methodcaller") and self._narrowed(s, obj):
            return None                                # provably cannot reach the sink (docs/scan.md)
        if not lit and s.what == "setattr" and s.recv and self._narrowed(s, obj) \
                and _plain_sets(self.idx, s.recv.rstrip("!"), s.recv.endswith("!")):
            return None                                # fix round 28: the same proof, and no code runs on a set
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
        if fn in self.trusted and fn in self.idx.funcs and attr.startswith("__") and attr not in _DESCRIPTIVE:
            return self._found("forbidden", "forbidden/monkeypatch",   # door.__code__ = other.__code__
                               f"assigns over {s.detail}, replacing what the sink or door runs at run time", s)
        return None

    def _other(self, s: _Site) -> Finding | None:
        if s.what == "wrapped":
            shown, fn, how, ext = (s.detail.split("|") + ["", "", ""])[:4]   # how: inspect.unwrap(f), ...
            return self._unwrap(s, fn, how or f"{shown or 'an object'}.__wrapped__", ext == "outside")
        if s.what == "ref" and s.detail in self.t.sinks:
            how = "is used as a value" if not s.recv else \
                "is decorated or named so that Python calls it implicitly (an entrypoint)" if s.recv == "own" else \
                f"is held by {s.recv.partition(':')[2]}, used as a value here"
            return self._found("unresolved", "unresolved/sink-escapes",
                               f"the sink {s.detail} {how}, so calls through it cannot be traced", s)
        if s.what == "attrpatch" and s.detail in CODE_ATTRS:
            return self._found("unresolved", "unresolved/unknown-receiver",
                               f"assigns or deletes .{s.detail} on a function the scan cannot name, which may "
                               f"replace what the sink or a door runs at run time", s)
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


def _unused_allows(idx: _Index, med: Mediation, edges: list[_Call], sites: list[_Site], entries: list[str],
                   t: _Targets, guarded: frozenset[str] = frozenset()) -> list[Finding]:
    """3. an allowed back door that is no longer one: its entry would let a regression through unseen.
    An entry is in use when the scan without allow reports something in it or on a path through it."""
    on = _OnPaths()
    # every escape counts: an allowed function that takes another as a value is in use (deleting it from
    # allow would make that value a new caller)
    bare = _check_rule(idx, Mediation(med.rule, med.sinks, med.doors), edges, sites, entries, on, every_escape=True,
                       guarded=guarded)
    used = {f.owner for f in bare if f.owner} | {n for f in bare for n in f.path}
    return [_finding(idx, med.rule, "missing", "missing/allow-unused",
                     f"allowed back door {a} no longer reaches the sink without the door; delete it from "
                     f"allow (a tightening), so a regression there is caught",
                     a)
            for a in sorted(t.allowed - t.sinks - t.doors - used) if a not in on]


# a function's attributes that only describe it (fix round 28): writing one (`door.__doc__ = ...`) changes
# no code that runs; __code__, __defaults__, __kwdefaults__, __wrapped__, __call__, __class__, __dict__ do
_DESCRIPTIVE = frozenset({"__doc__", "__name__", "__qualname__", "__module__", "__annotations__"})


def _holds(idx: _Index, name: str, trusted: set[str]) -> bool:
    """name is a scanned module or class that holds a sink, door or allowed function: rebinding it
    (`pkg.repo.Repo = Fake`) replaces them all."""
    return (name in idx.modules or name in idx.classes) and any(t.startswith(name + ".") for t in trusted)


def _outside(idx: _Index, fq: str) -> bool:
    return idx.outside(fq)


# fix round 26: the classes outside the scan a narrowed family may inherit: none has a __getattr__ (or
# __getattribute__) that answers a name the class lacks with something other than what the family stored or
# declared, nor calls anything by name. The builtin types and exceptions, ABC, the Enum family (EnumType's
# class-level __getattr__ gives members only, instances of the class itself), NamedTuple, Generic and Protocol
# (a Protocol is not trusted anyway), and pydantic's BaseModel, whose __getattr__ gives a private attribute
# declared in the class body (a scanned descriptor's __get__ counts below) or an extra field: a value someone
# stored, as an instance attribute is (a function stored there was used as a value where it was stored).
_PLAIN_BASES = frozenset({
    *(f"builtins.{n}" for n, v in vars(builtins).items() if isinstance(v, type) and (
        issubclass(v, BaseException) or n in ("object", "str", "int", "float", "complex", "bool", "bytes",
                                              "bytearray", "dict", "list", "tuple", "set", "frozenset"))),
    "abc.ABC", *(f"enum.{n}" for n in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum")),
    *(f"{m}.{n}" for m in ("typing", "typing_extensions") for n in ("Generic", "NamedTuple", "Protocol")),
    "pydantic.BaseModel", "pydantic.main.BaseModel"})
# metaclasses that make classes as type does (EnumType's __getattr__ gives members only)
_PLAIN_METAS = frozenset({"builtins.type", "abc.ABCMeta", "enum.EnumMeta", "enum.EnumType"})

_CLASS_BODY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign,
               ast.Expr, ast.Pass, ast.Import, ast.ImportFrom)


def _made_by(idx: _Index, home: object, func: ast.AST) -> set[str] | None:
    """The scanned functions that run when what `func(...)` makes is read or called as a class attribute: a
    scanned function's own code (a decorator), a scanned class's constructor, __call__ and __get__ (or
    those of the class a scanned factory is annotated to return); nothing for a foreign callable; None
    when the scan cannot tell what func is."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
        # Unicode().tag(config=True): a method of what a foreign call made is foreign too
        return set() if _made_by(idx, home, func.value.func) == set() else None
    fq = home._resolve(func, set(), None) if _dotted(func) else None  # type: ignore[attr-defined]
    if fq is None:
        return None
    if fq in idx.funcs:
        made = home.return_type(fq)  # type: ignore[attr-defined]
        out = {fq}
    elif fq in idx.classes:
        made, out = fq, set()
    elif _outside(idx, fq):
        return set()
    else:
        return None
    if made is not None:
        out.update(m for m in (idx.method(made, n) for n in ("__init__", "__new__", "__call__", "__get__")) if m)
    return out


_SETTERS = frozenset({"__setattr__", "__delattr__", "__set__", "__delete__", "__set_name__"})


def _plain_sets(idx: _Index, cls: str, exact: bool) -> bool:
    """A setattr with a computed name on an instance of cls (exact: of cls itself) runs no code of its own:
    no class the instance may be (cls, its scanned subclasses unless exact, and their method resolution
    orders, all seen) defines __setattr__, __delattr__, a descriptor's __set__ / __delete__ or
    __set_name__, a decorated method (a property or other descriptor), or a class attribute made by a call
    (a descriptor) other than dataclasses.field(). Together with _family_safe (nothing in the family is
    named like the sink or a door, or can reach the sink), the name set cannot change what the rule
    checks: docs/scan.md, "Computed getattr on a known receiver"."""
    from .inference import _Inference
    family: set[str] = set()
    for k in [cls] if exact else [cls, *idx.subclasses(cls)]:
        order = idx.strict_mro(k)
        if order is None:
            return False
        family.update(order)
    for c in family:
        info = idx.classes[c]
        if _SETTERS & set(info["methods"]):
            return False
        home = _Inference(idx, idx.modules[info["module"]])
        for st in info["node"].body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _dotted(d) not in ("staticmethod", "classmethod") for d in st.decorator_list):
                return False
            value = getattr(st, "value", None) if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
            if isinstance(value, ast.Call) and not (
                    _dotted(value.func) and home._resolve(value.func, set(), None) == "dataclasses.field"):
                return False
    return True


def _held(idx: _Index, fq: str) -> set[str]:
    """What a value naming fq runs when called or read: a scanned function, a class's constructors, or a
    scanned module's functions and constructors."""
    if fq in idx.funcs and idx.funcs[fq].node is not None:
        return {fq}
    if fq in idx.classes:
        return set(idx.constructors(fq))
    if fq in idx.modules:
        return set(idx.module_members(fq))
    return set()


def _stored(idx: _Index, home: object, cls: str) -> set[str]:
    """What the methods of cls store on self by name (`self.op = payments.Sender`, `self.mod = payments`,
    `self.cb = helper`): what getattr(self, name) may hand back, and so run."""
    out: set[str] = set()
    for fid in idx.classes[cls]["methods"].values():
        fn = idx.funcs[fid].node
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or idx.funcs[fid].cls != cls:
            continue
        for n in ast.walk(fn):
            pairs = [(t, n.value) for t in n.targets] if isinstance(n, ast.Assign) else \
                [(n.target, n.value)] if isinstance(n, ast.AnnAssign) and n.value is not None else []
            for t, v in pairs:
                if isinstance(t, ast.Attribute) and _dotted(v):
                    out |= _held(idx, home._resolve(v, set(), None) or "")  # type: ignore[attr-defined]
    return out


def _family_safe(idx: _Index, cls: str, exact: bool, guarded_names: set[str], reach: set[str]) -> bool:
    """Every attribute getattr can find on an instance of cls (exact: of cls itself, or the class object)
    is known and harmless for this rule: the classes it may be (cls, and each scanned subclass unless
    exact) and their method resolution orders are all seen (no base the scan cannot place: a computed
    base, an excluded or unknown project class; a standard-library or third-party base is foreign code),
    none is subverted, defined or subclassed in excluded code, has a scanned metaclass or its own
    __getattr__ / __getattribute__; no attribute of theirs (a method, a class-level name, a nested
    class, an instance attribute set through self) is named like a sink or door; and no method, nested
    class constructor or scanned descriptor's __get__ among them can reach a sink without a door."""
    from .inference import _Inference, subverted
    if cls not in idx.classes:
        return False
    kinds = [cls] if exact else [cls, *idx.subclasses(cls)]
    family: set[str] = set()
    for k in kinds:
        order = idx.strict_mro(k)
        if order is None:
            return False
        family.update(order)
    if family & subverted(idx) or idx.excluded_subclass(family) is not None:
        return False
    members: set[str] = set()
    names: set[str] = set()
    for c in family:
        info = idx.classes[c]
        home = _Inference(idx, idx.modules[info["module"]])
        for b in idx._base_names(c):
            # fix round 26: a base outside the scan only when it is known to answer no name it lacks and to
            # call nothing by name (a tempfile wrapper forwards __getattr__ to the object it wraps)
            if b is None or (b not in idx.classes and b not in _PLAIN_BASES):
                return False
        node = info["node"]
        for kw in node.keywords:
            if kw.arg == "metaclass":
                fq = home._resolve(kw.value, set(), None) if _dotted(kw.value) else None
                if fq is None or fq not in _PLAIN_METAS:
                    return False
        methods = info["methods"]
        if "__getattr__" in methods or "__getattribute__" in methods:
            return False
        names.update(methods)
        members.update(methods.values())
        for st in node.body:
            if not isinstance(st, _CLASS_BODY):
                return False                           # an if/try/for/with block may bind any name
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(st.name)
                for d in st.decorator_list:            # what the attribute is: the decorator's result
                    got = _made_by(idx, home, d.func if isinstance(d, ast.Call) else d)
                    if got is None:
                        return False
                    members |= got
            if isinstance(st, ast.ClassDef):
                members.update(idx.constructors(f"{c}.{st.name}"))
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in st.names)
                for a in st.names:                     # a class attribute bound to a module, class or function
                    src = idx._absolute(idx.modules[info["module"]], st) if isinstance(st, ast.ImportFrom) else ""
                    full = f"{src}.{a.name}" if src else a.name if a.asname else a.name.split(".")[0]
                    members.update(_held(idx, idx.canon(idx.imported(idx.modules[info["module"]], full))))
            targets = st.targets if isinstance(st, ast.Assign) else [st.target] \
                if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []
            for t in targets:
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            value = getattr(st, "value", None) if targets else None
            if isinstance(value, ast.Call):            # x = Desc(): a scanned descriptor's __get__ runs
                got = _made_by(idx, home, value.func)
                if got is None:
                    return False
                members |= got
            elif value is not None and _dotted(value):  # x = payments.Sender: what the class attribute holds
                members.update(_held(idx, home._resolve(value, set(), None) or ""))
        names.update(home._attr_seen(c))
        members.update(_stored(idx, home, c))
    if names & guarded_names:
        return False
    return not (members & reach)
