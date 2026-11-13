"""What verify() and standing() check beyond the registry itself: chokepoint prose, ablation results,
and whether a guard wraps a target."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping

from ..model import Rule
from ..resolve import resolve

if TYPE_CHECKING:
    from . import Registry


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


def _ablation_problems(rule: Rule, data: Mapping[str, Any]) -> list[str]:
    """Why ablation does not back the rule, from the pytest plugin's report (--inv-report-json)."""
    abl = data.get("ablation", data)
    if not isinstance(abl, Mapping):
        return ["ablation: the report holds no ablation results"]
    if rule.id in (abl.get("not_load_bearing") or []):
        return ["ablation: guard not load-bearing (a falsifier still passes with the rule off)"]
    rows = (abl.get("rules") or {}).get(rule.id)
    if not rows:
        return ["ablation: no falsifier of this rule ran under pytest --inv-ablate"]
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
