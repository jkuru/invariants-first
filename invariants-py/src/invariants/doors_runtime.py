"""The run-time door check: a path law's sinks, reached only through their doors, checked as the code runs.

`inv scan` (inside `inv check`) proves path laws from the source; dynamic dispatch (getattr, a DI
container, a callback, a monkeypatch) is where a static proof runs out. install() is the cheap
backstop for tests and staging:

    handle = inv.doors.install(mode="enforce")      # or "detect"; rules=None: the current registry's
    ...
    handle.uninstall()                              # or: with inv.doors.install(): ...

For every mediation rule that is not retired, install() resolves the sinks, the doors and the allow
entries (`py:` paths, as the scan reads them) and replaces each on its owner (the class or module
that holds it) with a wrapper (functools.wraps; a staticmethod stays a staticmethod, a classmethod a
classmethod, an async def an async def; it sits outermost, over an @inv.guard). A module-level function
is replaced in every loaded module that imported it by name too. While a door or an allowed back door
runs, its `py:` path is in a context variable; a sink, on entry, checks that a door it may be reached
through is in it:
- the list form (sink: [...], door: [...], allow: [...]): any door or allow entry of the rule;
- the mapping form (sink: {sink: doors}): a door of THAT sink, or an allow entry that lists that sink.
A sink named by several rules must satisfy each of them. A rule ablated now (inv.ablated, INV_ABLATE,
--inv-ablate) is skipped. A miss is a `violation` event recorded in the registry (point "door"; counted
in the pytest plugin's report like a guard's), logged at WARNING, and appended to handle.violations;
in mode "enforce" it then raises inv.BypassError (named rule, sink and doors; never an argument value).
The mode is capped by the rule's status, as every enforcer's is: a candidate or detect rule records
and never raises. `pytest --inv-doors` installs in enforce mode for the whole run, and fails the
session on any violation, even one a test caught.

Limits:
- The context variable does not cross a plain threading.Thread or loop.run_in_executor (each starts
  in a fresh context; asyncio tasks and asyncio.to_thread copy the caller's): a door that hands its
  write to a worker thread looks like a bypass. So use mode "detect" in staging, and "enforce" in tests.
- What install() replaces is the attribute on its owner (and module globals bound to a module-level
  function). A reference taken before install() (a bound method stored at start-up, a dispatch table
  holding the function itself, a closure) keeps the original: through it a sink is not checked, and a
  door does not count. Install before the app builds those (pytest --inv-doors installs after
  collection, before the first test).
- A generator door counts while its body runs (each next/send/throw), not while a caller holds it;
  an async generator likewise for each step it is awaited.
"""
from __future__ import annotations

import contextvars
import dataclasses
import functools
import inspect
import sys
import threading
import types
import typing
from typing import Any, Callable, Iterable, Literal

from .errors import BypassError, LoadError
from .model import Rule, effective_mode
from .registry import Registry, current
from .registry.events import log
from .resolve import resolve

Mode = Literal["detect", "enforce"]
POINT = "door"                                   # the point a run-time door check's events name
_ACTIVE: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("inv_doors_active", default=frozenset())
_LOCK = threading.RLock()                        # install and uninstall, one at a time
_INSTALLED: dict[Any, DoorsHandle] = {}          # config key -> the live handle (install twice: the same one)
_MISSING: Any = object()


def active() -> frozenset[str]:
    """The `py:` paths of the doors and allowed back doors running now, in this context."""
    return _ACTIVE.get()


@dataclasses.dataclass(frozen=True)
class _Demand:
    """One rule's demand on one sink: a door or allow entry in `permitted` must be running."""

    rule: str
    rev: Any
    status: str
    sink: str
    doors: tuple[str, ...]                   # what the error names
    permitted: frozenset[str]                # doors and the allow entries for this sink


@dataclasses.dataclass
class _Site:
    """One function's roles: the demands it checks as a sink, the paths it adds as a door."""

    handle: DoorsHandle
    demands: list[_Demand] = dataclasses.field(default_factory=list)
    door_ids: frozenset[str] = frozenset()
    disabled: bool = False                   # its handle is gone but the wrapper could not be taken out

    def check(self) -> None:
        if not self.demands:
            return
        running = _ACTIVE.get()
        for d in self.demands:
            if not (d.permitted & running):
                self.handle._miss(d)


@dataclasses.dataclass
class _Placement:
    """A wrapper put at owner.name: what was there before (own attribute or inherited) and what was put."""

    owner: Any
    name: str
    had_own: bool
    before: Any
    placed: Any
    site: _Site


def _own(owner: Any) -> dict[str, Any] | types.MappingProxyType[str, Any] | None:
    try:
        return vars(owner)
    except TypeError:                        # __slots__ without __dict__, a builtin
        return None


