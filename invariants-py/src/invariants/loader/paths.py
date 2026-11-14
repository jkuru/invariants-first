"""What each enforcer reads: the paths its clauses, names and providers read, whether they are set at
that point and timing, whether they bind against the target, and the old_self paths a guard copies."""
from __future__ import annotations

import inspect
from typing import Any, Iterator

from ..errors import LoadError
from ..model import DEFAULT_NAMES, EVALUATED, VIEW_POINTS, Rule
from ..registry import Registry
from ..resolve import close_matches
from .schemas import _params


def binding_problem(rule: Rule, reg: Registry, i: Any, target: Any, tk: str) -> str | None:
    """Why one enforcer (index i, or "rule") cannot bind against target: an args.<x> its clauses,
    names or provider arguments read that target does not take, or an async provider a sync target
    cannot await (every call would be a rule_error). None when it binds."""
    rid = rule.id
    m = rule.names_for.get(i, rule.names_for.get("rule", {}))
    if i in rule.names_for:
        want = {r[1] for r in rule.refs if r[0] == "args" and len(r) > 1}
        for p in m.values():
            if p.startswith("args."):
                want.add(p.split(".")[1])
            if p.startswith("provider."):
                for ppath in reg.providers[p.split(".")[1]].raw["args"].values():
                    head = ppath.split(".")[0]
                    path = m[head] + ppath[len(head):] if head in m and head not in DEFAULT_NAMES else ppath
                    if path.startswith("args."):
                        want.add(path.split(".")[1])
        missing = want - _params(target)
        if missing:
            return _unresolved_arg(rid, sorted(missing)[0], target, tk)
    if i == "rule":
        return None
    enf = rule.enforcers[i]
    if enf["point"] not in EVALUATED or enf["mode"] == "off":
        return None
    if _runs_async(target, enf["point"], enf.get("timing")):
        return None
    for p in m.values():
        if p.startswith("provider.") and reg.providers[p.split(".")[1]].is_async:
            return (f"{rid}: provider {p.split('.')[1]} is async def, but the {enf['point']} enforcer's target "
                    f"{tk} is sync, so it cannot await it (every call would be a rule_error); make {tk} async or "
                    f"the provider sync{_sync_wrapper_hint(target)}")
    return None


def _sync_wrapper_hint(target: Any) -> str:
    fn = inspect.unwrap(target)
    if not inspect.iscoroutinefunction(getattr(fn, "__func__", fn)):
        return ""
    return (" (it wraps an async def, but the function the decorator wraps is a sync wrapper, so its checks "
            "run sync before the coroutine exists: put the decorator directly on the async def, or make the "
            "wrapper async)")


_KIND = {"decorator": "guard", "middleware": "guard", "model_call": "model_call", "edge": "edge"}


def _inv_wrapper(target: Any, kind: str) -> Any:
    """The @inv.guard / @inv.model_call / @inv.edge wrapper (by kind) in target's __wrapped__ chain: what
    actually evaluates the enforcer. None when there is none (yet: inv verify reports a missing one)."""
    layer: Any = target
    for _ in range(64):
        if layer is None:
            return None
        layer = getattr(layer, "__func__", layer)          # a classmethod / staticmethod
        if getattr(layer, "__inv_kind__", None) == kind:
            return layer
        layer = getattr(layer, "__wrapped__", None)
    return None


def _runs_async(target: Any, point: str, timing: Any) -> bool:
    """Whether the enforcer's checks at point and timing run on the async path (so an async provider can
    be awaited), judged as the wrapper that runs them decides: a guard or an edge by the function it
    decorates itself (its own coroutine-ness, never unwrapped: an async wrapper of a sync def runs
    async, a sync wrapper of an async def runs sync), except that a sync guard's post_effect checks
    run async when the function returns a coroutine (a sync wrapper of an async def); a model call
    runs async for an async body and for a sync wrapper of an async def (its first reply is a
    coroutine). Without such a wrapper, the innermost function decides."""
    w = _inv_wrapper(target, _KIND.get(point, ""))
    inner = None if w is None else getattr(w, "__wrapped__", None)
    if inner is None:
        fn = inspect.unwrap(target)
        return inspect.iscoroutinefunction(getattr(fn, "__func__", fn))
    if inspect.iscoroutinefunction(w):
        return True
    if point == "edge" or (_KIND[point] == "guard" and timing != "post_effect"):
        return False
    fn = inspect.unwrap(inner)
    return inspect.iscoroutinefunction(getattr(fn, "__func__", fn))


