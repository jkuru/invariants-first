"""Explaining the changes inside one lock entry: a rule (its bad clauses, enforcers, mediation,
model_policy and other fields, each judged by FIELD_POLICY), a target, provider or contract entry
(pins, renamed OpenAPI operations, signature spellings), and the pinned scan configuration. Pure."""
from __future__ import annotations

import ast
import json
from typing import Any

from .cel_canon import Typer, _clause_key, _disjuncts, _unique
from .cel_mono import judge_bad
from .contracts import (ANALYSED, COMPUTED, DEFAULT_SCAN_EXCLUDE, FIELD_POLICY, MORE_IS_WEAKER,
                        NEUTRAL, NOTES, PIN, PRESENCE, PROSE, RANKED, RANKS, Change, _json_paths,
                        policy, same)
from .schema_diff import _param_schema_changes, constraint_weakening


# --- bad: canonical trees and polarity -----------------------------------------------------------
def _clauses(bad: Any) -> list[tuple[str, str]]:
    if isinstance(bad, str):
        return [(bad, "")]
    if not isinstance(bad, list):                  # a malformed stored lock: compared as text
        return [] if bad is None else [(json.dumps(bad), "")]
    out = []
    for c in bad:
        if isinstance(c, dict):
            out.append((str(c.get("when", "")), str(c.get("message", ""))))
        else:
            out.append((str(c), ""))
    return out


def _bad_changes(where: str, old: Any, new: Any, typer: Typer | None = None) -> list[Change]:
    """Clause by clause, on canonical trees. Whitespace, commuted operands, a disjunction split into
    clauses (or merged from them) and a repeated clause dropped are no change. A removed clause, or
    an edit that makes the rule match less, or one whose direction cannot be decided, is WEAKENED."""
    from ..cel import subset
    a, b = _clauses(old), _clauses(new)
    ka = [_clause_key(w, subset.parse, typer) for w, _ in a]
    kb = [_clause_key(w, subset.parse, typer) for w, _ in b]
    a_left = list(range(len(a)))
    b_left = list(range(len(b)))
    out = _matched_clauses(where, a, b, ka, kb, a_left, b_left)
    if not a_left and not b_left:
        return out
    if a_left and b_left:                              # regrouped: the same disjuncts in other clauses
        da = sorted(_unique([d for i in a_left for d in _disjuncts(ka[i])]), key=repr)
        db = sorted(_unique([d for j in b_left for d in _disjuncts(kb[j])]), key=repr)
        if da == db:
            out.append(Change(where, "bad clauses regrouped", [_label(a[i]) for i in a_left],
                              [_label(b[j]) for j in b_left], False,
                              "the same conditions, split into clauses or merged; no change in what is flagged"))
            return out
    return out + _edited_clauses(where, a, b, a_left, b_left, typer)


def _matched_clauses(where: str, a: list[tuple[str, str]], b: list[tuple[str, str]], ka: list[Any], kb: list[Any],
                     a_left: list[int], b_left: list[int]) -> list[Change]:
    """Clauses whose canonical condition is on both sides (perhaps with a new message, or repeated);
    their indices are removed from a_left and b_left."""
    out: list[Change] = []
    for i in list(a_left):                             # the same condition, perhaps a new message
        j = next((j for j in b_left if kb[j] == ka[i]), None)
        if j is None:
            continue
        a_left.remove(i)
        b_left.remove(j)
        if a[i][1] != b[j][1]:
            out.append(Change(where, f"bad message of {b[j][0]!r}", a[i][1], b[j][1]))
        elif a[i][0] != b[j][0]:
            out.append(Change(where, "bad (equivalent rewrite)", a[i][0], b[j][0], False,
                              "the same condition after normalisation"))
    for i in list(a_left):                             # a repeat of a clause that is still there
        if ka[i] in kb:
            a_left.remove(i)
            out.append(Change(where, "bad repeated clause removed", _label(a[i]), None, False,
                              "the same condition is still a clause; no change in what is flagged"))
    for j in list(b_left):
        if kb[j] in ka:
            b_left.remove(j)
            out.append(Change(where, "bad repeated clause added", None, _label(b[j]), False,
                              "the same condition is already a clause"))
    return out