def _raw(owner: Any, name: str) -> tuple[Any, bool]:
    """The attribute as stored (a staticmethod stays one) and whether owner holds it itself."""
    own = _own(owner)
    if own is not None and name in own:
        return own[name], True
    if inspect.isclass(owner):
        for k in owner.__mro__[1:]:
            kd = _own(k)
            if kd is not None and name in kd:
                return kd[name], False
    return getattr(owner, name), False       # a bound method of an instance, through its class


def _split(path: str) -> tuple[Any, str]:
    """'py:pkg.mod.Cls.fn' -> (the object holding fn, 'fn'); LoadError when it does not resolve."""
    resolve(path, "door check")              # the whole path: resolve()'s own errors
    body = path[3:]
    if "." not in body:
        raise LoadError(f"door check {path}: names a module, not a function")
    head, name = body.rsplit(".", 1)
    return resolve("py:" + head, "door check"), name


def _demands(rule: Rule) -> tuple[list[_Demand], set[str]] | None:
    """A rule's demands, per sink, and the paths that act as its doors (doors and allow entries); None
    for a rule with no path law (no mediation.sink)."""
    med = rule.raw.get("mediation")
    if not isinstance(med, dict) or "sink" not in med:
        return None
    from invariants_scan import ScanConfigError
    from invariants_scan.mediation import allow_map_of, pairs_of
    rid, rev, status = rule.id, rule.raw.get("rev"), rule.status
    out: list[_Demand] = []
    try:
        if isinstance(med["sink"], dict):        # each sink with its own doors; allow: entry -> its sinks
            pairs = pairs_of(rid, med["sink"])
            allow_to = allow_map_of(rid, med["allow"], list(pairs)) if "allow" in med else {}
            for s, ds in pairs.items():
                allowed = [a for a, ss in allow_to.items() if s in ss]
                out.append(_Demand(rid, rev, status, s, tuple(ds), frozenset(ds) | frozenset(allowed)))
            doors = {d for ds in pairs.values() for d in ds} | set(allow_to)
            return out, doors
    except ScanConfigError as e:
        raise LoadError(str(e)) from None

    def paths(v: Any) -> list[str]:
        return [v] if isinstance(v, str) else list(v or [])
    sinks = paths(med["sink"])
    door = med.get("door", (rule.raw.get("subject") or {}).get("target"))
    if door is None:
        raise LoadError(f"{rid}: mediation.sink needs mediation.door or a subject.target")
    ds, allow = paths(door), paths(med.get("allow"))
    for s in sinks:
        out.append(_Demand(rid, rev, status, s, tuple(ds), frozenset(ds) | frozenset(allow)))
    return out, set(ds) | set(allow)


def _rules_of(rules: Registry | Iterable[Rule] | None, registry: Registry | None) -> list[Rule]:
    if rules is None:
        rules = registry if registry is not None else current()
    if isinstance(rules, Registry):
        return list(rules._view().rules.values())
    out = list(rules)
    for r in out:
        if not isinstance(r, Rule):
            raise TypeError(f"inv.doors.install: rules must be a Registry or Rule objects, got {type(r).__name__}")
    return out


class DoorsHandle:
    """A live inv.doors.install(): uninstall() (or leaving its `with` block) puts every original back.

    rules: the ids of the path laws it checks. violations: one dict per miss (rule, sink, mode), in order;
    the pytest plugin adds the test's node id."""

    def __init__(self, mode: Mode, registry: Registry | None, key: Any) -> None:
        self.mode, self.registry, self._key = mode, registry, key
        self.rules: list[str] = []
        self.violations: list[dict[str, Any]] = []
        self._placements: list[_Placement] = []
        self._count = 0                      # install() calls this handle answers; the last uninstall restores
        self._vlock = threading.Lock()

    @property
    def installed(self) -> bool:
        return self._count > 0

    def __enter__(self) -> DoorsHandle:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    def _miss(self, d: _Demand) -> None:
        """A sink reached without one of its doors: skipped if the rule is ablated, else recorded, then raised
        when the mode in force (the handle's, capped by the rule's status) is enforce."""
        reg = self.registry if self.registry is not None else current()
        if reg.skips(d.rule):
            return
        mode = effective_mode(self.mode, d.status)
        msg = (f"{d.rule}: {d.sink} was reached without passing its door "
               f"({' or '.join(d.doors)}{', or an allowed back door' if d.permitted - set(d.doors) else ''})")
        ev: dict[str, Any] = {"rule": d.rule, "rev": d.rev, "outcome": "violation", "mode": mode, "point": POINT,
                              "timing": "pre_effect", "messages": [msg], "error": None, "attempts": None,
                              "action": "reject" if mode == "enforce" else "proceed", "sink": d.sink}
        with self._vlock:
            self.violations.append({"rule": d.rule, "sink": d.sink, "mode": mode})
        reg.record(d.rule, "violation", ev)
        log.warning("invariant %s", ev)
        if mode == "enforce":
            raise BypassError(msg, rule=d.rule, point=POINT)

    def uninstall(self) -> None:
        """Undo this install(): with install() called n times for the same rules, the n-th uninstall puts
        back the exact objects that were there (an inherited method is deleted from the subclass again)."""
        with _LOCK:
            if self._count == 0:
                return
            self._count -= 1
            if self._count:
                return
            _INSTALLED.pop(self._key, None)
            for p in reversed(self._placements):
                _take_out(p)
            self._placements.clear()