def _unresolved_arg(rid: str, name: str, target: Any, tk: str) -> str:
    """args.<name> names no parameter of the target: the closest parameter, and every one it takes."""
    try:
        fn = inspect.unwrap(target)
        params = [p for p in inspect.signature(fn).parameters if p not in ("self", "cls")] if callable(fn) else []
    except (TypeError, ValueError):
        params = []
    close = close_matches(name, params, n=1)
    hint = f"did you mean args.{close[0]}? " if close else ""
    taken = f"its parameters: {', '.join(params)}" if params else "it takes no parameters"
    return f"{rid}: args.{name} does not resolve against {tk} ({hint}{taken})"


def _read_paths(rule: Rule, reg: Registry, i: Any) -> Iterator[tuple[str, str]]:
    """(path, how it is read) for every root path one enforcer's evaluation reads: the clauses' own
    paths, the bind entries they read, every view entry, and the arguments of the providers those
    bind. A provider argument naming a bound name reads that name's path, whether or not bad reads
    the name itself (bind: {prev: old_self.size} read only as a provider argument is still old_self)."""
    for r in sorted(rule.refs):
        if r[0] in DEFAULT_NAMES:
            yield ".".join(r), "bad"
    m = rule.names_for.get(i, {})
    view = (rule.enforcers[i].get("view") or {}) if i != "rule" else {}
    for n, path in m.items():
        if n not in rule.names and n not in view:
            continue
        how = "view" if n in view else "bind"
        head, *rest = path.split(".")
        if head == "provider":
            prov = reg.providers.get(rest[0]) if rest else None
            for a, ap in ((prov.raw.get("args") or {}) if prov else {}).items():
                if not isinstance(ap, str):
                    raise LoadError(f"provider {rest[0]}: argument {a!r} must be a path string "
                                    f"(e.g. args.account), not {type(ap).__name__}")
                ah = ap.split(".")[0]
                if ah in DEFAULT_NAMES:
                    yield ap, f"provider {rest[0]} argument {a}"
                elif ah in m and not m[ah].startswith("provider."):
                    yield m[ah] + ap[len(ah):], f"provider {rest[0]} argument {a} (bind {ah})"
        else:
            yield path, f"{how} {n}"


def _scope_problem(point: str, timing: str, path: str) -> str | None:
    """Why `path` is never set at an enforcer of this point and timing, with the fix; None if it is."""
    head, *rest = path.split(".")
    if head == "args" and rest and rest[0] == "self":
        return "args never holds self (self is its own root): write self.<field>"
    if head == "args" and rest and rest[0] == "inv_feedback":
        return ("args never holds inv_feedback (it is the parse error the SDK sends the model back); "
                "read the reply or the other arguments")
    guard = point in ("decorator", "middleware")
    if head == "result" and point in EVALUATED and not (guard and timing == "post_effect"):
        if not guard:
            return f"result is never set at a {point} enforcer: the model's parsed reply is `reply`"
        return ("result is never set before the call (pre_effect): it exists only at a post_effect decorator "
                "or middleware enforcer; read args, or put the clause on a post_effect enforcer")
    if head == "reply" and guard:
        return (f"reply is never set at a {point} enforcer: it is the parsed reply of an @inv.model_call (read "
                f"at its model_call and edge enforcers); read args or result")
    if head == "old_self" and point in ("model_call", "edge", "monitor"):
        return (f"old_self is never set at a {point} enforcer: it is the snapshot of self an @inv.guard takes "
                f"before the call; read self or args")
    if head == "old_self" and guard and timing == "pre_effect":
        return ("old_self is the snapshot of self taken before the call, so before the effect (pre_effect) it "
                "is self itself and a transition clause never fires; put it on a post_effect enforcer, or read "
                "self and args")
    return None


def _check_scopes(rule: Rule, reg: Registry) -> None:
    """LoadError for a root an enforcer never sets (it would be a rule_error on every evaluation)."""
    for i, enf in enumerate(rule.enforcers):
        point = enf["point"]
        if point not in VIEW_POINTS:
            continue                          # held elsewhere (type, gateway, build, structure)
        for path, how in _read_paths(rule, reg, i if i in rule.names_for else "rule"):
            why = _scope_problem(point, enf["timing"], path)
            if why:
                raise LoadError(f"{rule.id}: enforcer[{i}] ({point}, {enf['timing']}) reads {path} ({how}), "
                                f"but {why}")


def _snapshot_paths(rule: Rule, reg: Registry) -> list[tuple[str, ...]]:
    """The old_self paths a guard must copy before the call: read by bad, by a bind entry bad reads,
    by any enforcer's view, or by a provider argument of those (only the shortest prefixes)."""
    paths = set()
    keys = list(rule.names_for) or ["rule"]
    for i in keys:
        for path, how in _read_paths(rule, reg, i):
            head, *rest = path.split(".")
            if head != "old_self":
                continue
            if not rest:
                raise LoadError(f"{rule.id}: old_self must be followed by a field path (old_self.<path>), "
                                f"in {how}")
            paths.add(tuple(rest))
    return sorted(p for p in paths if not any(q != p and p[:len(q)] == q for q in paths))
