"""What an edit of a `bad` condition adds that can raise: new_error_sites lists the sub-expressions
of a canonical tree (field reads, indexes, divisions, arithmetic that may leave int64, calls) with
no equal in the old one, described for a reviewer. Pure."""
from __future__ import annotations

import typing
from typing import Any

from .cel_canon import _MACRO_CALLS, _num


_TOTAL_CALLS = frozenset({"has"})


def _error_sites(n: Any, ctx: tuple[Any, ...], out: list[tuple[Any, ...]], skip: set[int]) -> None:
    """Every sub-expression of a canonical tree that can raise by itself: a field or variable read
    (a missing key, a read on null), an index, a division or modulus (by zero, int64 min / -1),
    arithmetic (int overflow) and a function call (a bad argument). Each is keyed by the macro
    targets it sits under, so `x.f` in one comprehension is not `x.f` in another. Nodes whose id()
    is in `skip` are left out with all they contain."""
    if not isinstance(n, tuple) or not n or id(n) in skip:
        if isinstance(n, list):
            for x in n:
                _error_sites(x, ctx, out, skip)
        return
    k = n[0]
    if not isinstance(k, str):                         # a map item: (key, value)
        for x in n:
            _error_sites(x, ctx, out, skip)
        return
    if k == "lit":
        return
    if k == "id":
        if not str(n[1]).startswith("$"):              # macro variables are always bound
            out.append((ctx, n))
        return
    if k == "call" and n[1] in _MACRO_CALLS:
        receiver = n[2] is not None
        args = n[3]
        tgt, var, body = (n[2], args[0], args[1:]) if receiver else (args[0], args[1], args[2:])
        if isinstance(var, tuple) and var[:1] == ("id",):
            _error_sites(tgt, ctx, out, skip)
            for b in body:
                _error_sites(b, (*ctx, tgt), out, skip)
            return
    if k == "call" and n[1] == "has" and n[2] is None and n[3] and n[3][0][:1] == ("sel",):
        _error_sites(n[3][0][1], ctx, out, skip)       # has(a.b) reads a, and tests b
        return
    if _can_raise(n):
        out.append((ctx, n))
    for x in n[1:]:
        if isinstance(x, (tuple, list)):
            _error_sites(x, ctx, out, skip)


def _can_raise(n: Any) -> bool:
    """A canonical node (not a literal, variable or macro) that can raise by itself, apart from its
    operands: a field read, an index, a call other than has(), unary minus, arithmetic that may leave
    int64, a division or modulus by anything but a non-zero literal other than -1."""
    k = n[0]
    own = k in ("sel", "idx") or (k == "call" and n[1] not in _TOTAL_CALLS) \
        or (k == "un" and n[1] == "-") \
        or (k in ("bin", "nary") and n[1] in ("+", "-", "*"))
    if own and k in ("bin", "nary") and n[1] in ("+", "-", "*"):
        b = _magnitude(n)                              # literals and size(): provably in int64
        own = b is None or b >= 2 ** 63
    if k == "bin" and n[1] in ("/", "%"):
        d = n[3]                                       # a non-zero literal divisor other than -1
        own = not (_num(d) and d[2] not in (0, -1))    # cannot fail; anything else can
    return bool(own)


def _magnitude(n: Any) -> int | None:
    """An upper bound on |n| for arithmetic over int literals and size() (at most 2**32: no list,
    map or string in a call's arguments is longer), or None when there is none."""
    if _num(n) and n[1] == "int" and len(n) == 3:
        return abs(n[2])
    if isinstance(n, tuple) and n[:2] == ("call", "size") and n[2] is None:
        return 2 ** 32
    if isinstance(n, tuple) and n[0] in ("bin", "nary") and n[1] in ("+", "-", "*"):
        parts = [_magnitude(x) for x in (n[2] if n[0] == "nary" else n[2:4])]
        if any(x is None for x in parts):
            return None
        total = 1 if n[1] == "*" else 0
        for x in parts:
            total = total * x if n[1] == "*" else total + x   # type: ignore[operator]
        return total
    return None


def _describe_site(n: Any) -> str:
    path = []
    x = n
    while isinstance(x, tuple) and x[:1] == ("sel",):
        path.append(x[2])
        x = x[1]
    if isinstance(x, tuple) and x[:1] == ("id",) and path:
        return "a read of " + ".".join([str(x[1]), *reversed(path)])
    k = n[0]
    if k == "id":
        return f"a read of {n[1]}"
    if k == "sel":
        return f"a read of .{n[2]}"
    if k == "idx":
        return "an index"
    if k == "call":
        return f"a call of {n[1]}()"
    if k == "bin" and n[1] in ("/", "%"):
        return "a division" if n[1] == "/" else "a modulus"
    return f"arithmetic ({n[1]})"


def new_error_sites(old: Any, new: Any, exempt: typing.Sequence[Any] = ()) -> list[str]:
    """The sub-expressions of canonical tree `new` that can raise an error and have no equal in
    `old`, described for a reviewer. Operands in `exempt` (a disjunct or conjunct added to an
    existing || or &&) are left out: wherever the old condition held, the operands it already had
    decide the || or && and absorb an error in the new one, as CEL's commutative || and && do.

    Under on_error: fail_open_and_page a rule that errors lets the call through, so an edit that
    flags more cases but can now raise where it used to fire is not a strengthening."""
    so: list[tuple[Any, ...]] = []
    sn: list[tuple[Any, ...]] = []
    _error_sites(old, (), so, set())
    _error_sites(new, (), sn, {id(x) for x in exempt})
    out: list[str] = []
    for site in sn:
        if site not in so:
            d = _describe_site(site[1])
            if d not in out:
                out.append(d)
    return [d for d in out if not any(e.startswith(d + ".") for e in out)]   # a.b.c covers a.b
