"""Static reads of a parsed tree: its children, the names and field paths it references, what it reads.

The engines and the lock read these to know which values a clause converts and compares.
"""
from __future__ import annotations

from typing import Iterator

from .catalog import _MACROS
from .parser import Node


def kids(n: Node) -> Iterator[Node]:
    """Child nodes of a tree node (map items are flattened)."""
    for c in n[1:]:
        for x in (c if isinstance(c, list) else [c] if isinstance(c, tuple) else []):
            yield from (x if isinstance(x[0], tuple) else [x])


def _macro_parts(n: Node) -> tuple[Node, str, Node]:
    tgt, (var, body) = (n[2], n[3]) if n[2] is not None else (n[3][0], n[3][1:])
    return tgt, var[1], body


def free_names(n: Node, bound: frozenset = frozenset()) -> set[str]:
    """Identifiers an expression reads."""
    return {r[0] for r in references(n, bound)}


def references(n: Node, bound: frozenset = frozenset()) -> set[tuple[str, ...]]:
    """Every free identifier with its static field path, e.g. ('args', 'total_jobs')."""
    if n[0] == "id":
        return set() if n[1] in bound else {(n[1],)}
    if n[0] == "sel":
        chain, x = [], n
        while x[0] == "sel":
            chain.append(x[2])
            x = x[1]
        if x[0] == "id":
            return set() if x[1] in bound else {(x[1], *reversed(chain))}
        return references(x, bound)
    if n[0] == "call" and (n[1] in _MACROS or n[1] == "inv.unique_by"):
        tgt, var, body = _macro_parts(n)
        return references(tgt, bound) | references(body, bound | {var})
    return set().union(*(references(c, bound) for c in kids(n)))


ELEM, SIZE = "[]", "#"          # reads() path steps: every element of a list; only a list's or map's length
_ELEMENT_MACROS = frozenset({"all", "exists", "exists_one", "map", "inv.unique_by"})


def _static_path(n: Node) -> tuple[str, ...] | None:
    """('a', 'b', 'c') for a field chain a.b.c, else None."""
    chain = []
    while n[0] == "sel":
        chain.append(n[2])
        n = n[1]
    return (n[1], *reversed(chain)) if n[0] == "id" else None


def reads(n: Node, bound: frozenset = frozenset()) -> set[tuple[str, ...]]:
    """references(), refined to what the clause reads inside lists: what an engine must convert.

    A path ending in SIZE reads only the length (size(args.items)); a path through ELEM reads those
    fields of every element (args.items.exists(i, i.qty < 0) reads ('args', 'items', ELEM, 'qty')).
    Only all/exists/exists_one/map/inv.unique_by over a field path refine: their results never hold
    an element itself (map's values are what its body reads), while filter() returns elements, so
    it reads its list whole. Anything else reads as references() does: the whole value at the path.
    """
    if n[0] in ("id", "sel"):
        return references(n, bound)
    if n[0] == "call" and n[1] == "size" and len(n[3]) + (n[2] is not None) == 1:
        arg = n[2] if n[2] is not None else n[3][0]
        p = _static_path(arg)
        if p is not None and p[0] not in bound:
            return {(*p, SIZE)}
        return reads(arg, bound)
    if n[0] == "call" and (n[1] in _MACROS or n[1] == "inv.unique_by"):
        tgt, var, body = _macro_parts(n)
        p = _static_path(tgt)
        if n[1] not in _ELEMENT_MACROS or p is None or p[0] in bound:
            return reads(tgt, bound) | reads(body, bound | {var})
        inner = reads(body, bound - {var})
        own = {r for r in inner if r[0] == var}
        out = (inner - own) | {(*p, ELEM, *r[1:]) for r in own}
        return out | ({(*p, SIZE)} if not own else set())
    return set().union(*(reads(c, bound) for c in kids(n)))
