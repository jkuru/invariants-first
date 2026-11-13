"""CEL `bad` conditions compared as trees: canon_tree writes a subset parse tree in a canonical form
(equal trees are equivalent), compare_bad decides whether an edit flags more cases, fewer, the same, or
cannot be decided (the polarity walk, _walk), and new_error_sites lists what an edit adds that can
raise. Pure, apart from the typer, which is built from the loader's static type environments.

The canonical form and the typer are in cel_canon, the error sites in cel_sites; this module
judges the direction of an edit and re-exports their names, public and private."""
from __future__ import annotations

import typing
from typing import Any

from .cel_canon import (_BOOL_CALLS, _BOOL_OPS, _INT64, _INTEGRAL, _MACRO_CALLS, _MIRROR, _NEGATE, Typer, _Canon,
                        _clause_key, _disjuncts, _int_lit, _lit, _mask, _num, _numeric, _order, _unique,
                        canon_tree, clause_types, make_typer)
from .cel_sites import _TOTAL_CALLS, _can_raise, _describe_site, _error_sites, _magnitude, new_error_sites

# the private class of cel_canon keeps the module name it had before the split
_Canon.__module__ = __name__


def _new_sites_text(old: str, new: str, typer: Typer | None) -> list[str]:
    """new_error_sites() for an edit the polarity walk would call stronger, else []."""
    from ..cel import subset
    try:
        ta, tb = canon_tree(subset.parse(old), typer), canon_tree(subset.parse(new), typer)
    except Exception:
        return []
    effects: list[int] = []
    added: list[Any] = []
    if not _walk(ta, tb, 1, effects, typer, added) or not effects or not all(e > 0 for e in effects):
        return []
    return new_error_sites(ta, tb, added)


# polarity analysis over canonical CEL subset trees ------------------------------------------------
_CMP = {">": (1, -1), ">=": (1, -1), "<": (-1, 1), "<=": (-1, 1)}
_OP_STRENGTH = {(">", ">="): 1, (">=", ">"): -1, ("<", "<="): 1, ("<=", "<"): -1}
_FALSE = ("lit", "bool", False)


def compare_bad(old: str, new: str, parse: Any, typer: Typer | None = None) -> str:
    """'same' | 'stronger' | 'weaker' | 'unknown': does `new` flag the same cases as `old`, more, fewer,
    or cannot it be decided?

    Both are compared as canonical trees (canon_tree, with the rule's declared types when a typer is
    given), so whitespace, commuted operands, mirrored comparisons, De Morgan, `!!`, repeated
    disjuncts, renamed macro variables and regrouped || && chains are the same. Otherwise decided only when
    the trees have the same shape and differ in numeric literals of one type, list literal elements,
    strictness of a comparison, or in a disjunct or conjunct added or removed. Polarity: && || keep
    it, ! flips it, the left of > flips relative to the right, + keeps it, the right of - flips it.
    `*` and `/` keep or flip it only when the other operand is provably non-negative (a non-negative
    literal, size(), uint); otherwise the sign is unknown and so is the direction. The list on the
    right of `in` keeps it, exists/all keep it.

    An edit that would be 'stronger' but adds a sub-expression that can raise where the old tree
    could not (a new field read, divisor, arithmetic, index or call; see new_error_sites) is
    'unknown': under on_error: fail_open_and_page the error would let through a call the old rule
    rejected. A disjunct or conjunct added to an existing || or && is exempt.
    """
    return judge_bad(old, new, parse, typer)[0]


def judge_bad(old: str, new: str, parse: Any, typer: Typer | None = None) -> tuple[str, list[str]]:
    """compare_bad's direction, and the sub-expressions that can raise (new_error_sites) when they are
    what makes an otherwise stronger edit 'unknown' (else []). Both trees are parsed and walked once."""
    if old.strip() == new.strip():
        return "same", []
    try:
        ta, tb = canon_tree(parse(old), typer), canon_tree(parse(new), typer)
    except Exception:
        if new.strip() == "false":
            return "weaker", []
        if old.strip() == "false":
            return "stronger", []
        return "unknown", []
    if ta == tb:
        return "same", []
    if tb == _FALSE:
        return "weaker", []
    if ta == _FALSE:
        return "stronger", []
    effects: list[int] = []
    added: list[Any] = []
    if not _walk(ta, tb, 1, effects, typer, added) or not effects:
        return "unknown", []
    if all(e > 0 for e in effects):
        sites = new_error_sites(ta, tb, added)
        return ("unknown", sites) if sites else ("stronger", [])
    if all(e < 0 for e in effects):
        return "weaker", []
    return "unknown", []



def _nonneg(n: Any, typer: Typer | None) -> bool:
    """A canonical node that is provably >= 0 (or an error): a non-negative literal, size(), uint(),
    a uint-typed path, or a product, sum or quotient of those."""
    if not isinstance(n, tuple) or not n:
        return False
    if _num(n):
        return n[2] >= 0 and len(n) == 3
    if n[0] == "call":
        return n[1] == "size" or (n[1] == "uint" and n[2] is None)
    if n[0] == "nary" and n[1] in ("*", "+"):
        return all(_nonneg(x, typer) for x in n[2])
    if n[0] == "bin" and n[1] == "/":
        return _nonneg(n[2], typer) and _nonneg(n[3], typer)
    if n[0] in ("id", "sel") and typer is not None:
        kinds = typer(n)
        return bool(kinds) and kinds <= {"uint"}      # type: ignore[operator]
    return False