def _edited_clauses(where: str, a: list[tuple[str, str]], b: list[tuple[str, str]], a_left: list[int],
                    b_left: list[int], typer: Typer | None) -> list[Change]:
    """The clauses left unmatched: paired by message, then by position, and judged by direction;
    unequal leftovers are compared as one disjunction; the rest were removed (WEAKENED) or added."""
    out: list[Change] = []
    pairs: list[tuple[int, int]] = []
    for i in list(a_left):                             # pair by message, then by position
        j = next((j for j in b_left if a[i][1] and b[j][1] == a[i][1]), None)
        if j is not None:
            pairs.append((i, j))
            a_left.remove(i)
            b_left.remove(j)
    if len(a_left) == len(b_left):
        pairs += list(zip(a_left, b_left))
        a_left, b_left = [], []
    for i, j in pairs:
        (w, m), (w2, _) = a[i], b[j]
        out.append(_direction_change(where, f"bad{' [' + m + ']' if m else ''}", w, w2, typer=typer))
    if a_left and b_left:                              # unequal counts: compare the leftovers as one disjunction
        w = " || ".join(f"({a[i][0]})" for i in a_left)
        w2 = " || ".join(f"({b[j][0]})" for j in b_left)
        out.append(_direction_change(where, "bad clauses", [_label(a[i]) for i in a_left],
                                     [_label(b[j]) for j in b_left], w, w2, typer))
        return out
    for i in a_left:
        out.append(Change(where, "bad clause removed", _label(a[i]), None, True))
    for j in b_left:
        out.append(Change(where, "bad clause added", None, _label(b[j])))
    return out


def _label(c: tuple[str, str]) -> str:
    return c[0] + (f" [{c[1]}]" if c[1] else "")


def _direction_change(where: str, label: str, old: Any, new: Any, w: str | None = None,
                      w2: str | None = None, typer: Typer | None = None) -> Change:
    from ..cel import subset
    direction, sites = judge_bad(old if w is None else w, new if w2 is None else w2, subset.parse, typer)
    if direction == "same":
        return Change(where, label, old, new, False, "the same condition after normalisation")
    if direction == "weaker":
        return Change(where, label, old, new, True, "the condition now matches fewer cases")
    if direction == "stronger":
        return Change(where, label, old, new, False, "the condition now matches more cases")
    if sites:
        return Change(where, label, old, new, True,
                      f"the new condition can raise an error the old one could not ({', '.join(sites)}); "
                      "with on_error: fail_open_and_page an error lets the call through, so this is "
                      "treated as a possible weakening")
    return Change(where, label, old, new, True, "the direction of the change cannot be decided; "
                                                "treated as a possible weakening")


# --- per-field changes --------------------------------------------------------------------------
def _field_change(where: str, label: str, loc: str, old: Any, new: Any) -> list[Change]:
    """One field's change, judged by FIELD_POLICY[loc]. Dict values are shown per changed key."""
    pol = policy(loc)
    note = next((NOTES[k] for k in (loc, loc.rsplit(".", 1)[0]) if k in NOTES), "")
    if pol in (NEUTRAL, PIN, PROSE, COMPUTED, ANALYSED):
        return [Change(where, f"{label}{'.' + p if p else ''}", x, y)
                for p, x, y in _json_paths(old, new)] if isinstance(old, dict) and isinstance(new, dict) \
            else [Change(where, label, old, new)]
    if pol == RANKED:
        rank = RANKS.get(loc, {})
        ko, kn = (v if isinstance(v, str) else json.dumps(v) for v in (old, new))
        if ko in rank and kn in rank:
            weak = rank[kn] < rank[ko]
            return [Change(where, label, old, new, weak, note if weak else "")]
        return [Change(where, label, old, new, True, "an unranked value; treated as a possible weakening")]
    if pol == MORE_IS_WEAKER:
        if isinstance(old, int) and isinstance(new, int) and not isinstance(old, bool) and not isinstance(new, bool):
            weak = new > old
            return [Change(where, label, old, new, weak, note if weak else "")]
        return [Change(where, label, old, new, True, note or "treated as a possible weakening")]
    if pol == PRESENCE:
        weak = new is None or (old is not None)        # removed, or changed to another value
        return [Change(where, label, old, new, weak, note if weak else "")]
    # WEAKENING (and anything unlisted)
    why = note or "an unclassified field; treated as a possible weakening"
    if isinstance(old, dict) and isinstance(new, dict):
        return [Change(where, f"{label}.{p}", x, y, True, why) for p, x, y in _json_paths(old, new)]
    return [Change(where, label, old, new, True, why)]