_PLACED: dict[int, _Placement] = {}         # id(placed object) -> its placement, while it may be found


def _take_out(p: _Placement) -> None:
    """Restore p.before at p.owner.name if p's wrapper is still what is there; else (another install wrapped
    over it since) leave it in, passing straight through, for that one's uninstall to take out."""
    p.site.disabled = True
    own = _own(p.owner)
    now = own.get(p.name, _MISSING) if own is not None else getattr(p.owner, p.name, _MISSING)
    if now is not p.placed:
        return
    before, had_own = p.before, p.had_own
    while id(before) in _PLACED and _PLACED[id(before)].site.disabled and _PLACED[id(before)].placed is before:
        inner = _PLACED.pop(id(before))     # a disabled wrapper left under this one: take it out too
        before, had_own = inner.before, inner.had_own
    if had_own:
        setattr(p.owner, p.name, before)
    else:
        delattr(p.owner, p.name)
    _PLACED.pop(id(p.placed), None)


def _drive(gen: Any, ids: frozenset[str]) -> Any:
    """A generator door: ids are active while its body runs, step by step."""
    send: Any = None
    exc: BaseException | None = None
    while True:
        token = _ACTIVE.set(_ACTIVE.get() | ids)
        try:
            item = gen.throw(exc) if exc is not None else gen.send(send)
        except StopIteration as e:
            return e.value
        finally:
            _ACTIVE.reset(token)
        try:
            send, exc = (yield item), None
        except GeneratorExit:
            gen.close()
            raise
        except BaseException as e:
            send, exc = None, e


async def _adrive(agen: Any, ids: frozenset[str]) -> Any:
    """An async generator door: ids are active while each step is awaited."""
    send: Any = None
    exc: BaseException | None = None
    while True:
        token = _ACTIVE.set(_ACTIVE.get() | ids)
        try:
            item = await (agen.athrow(exc) if exc is not None else agen.asend(send))
        except StopAsyncIteration:
            return
        finally:
            _ACTIVE.reset(token)
        try:
            send, exc = (yield item), None
        except GeneratorExit:
            await agen.aclose()
            raise
        except BaseException as e:
            send, exc = None, e


def _wrapper(fn: Callable[..., Any], site: _Site) -> Callable[..., Any]:
    """fn with site's check on entry and site's door paths active while it runs."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def acall(*a: Any, **kw: Any) -> Any:
            if site.disabled:
                return await fn(*a, **kw)
            site.check()
            if not site.door_ids:
                return await fn(*a, **kw)
            token = _ACTIVE.set(_ACTIVE.get() | site.door_ids)
            try:
                return await fn(*a, **kw)
            finally:
                _ACTIVE.reset(token)
        w: Any = acall
    else:
        gen, agen = inspect.isgeneratorfunction(fn), inspect.isasyncgenfunction(fn)

        @functools.wraps(fn)
        def call(*a: Any, **kw: Any) -> Any:
            if site.disabled:
                return fn(*a, **kw)
            site.check()
            if not site.door_ids:
                return fn(*a, **kw)
            if gen:
                return _drive(fn(*a, **kw), site.door_ids)
            if agen:
                return _adrive(fn(*a, **kw), site.door_ids)
            token = _ACTIVE.set(_ACTIVE.get() | site.door_ids)
            try:
                return fn(*a, **kw)
            finally:
                _ACTIVE.reset(token)
        w = call
    w.__inv_doors__ = site
    return typing.cast(Callable[..., Any], w)


def _placed_for(raw: Any, owner: Any, path: str, site_of: Callable[[Any], _Site]) -> Any:
    """What to put at owner.name for raw (the attribute as stored): the wrapper, in raw's own descriptor."""
    if isinstance(raw, staticmethod):
        return staticmethod(_wrapper(raw.__func__, site_of(raw.__func__)))
    if isinstance(raw, classmethod):
        return classmethod(_wrapper(raw.__func__, site_of(raw.__func__)))
    if isinstance(raw, types.FunctionType) or inspect.ismethod(raw):
        return _wrapper(raw, site_of(raw))
    if inspect.isclass(owner) and hasattr(type(raw), "__get__"):
        raise LoadError(f"door check {path}: a {type(raw).__name__} on a class; the run-time check wraps "
                        f"functions, methods, staticmethods and classmethods")
    if not callable(raw):
        raise LoadError(f"door check {path}: not callable ({type(raw).__name__})")
    w = _wrapper(raw, site_of(raw))
    return staticmethod(w) if inspect.isclass(owner) else w      # a callable object on a class binds no self