def _walk(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None = None,
          added: list[Any] | None = None) -> bool:
    """Collect the effect (+1 stronger, -1 weaker) of each difference; False when undecidable.
    pol 0: the direction at this position is unknown, so only an unchanged subtree is decidable."""
    if a == b:
        return True
    if _num(a) and _num(b):
        if pol == 0 or a[1] != b[1] or len(a) != 3 or len(b) != 3 or a[2] == b[2]:
            return False                               # 1 vs 1.0 (or 0.0 vs -0.0): a type change
        out.append(pol if b[2] > a[2] else -pol)
        return True
    if not (isinstance(a, tuple) and isinstance(b, tuple)) or not a or not b:
        return False
    if a[0] == "nary" and b[0] != "nary" and a[1] in ("||", "&&"):     # `x && y` -> `x`: one operand left
        b = ("nary", a[1], (b,))
    elif b[0] == "nary" and a[0] != "nary" and b[1] in ("||", "&&"):
        a = ("nary", b[1], (a,))
    if a[0] != b[0]:
        return False
    walk = _WALKS.get(a[0])
    return walk is not None and walk(a, b, pol, out, typer, added)


def _walk_nary(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    """Two flattened || && + * chains: common operands cancel, the rest pair up, or (for || and &&)
    operands were only added or only removed."""
    if a[1] != b[1] or pol == 0:
        return False
    xa, xb = list(a[2]), list(b[2])
    common = []
    for x in list(xa):                             # operands present on both sides
        if x in xb:
            xa.remove(x)
            xb.remove(x)
            common.append(x)
    sub = pol
    if a[1] == "*":                                # monotone in a factor only if the rest is >= 0
        others = common if len(xa) == 1 else [*common, *xa, *xb]
        if not all(_nonneg(x, typer) for x in others):
            sub = 0
    if len(xa) == len(xb):
        return all(_walk(x, y, sub, out, typer, added) for x, y in zip(xa, xb))
    if a[1] in ("||", "&&") and (not xa or not xb):
        more = 1 if xb else -1                     # operands added (1) or removed (-1)
        if added is not None:                      # where the old tree fired, the old operands
            added += xb                            # decide || / && and absorb an error in these
        out.append(more * pol if a[1] == "||" else -more * pol)
        return True
    return False


def _walk_bin(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    op, op2 = a[1], b[1]
    if op != op2:
        step = _OP_STRENGTH.get((op, op2))
        if step is None or pol == 0:
            return False
        out.append(step * pol)                     # > to >= flags the boundary too
        lp, rp = _CMP[op]
        return (_walk(a[2], b[2], pol * lp, out, typer, added)
                and _walk(a[3], b[3], pol * rp, out, typer, added))
    if op in ("&&", "||", "+"):
        return _walk(a[2], b[2], pol, out, typer, added) and _walk(a[3], b[3], pol, out, typer, added)
    if op == "-":
        return _walk(a[2], b[2], pol, out, typer, added) and _walk(a[3], b[3], -pol, out, typer, added)
    if op == "/":                                  # x / k: up in x if k >= 0, down in k if x >= 0
        lp = pol if _nonneg(a[3], typer) and _nonneg(b[3], typer) else 0
        rp = -pol if _nonneg(a[2], typer) and _nonneg(b[2], typer) and _nonneg(a[3], typer) \
            and _nonneg(b[3], typer) else 0
        return _walk(a[2], b[2], lp, out, typer, added) and _walk(a[3], b[3], rp, out, typer, added)
    if op in _CMP:
        lp, rp = _CMP[op]
        return (_walk(a[2], b[2], pol * lp, out, typer, added)
                and _walk(a[3], b[3], pol * rp, out, typer, added))
    if op == "in":
        return _walk_in(a, b, pol, out, typer, added)
    return False                                  # == != % : not monotone


def _walk_in(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    """`x in list`: the same left side; list literal elements added keep the polarity, removed flip it."""
    if a[2] != b[2]:
        return False
    la, lb = a[3], b[3]
    if not la[0] == lb[0] == "list":
        return _walk(la, lb, pol, out, typer, added)
    xa, xb = list(la[1]), list(lb[1])
    grown, shrunk = [x for x in xb if x not in xa], [x for x in xa if x not in xb]
    if pol == 0 or (grown and shrunk):
        return False
    if grown:
        out.append(pol)
    if shrunk:
        out.append(-pol)
    return True


def _walk_un(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    if a[1] != b[1]:
        return False
    return _walk(a[2], b[2], -pol, out, typer, added)    # ! and unary - both flip


def _walk_call(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    if a[1] != b[1] or a[2] != b[2] or len(a[3]) != len(b[3]):
        return False
    if a[1] in ("exists", "all") and a[2] is not None and a[3][0] == b[3][0]:
        return _walk(a[3][1], b[3][1], pol, out, typer, added)
    return False


def _walk_cond(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None, added: list[Any] | None) -> bool:
    return (a[1] == b[1] and _walk(a[2], b[2], pol, out, typer, added)
            and _walk(a[3], b[3], pol, out, typer, added))


# _walk's step for each kind of node (both sides have the same kind); any other kind is undecidable
_WALKS: dict[str, typing.Callable[[Any, Any, int, list[int], Typer | None, list[Any] | None], bool]] = {
    "nary": _walk_nary, "bin": _walk_bin, "un": _walk_un, "call": _walk_call, "cond": _walk_cond,
}