def _enforcer_key(e: Any, i: int) -> str:
    if not isinstance(e, dict):
        return f"#{i}"
    return f"{e.get('point')}" + (f" {e['target']}" if e.get("target") else "")


def _keyed(enfs: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, e in enumerate(enfs or []):
        k = _enforcer_key(e, i)
        while k in out:
            k += "'"
        out[k] = e if isinstance(e, dict) else {"value": e}
    return out


def _as_list(v: Any) -> list[str]:
    return [v] if isinstance(v, str) else list(v or [])


def _mediation_changes(where: str, old: Any, new: Any, subject_old: Any = None,
                       subject_new: Any = None) -> list[Change]:
    a, b = old if isinstance(old, dict) else {}, new if isinstance(new, dict) else {}
    out = []
    sinks_a, sinks_b = _as_list(a.get("sink")), _as_list(b.get("sink"))
    # the doors the scan uses: mediation.door, else subject.target
    doors_a = _as_list(a.get("door")) or ([subject_old] if isinstance(subject_old, str) else [])
    doors_b = _as_list(b.get("door")) or ([subject_new] if isinstance(subject_new, str) else [])
    if sinks_a != sinks_b:
        gone = [x for x in sinks_a if x not in sinks_b]
        out.append(Change(where, "mediation.sink", sinks_a or None, sinks_b or None, bool(gone),
                          "a protected sink is no longer checked" if gone else ""))
    if _as_list(a.get("door")) != _as_list(b.get("door")) and (doors_a != doors_b or not sinks_a):
        came = [x for x in doors_b if x not in doors_a]
        weak = bool(sinks_a) and bool(came)           # a new door on an existing scan lets more paths through
        out.append(Change(where, "mediation.door", _as_list(a.get("door")) or None, _as_list(b.get("door")) or None,
                          weak, "more doors may reach the sink" if weak else ""))
    allow_a, allow_b = _as_list(a.get("allow")), _as_list(b.get("allow"))
    if sorted(allow_a) != sorted(allow_b):
        came = [x for x in allow_b if x not in allow_a]
        out.append(Change(where, "mediation.allow", allow_a or None, allow_b or None, bool(came),
                          f"a back door is now allowed: {', '.join(came)}" if came else ""))
    ra, rb = a.get("residual_paths"), b.get("residual_paths")
    if not same(ra, rb):
        out.append(_residual_change(where, ra, rb))
    for k in sorted((set(a) | set(b)) - {"sink", "door", "residual_paths", "allow"}):
        if not same(a.get(k), b.get(k)):
            out += _field_change(where, f"mediation.{k}", f"rule.mediation.{k}", a.get(k), b.get(k))
    return out


def _residual_change(where: str, ra: Any, rb: Any) -> Change:
    la = ra if isinstance(ra, list) else ([] if ra is None else [ra])
    lb = rb if isinstance(rb, list) else ([] if rb is None else [rb])
    gone = [x for x in la if not any(same(x, y) for y in lb)]
    return Change(where, "mediation.residual_paths", ra, rb, bool(gone),
                  "a documented residual path was removed; `inv status` withholds `held` while "
                  "one is listed, so this can raise the rule's standing" if gone else "")


def _model_policy_changes(where: str, old: Any, new: Any) -> list[Change]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return _field_change(where, "model_policy", "rule.model_policy.whole", old, new)
    out: list[Change] = []
    for k in sorted(set(old) | set(new)):
        x, y = old.get(k), new.get(k)
        if same(x, y):
            continue
        loc = f"modelPolicy.{k}"
        if isinstance(x, dict) and isinstance(y, dict):
            for p, xv, yv in _json_paths(x, y):
                sub = f"{loc}.{p.split('.')[0].split(' ')[0]}"
                out += _field_change(where, f"model_policy.{k}.{p}", sub if sub in FIELD_POLICY else loc, xv, yv)
        else:
            out += _field_change(where, f"model_policy.{k}", loc, x, y)
    return out


def _rule_changes(rid: str, a: dict[str, Any], b: dict[str, Any], typer: Typer | None = None) -> list[Change]:
    where = f"rule {rid}"
    out: list[Change] = []
    if not same(a.get("status"), b.get("status")):
        out += _field_change(where, "status", "rule.status", a.get("status"), b.get("status"))
    if not same(a.get("rev"), b.get("rev")):
        out.append(Change(where, "rev", a.get("rev"), b.get("rev")))
    ta, tb = a.get("targets") or [], b.get("targets") or []
    if not same(ta, tb):
        gone = [t for t in ta if t not in tb]
        out.append(Change(where, "targets", ta, tb, bool(gone), "a target is no longer covered" if gone else ""))
    sa, sb = a.get("semantic") or {}, b.get("semantic") or {}
    for field in sorted(set(sa) | set(sb)):
        va, vb = sa.get(field), sb.get(field)
        if same(va, vb) or field == "status":
            continue
        out += _semantic_changes(where, field, va, vb, sa, sb, same(ta, tb), typer)
    out += _doc_changes(where, a.get("doc") or {}, b.get("doc") or {})
    return out


def _semantic_changes(where: str, field: str, va: Any, vb: Any, sa: dict[str, Any], sb: dict[str, Any],
                      same_targets: bool, typer: Typer | None) -> list[Change]:
    """One changed field of a rule's semantic pin; `sa` and `sb` are the whole pins (for the subject)."""
    if field == "bad":
        return _bad_changes(where, va, vb, typer)
    if field == "enforcers":
        return _enforcer_changes(where, va, vb)
    if field == "mediation":
        return _mediation_changes(where, va, vb, (sa.get("subject") or {}).get("target"),
                                  (sb.get("subject") or {}).get("target"))
    if field == "model_policy":
        return _model_policy_changes(where, va, vb)
    if field == "falsified_by":
        gone = [x for x in (va or []) if x not in (vb or [])]
        return [Change(where, "falsified_by", va, vb, False, "a falsifier was dropped" if gone else "")]
    if field == "applies_to":
        # the targets list shows coverage changes
        return [Change(where, "applies_to", va, vb)] if same_targets else []
    return _field_change(where, field, f"rule.{field}", va, vb)


def _enforcer_changes(where: str, va: Any, vb: Any) -> list[Change]:
    """Enforcers matched by point and target: one gone is WEAKENED, one added is not, and the fields
    of one on both sides are judged one by one."""
    out: list[Change] = []
    ka, kb = _keyed(va), _keyed(vb)
    for k in sorted(set(ka) | set(kb)):
        ea, eb = ka.get(k), kb.get(k)
        if eb is None:
            out.append(Change(where, f"enforcer {k} removed", ea, None, True))
        elif ea is None:
            out.append(Change(where, f"enforcer {k} added", None, eb))
        else:
            out += _enforcer_field_changes(where, k, ea, eb)
    return out


def _enforcer_field_changes(where: str, k: str, ea: dict[str, Any], eb: dict[str, Any]) -> list[Change]:
    out: list[Change] = []
    for f in sorted(set(ea) | set(eb)):
        if same(ea.get(f), eb.get(f)):
            continue
        cs = _field_change(where, f"enforcer {k} {f}", f"enforcer.{f}", ea.get(f), eb.get(f))
        if f == "on_error" and any(c.weakened for c in cs):
            for c in cs:
                c.note = "fail_open: an error in the rule lets the call through"
        out += cs
    return out


def _doc_changes(where: str, da: dict[str, Any], db: dict[str, Any]) -> list[Change]:
    """The documentation digests: prose is information only; the mediation chokepoint is judged."""
    out: list[Change] = []
    for f in sorted(set(da) | set(db)):
        if da.get(f) == db.get(f):
            continue
        verb = "added" if f not in da else "removed" if f not in db else "edited"
        if f.startswith("mediation."):
            # not prose: `inv status` reads the chokepoint to decide `held`, so a new or edited one
            # can raise the rule's standing (a removed one can only lower it)
            weak = verb != "removed"
            out.append(Change(where, f"{f} {verb}", weakened=weak,
                              note="the chokepoint argument decides whether the rule can be held; "
                                   "the rule file diff shows it" if weak else "the rule can no longer be held"))
        else:
            out.append(Change(where, f"{f} text {verb}", info=True,
                              note="prose only, so the lock is not stale; the rule file diff shows it"))
    return out


def _operation_renames(a: Any, b: Any) -> dict[str, str]:
    """OpenAPI operations whose operationId changed while their method and path did not: old -> new.
    FastAPI derives an operationId from the function name, so renaming the function renames it."""
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return {}

    def route(op: Any) -> tuple[str, str] | None:
        return (str(op.get("method")).upper(), str(op.get("path"))) if isinstance(op, dict) else None
    added = {route(b[k]): k for k in sorted(set(b) - set(a)) if route(b[k]) is not None}
    return {k: added[r] for k in sorted(set(a) - set(b)) if (r := route(a[k])) is not None and r in added}


def _named_operations(contract: str, rules: dict[str, Any] | None) -> dict[str, list[str]]:
    """operationId -> the rules of the stored lock whose subject names it on this contract."""
    out: dict[str, list[str]] = {}
    for rid, e in sorted((rules or {}).items()):
        subj = ((e.get("semantic") or {}).get("subject") or {}) if isinstance(e, dict) else {}
        if subj.get("contract") == contract and isinstance(subj.get("operation"), str):
            out.setdefault(subj["operation"], []).append(rid)
    return out


def _entry_changes(where: str, kind: str, a: dict[str, Any], b: dict[str, Any],
                   refs: list[tuple[str, str, str, str]], rules: dict[str, Any] | None = None) -> list[Change]:
    """A target, provider or contract entry, field by field, judged by FIELD_POLICY[kind.field]. An
    OpenAPI operation renamed with the same method and path is compared with its old self."""
    out: list[Change] = []
    if kind == "contract":
        a, out = _with_renamed_operations(where, a, b)
    named = _named_operations(where.partition(" ")[2], rules) if kind == "contract" else {}
    for f in sorted(set(a) | set(b)):
        x, y = a.get(f), b.get(f)
        if same(x, y):
            continue
        loc = f"{kind}.{f}"
        if f == "param_schemas":
            out += _param_schema_changes(where, x, y)
            continue
        if f == "signature" and isinstance(x, str) and isinstance(y, str) and "types" in a and "types" in b \
                and _signature_shape(x) is not None and _signature_shape(x) == _signature_shape(y):
            out.append(Change(where, f, x, y, info=True,
                              note="the same parameters, kinds and defaults, spelled differently; the "
                                   "resolved types are pinned and compared on their own"))
            continue
        if policy(loc) == PIN:
            out += _pin_changes(where, kind, f, a, b, named, refs)
            continue
        cs = _field_change(where, f, loc, x, y)
        if loc == "target.effects":
            for c in cs:
                if c.weakened:
                    c.note = "declared pure, so the scan no longer needs a sink for it"
        out += cs
    return out


def _with_renamed_operations(where: str, a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], list[Change]]:
    """The stored contract entry with each renamed OpenAPI operation under its new id, and the renames."""
    renames = _operation_renames(a.get("operations"), b.get("operations"))
    if not renames:
        return a, []
    out: list[Change] = []
    ops = dict(a["operations"])
    for old_id, new_id in renames.items():
        ops[new_id] = ops.pop(old_id)
        out.append(Change(where, f"operation {old_id} renamed to {new_id}", old_id, new_id,
                          note=f"same method and path ({ops[new_id].get('method')} {ops[new_id].get('path')}); "
                               f"compared with the old operation"))
    return {**a, "operations": ops}, out