def _key_of(fn: Any) -> Any:
    if inspect.ismethod(fn):
        return ("method", id(fn.__self__), id(fn.__func__))
    return ("fn", id(fn))


def _config_key(rules: list[Rule], mode: str, registry: Registry | None) -> Any:
    return (mode, id(registry), tuple((r.id, id(r.raw)) for r in rules))


def install(rules: Registry | Iterable[Rule] | None = None, *, mode: Mode = "detect",
            registry: Registry | None = None) -> DoorsHandle:
    """Check every path law's sinks at run time (see the module docstring). rules: a loaded Registry
    (inv.load(path, install=False)) or Rule objects; None: the rules of `registry`, else of the current
    registry. registry: where misses are recorded and ablation is read (None: the current one at each
    miss). mode: "detect" (record and warn) or "enforce" (record, then raise inv.BypassError).

    Installing the same rules again returns the live handle (one wrapper per function; uninstall once
    per install). Different rules stack over it. A sink, door or allow entry that does not resolve is
    a LoadError, and nothing is left wrapped."""
    if mode not in ("detect", "enforce"):
        raise ValueError(f"inv.doors.install: mode must be 'detect' or 'enforce', not {mode!r}")
    chosen = [r for r in _rules_of(rules, registry) if r.status != "retired"]
    with _LOCK:
        key = _config_key(chosen, mode, registry)
        live = _INSTALLED.get(key)
        if live is not None:
            live._count += 1
            return live
        handle = DoorsHandle(mode, registry, key)
        try:
            _place_all(handle, chosen)
        except BaseException:
            for p in reversed(handle._placements):
                _take_out(p)
            raise
        handle._count = 1
        _INSTALLED[key] = handle
        return handle


def _place_all(handle: DoorsHandle, rules: list[Rule]) -> None:
    demands: dict[str, list[_Demand]] = {}       # sink path -> demands
    door_paths: set[str] = set()
    for rule in rules:
        got = _demands(rule)
        if got is None:
            continue
        ds, doors = got
        handle.rules.append(rule.id)
        for d in ds:
            demands.setdefault(d.sink, []).append(d)
        door_paths |= doors
    # each function once, whatever paths name it: its demands as a sink, every path naming it as a door
    sites: dict[Any, _Site] = {}
    where: list[tuple[str, Any, str, Any, bool]] = []
    for path in sorted(set(demands) | door_paths):
        owner, name = _split(path)
        raw, had_own = _raw(owner, name)
        base = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
        site = sites.setdefault(_key_of(base), _Site(handle))
        site.demands.extend(demands.get(path, ()))
        if path in door_paths:
            site.door_ids = site.door_ids | {path}
        where.append((path, owner, name, raw, had_own))

    def site_of(fn: Any) -> _Site:
        return sites[_key_of(fn)]
    done: set[tuple[int, str]] = set()
    for path, owner, name, raw, had_own in where:
        if (id(owner), name) in done:
            continue
        placed = _placed_for(raw, owner, path, site_of)
        _put(handle, owner, name, had_own, raw, placed, site_of(_base(raw)))
        done.add((id(owner), name))
        if inspect.ismodule(owner) and isinstance(raw, types.FunctionType):
            for mod in list(sys.modules.values()):           # `from shop import refund` elsewhere
                own = _own(mod) if mod is not None else None
                if mod is owner or own is None:
                    continue
                for k, v in list(own.items()):
                    if v is raw and (id(mod), k) not in done:
                        _put(handle, mod, k, True, raw, placed, site_of(raw))
                        done.add((id(mod), k))


def _base(raw: Any) -> Any:
    return raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw


def _put(handle: DoorsHandle, owner: Any, name: str, had_own: bool, before: Any, placed: Any, site: _Site) -> None:
    try:
        setattr(owner, name, placed)
    except (AttributeError, TypeError) as e:
        raise LoadError(f"door check: cannot wrap {name} on {getattr(owner, '__name__', type(owner).__name__)}: "
                        f"{type(e).__name__}") from None
    p = _Placement(owner, name, had_own, before, placed, site)
    handle._placements.append(p)
    _PLACED[id(placed)] = p


def installed() -> list[DoorsHandle]:
    """The live handles, oldest first."""
    with _LOCK:
        return list(_INSTALLED.values())


__all__ = ["BypassError", "DoorsHandle", "Mode", "POINT", "active", "install", "installed"]
