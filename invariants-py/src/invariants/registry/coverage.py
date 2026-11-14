"""What verify() and standing() check beyond the registry itself: chokepoint prose, ablation results,
whether a guard wraps a target, the targets and mediation of a rule, and WRAPPED (every guarded
callable in the process, which verify reads)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import weakref
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

from ..model import GUARD_POINTS, Rule
from ..resolve import edit_distance as edits, resolve

if TYPE_CHECKING:
    from . import Registry, _View


def _edge_problem(rule: Rule, w: Any) -> str | None:
    """Why an @inv.edge(rule.id) wrapper w is misplaced: no edge enforcer of the rule targets it (at
    run time it would borrow the rule's only edge enforcer, which names another function)."""
    targets = [e.get("target") for e in rule.enforcers if e.get("point") == "edge"]
    for t in targets:
        try:
            if _wraps(resolve(str(t)), w):
                return None
        except Exception:
            continue
    if not targets:
        return f"@inv.edge({rule.id!r}), but {rule.id} has no edge enforcer"
    return f"@inv.edge({rule.id!r}), but no edge enforcer of {rule.id} targets it (they target {', '.join(map(str, targets))})"


def _targetless_problems(reg: Registry, rule: Rule, w: Any) -> list[str]:
    """A guard on a rule without subject.target: the load-time binding checks, against the function
    the guard wraps (the only target there is)."""
    from ..loader import binding_problem
    points = set(getattr(w, "__inv_points__", ("decorator",)))
    out = []
    for i, e in enumerate(rule.enforcers):
        if e.get("point") in points:
            try:
                why = binding_problem(rule, reg, i, w, w.__qualname__)
            except Exception as ex:
                why = f"{rule.id}: its bindings could not be checked against {w.__qualname__} ({type(ex).__name__})"
            if why:
                out.append(why)
    return out


def _resolves(key: str) -> bool:
    try:
        resolve(key)
    except Exception:
        return False
    return True


def _function_names(rule: Rule) -> set[str]:
    """The short names of the functions a rule names: its target, edge targets, door and sink."""
    med = rule.raw.get("mediation") or {}
    keys: list[Any] = [rule.target_key, *(e.get("target") for e in rule.enforcers)]
    for k in ("door", "sink"):
        v = med.get(k)
        keys.extend([v] if isinstance(v, str) else v if isinstance(v, list) else [])
    return {k[3:].split(".")[-1] for k in keys if isinstance(k, str) and k.startswith("py:")}


_PY_PATH = re.compile(r"py:[A-Za-z_][\w.]*\w")
_WORD = re.compile(r"[A-Za-z_][\w.]*")


def _chokepoint_problems(rule: Rule, text: str) -> list[str]:
    """mediation.chokepoint is prose, checked where it can be: every py: path in it resolves, and it
    names at least one function the rule names (its target, an edge target, the door or the sink)."""
    out = [f"mediation.chokepoint names {p}, which does not resolve"
           for p in _PY_PATH.findall(text) if not _resolves(p)]
    known = _function_names(rule)
    words = {w for tok in _WORD.findall(text) for w in tok.split(".")}
    if known and not out and not words & known:
        out.append(f"mediation.chokepoint names none of the rule's functions ({', '.join(sorted(known))}): "
                   f"say which function every path crosses")
    return out


_PROSE = frozenset({"id", "rev", "status", "evidence", "statement", "why", "owner", "class"})


def rule_digest(rule: Rule) -> str:
    """The digest of what an ablation result is about: every field of the rule as loaded but its
    prose, id, rev, status and evidence (so its bad, enforcers, model_policy, subject, mediation and
    falsified_by), and the records it reads: the providers its names bind and its targets' (Rule.bound),
    so a provider switched to another function, or a target's effects changed, is another version.
    The pytest plugin writes it per rule into --inv-report-json; a report whose digest differs judged
    another version of the rule."""
    sem = {k: v for k, v in rule.raw.items() if k not in _PROSE}
    if rule.bound:
        sem["(bound records)"] = rule.bound    # a key no rule field can have
    data = json.dumps(sem, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _ablation_problems(rule: Rule, data: Mapping[str, Any]) -> list[str]:
    """Why ablation does not back the rule, from the pytest plugin's report (--inv-report-json).

    The report must be for this version of the rule: its digest for the rule (ablation.digests)
    must be rule_digest(rule). A report for another version (bad, an enforcer, falsified_by or a
    record it reads changed since it ran), or one that names no digest, proves nothing about this one."""
    abl = data.get("ablation", data)
    if not isinstance(abl, Mapping):
        return ["ablation: the report holds no ablation results"]
    digests = abl.get("digests")
    digest = digests.get(rule.id) if isinstance(digests, Mapping) else None
    if digest is not None and digest != rule_digest(rule):
        return ["ablation: the report is for another version of the rule (its bad, enforcers, falsified_by, or "
                "a provider or target record it reads changed since pytest --inv-ablate ran); run it again"]
    if rule.id in (abl.get("not_load_bearing") or []):
        return ["ablation: guard not load-bearing (a falsifier still passes with the rule off)"]
    rows = (abl.get("rules") or {}).get(rule.id)
    if not rows:
        return ["ablation: no falsifier of this rule ran under pytest --inv-ablate"]
    if digest is None:
        return ["ablation: the report does not say which version of the rule it ablated (no digest); run "
                "pytest --inv-ablate --inv-report-json again"]
    if not any(isinstance(r, Mapping) and r.get("result") == "caught" for r in rows):
        return ["ablation: no falsifier caught the ablation ("
                + ", ".join(str(r.get("result")) for r in rows if isinstance(r, Mapping)) + ")"]
    return []


def _wraps(target: Any, w: Any) -> bool:
    """True if w is target, or is reached from it through __wrapped__ / __func__ (stacked decorators)."""
    obj, seen = target, 0
    while obj is not None and seen < 32:
        if obj is w:
            return True
        # __func__ first: a bound method forwards __wrapped__ to the inner function, skipping w
        obj = getattr(obj, "__func__", None) or getattr(obj, "__wrapped__", None)
        seen += 1
    return False


def _scope(view: _View) -> set[str]:
    """Top-level packages of one generation's targets, edge targets and python contracts."""
    out: set[str] = set()
    keys: list[Any] = list(view.targets)
    for rule in view.rules.values():
        keys.append(rule.target_key)
        keys.extend(e.get("target") for e in rule.enforcers)
    for key in keys:
        if isinstance(key, str) and key.startswith("py:"):
            out.add(key[3:].split(".")[0])
    for c in view.contracts.values():
        if isinstance(c, dict) and c.get("type") == "python" and isinstance(c.get("ref"), str):
            out.add(c["ref"].split(":")[0].split(".")[0])
    return out


def _target_problems(rule: Rule) -> list[str]:
    """Why a rule's target or edge targets do not carry its guard (verify's first check)."""
    problems: list[str] = []
    if rule.status == "retired" or rule.target is None:
        return problems
    points = {e["point"] for e in rule.enforcers} & GUARD_POINTS
    if rule.policy or points:
        keys = getattr(rule.target, "__inv_rules__", ())
        kind = getattr(rule.target, "__inv_kind__", "")
        if rule.id not in keys and rule.target_key not in keys:
            problems.append(_not_named(rule, keys, kind))
        elif not rule.policy and kind != "guard":
            problems.append(f"{rule.id}: {rule.target_key} is wrapped by @inv.{kind or '?'}, not by "
                            f"@inv.guard: its decorator/middleware enforcers are never evaluated")
        elif not rule.policy and points - set(getattr(rule.target, "__inv_points__", ("decorator",))):
            missing = sorted(points - set(getattr(rule.target, "__inv_points__", ("decorator",))))
            problems.append(f"{rule.id}: the guard on {rule.target_key} does not evaluate point "
                            f"{', '.join(missing)}")
        elif rule.policy and getattr(rule.target, "__inv_kind__", "") != "model_call":
            problems.append(f"{rule.id}: has model_policy but target is not @inv.model_call")
        elif rule.policy and getattr(rule.target.__inv_schema__, "__name__", "") != rule.policy.get("schema"):
            got = getattr(rule.target.__inv_schema__, "__name__", "an unnamed schema")
            problems.append(f"{rule.id}: @inv.model_call on {rule.target_key} parses into {got}, but "
                            f"model_policy.schema is {rule.policy.get('schema')!r}: name the same schema in both")
    for e in rule.enforcers:
        if e["point"] == "edge":
            t = resolve(e["target"])
            if getattr(t, "__inv_kind__", "") != "edge" or rule.id not in getattr(t, "__inv_rules__", ()):
                problems.append(f"{rule.id}: edge target {e['target']} is not wrapped by @inv.edge({rule.id!r})")
    return problems


def _not_named(rule: Rule, keys: Any, kind: str) -> str:
    """Why a target's guard does not count for this rule: no guard at all, or one that names other rules."""
    if not keys:
        return f"{rule.id}: {rule.target_key} is not wrapped by its guard"
    named = ", ".join(repr(k) for k in keys)
    deco = f"@inv.{kind or 'guard'}({named})"
    if kind not in ("", "guard"):
        return (f"{rule.id}: {rule.target_key} is wrapped by {deco}, which does not name {rule.id}, and is not "
                f"wrapped by its guard")
    typo = [k for k in keys if isinstance(k, str) and not k.startswith("py:") and 0 < edits(k, rule.id) <= 2]
    return (f"{rule.target_key} is guarded by {deco}, which does not name {rule.id}: add {rule.id!r} to the "
            f"decorator (or use @inv.guard({rule.target_key!r}))"
            + (f", or fix {typo[0]!r} if it is a typo of {rule.id!r}" if typo else ""))


def _mediation_problems(rule: Rule) -> list[str]:
    """Why a rule's mediation keeps it from held: no chokepoint, residual paths, live back doors."""
    why: list[str] = []
    med = rule.raw.get("mediation") or {}
    if not med.get("chokepoint"):
        why.append("mediation names no chokepoint")
    else:
        why.extend(_chokepoint_problems(rule, str(med["chokepoint"])))
    residual = med.get("residual_paths") or []
    if residual:
        why.append(f"residual paths: {residual}")
    allow = med.get("allow") or []
    live = [a for a in ([allow] if isinstance(allow, str) else allow)
            if isinstance(a, str) and a not in residual and _resolves(a)]
    if live:
        why.append(f"mediation.allow keeps {len(live)} live back door(s) to the sink: {', '.join(live)} "
                   f"(remove them, or declare each in mediation.residual_paths)")
    return why


class _Wrapped(weakref.WeakSet[Callable[..., Any]]):
    """A WeakSet whose adds and iteration take one lock: verify() iterating it while another thread
    decorates a function would otherwise raise "Set changed size during iteration" (Python < 3.13)."""

    def __init__(self, data: Iterable[Callable[..., Any]] | None = None) -> None:
        self._lock = threading.Lock()
        super().__init__(data)

    def add(self, item: Callable[..., Any]) -> None:
        with self._lock:
            super().add(item)

    def discard(self, item: Callable[..., Any]) -> None:
        with self._lock:
            super().discard(item)

    def snapshot(self) -> list[Callable[..., Any]]:
        """The live members now, copied under the lock."""
        with self._lock:
            return list(super().__iter__())

    def __iter__(self) -> Iterator[Callable[..., Any]]:
        return iter(self.snapshot())

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()


# Every guarded callable in the process, for verify. Weak, so guards defined in a
# function (tests) drop out once they are garbage.
WRAPPED: _Wrapped = _Wrapped()