def _pin_changes(where: str, kind: str, f: str, a: dict[str, Any], b: dict[str, Any], named: dict[str, list[str]],
                 refs: list[tuple[str, str, str, str]]) -> list[Change]:
    """A pinned field of an entry, leaf by leaf; a contract's schemas are judged by constraint_weakening,
    and an operation removed while a rule names it is WEAKENED. Changed schema-table references are
    appended to `refs` for _schema_changes."""
    out: list[Change] = []
    found: list[tuple[str, str, str]] = []
    for p, xv, yv in _json_paths(a.get(f), b.get(f), f, found):
        why = constraint_weakening(p, xv, yv, a, b) if kind == "contract" else None
        op = p.split(".")
        if f == "operations" and len(op) == 2 and yv is None:
            users = named.get(op[1])
            why = (f"operation removed while rule {', '.join(users)} names it" if users else None)
            out.append(Change(where, f"{p} removed", xv, None, why is not None,
                              why or "operation removed; no rule names it"))
            continue
        out.append(Change(where, p, xv, yv, why is not None, why or ""))
    refs += [(f"{where} {p}", p, da, db) for p, da, db in found]
    return out


def _signature_shape(text: str) -> Any:
    """What a pinned signature text means, without how its annotations are spelled: the name, each
    parameter's name, kind (positional-only, keyword-only, *args, **kwargs), default and annotation
    (as written, quotes and spacing aside: an alias may stand for an Annotated[...] whose metadata no
    pin carries), and the return annotation. None when the text does not parse as a Python signature (it is then compared as
    text, so any change is stale)."""
    head, paren, rest = text.partition("(")
    if not paren:
        return None
    try:
        tree = ast.parse(f"def f({rest}:\n    pass\n")
    except (SyntaxError, ValueError):
        return None
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, ast.FunctionDef):
        return None

    def ann(node: ast.expr | None) -> Any:
        # The annotation as written, normalised ('str' and str, spacing): an alias name can stand for an
        # Annotated[...] whose metadata (Depends, validators) no pin carries, so any other change is stale.
        if node is None:
            return None
        for _ in range(4):                     # 'X' and "'X'" (a quoted hint under future annotations)
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                break
            text: str = node.value
            try:
                node = ast.parse(text, mode="eval").body
            except SyntaxError:
                return text
        return ast.unparse(node)

    def arg(a: ast.arg, kind: str, default: ast.expr | None = None) -> tuple[Any, ...]:
        return (a.arg, kind, ann(a.annotation), ast.dump(default) if default is not None else None)

    args = fn.args
    pos = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    shape: list[Any] = [arg(a, "posonly" if i < len(args.posonlyargs) else "pos", d)
                        for i, (a, d) in enumerate(zip(pos, defaults))]
    if args.vararg is not None:
        shape.append(arg(args.vararg, "*"))
    shape += [arg(a, "kwonly", d) for a, d in zip(args.kwonlyargs, args.kw_defaults)]
    if args.kwarg is not None:
        shape.append(arg(args.kwarg, "**"))
    return {"name": head, "params": shape, "return": ann(fn.returns)}


# How each pinned scan setting is judged: (setting, the verb that is WEAKENED, why). Every change to
# the roots is (added or removed): the scan names a file from the deepest root that holds it and
# matches the exclude globs against root-relative paths, so a new root renames the files under it (a
# relative import, a sink or door name, an allow entry may stop resolving) and lets an existing
# exclude glob match more of them.
_SCAN_WEAK = (
    ("exclude", "added", "files matching it are no longer scanned for back doors"),
    ("roots", "removed", "the scan no longer reads this source root"),
    ("skip", "added", "the scan now always skips directories with this name"),
)
_ROOT_ADDED = ("a new root renames the files under it (a file is named from the deepest root that holds it) "
               "and the exclude globs match there too, so code can drop out of the scan")


def _scan_changes(old: Any, new: Any, default: dict[str, Any] | None = None) -> list[Change]:
    """The pinned scan roots, excludes and always-skipped directories. A root added or removed, an
    added exclude or an added skipped directory: WEAKENED (see _SCAN_WEAK). A lock pinned before the
    scan was pinned is compared with `default`, what the old configuration scanned by default; without
    one, with the new roots and the default excludes."""
    if same(old, new) or new is None:
        return []
    first = old is None
    if first:
        old = default or {"roots": new["roots"], "exclude": list(DEFAULT_SCAN_EXCLUDE)}
        old = {**old, "skip": new.get("skip") or []}          # the skip list is pinned as it is
    out: list[Change] = []
    for key, weak_when, why in _SCAN_WEAK:
        a, b = set(old.get(key) or []), set(new.get(key) or [])
        for verb, items in (("added", sorted(b - a)), ("removed", sorted(a - b))):
            for x in items:
                weak = verb == weak_when or key == "roots"         # any change to the root set
                notes = [(why if verb == weak_when else _ROOT_ADDED) if weak else "",
                         "compared with the default: the scan was not pinned before" if first else ""]
                out.append(Change("scan", f"{key} {verb}", None if verb == "added" else x,
                                  x if verb == "added" else None, weak, "; ".join(n for n in notes if n)))
    if first and not out:
        out.append(Change("scan", "pinned", None, new,
                          note="the scan roots, excludes and skipped directories are pinned for the first time"))
    return out
