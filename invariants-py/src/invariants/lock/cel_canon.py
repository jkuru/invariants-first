"""CEL `bad` conditions in a canonical form: canon_tree writes a subset parse tree so that equal
trees are equivalent (literals tagged with their type, chains flattened and sorted, ! pushed
inward, macro variables named by depth), and a typer (make_typer, clause_types) gives the static
kinds the rule's declared types allow. Pure, apart from the typer, which is built from the loader's
static type environments."""
from __future__ import annotations

import math
import typing
from typing import Any


def _lit(v: Any) -> tuple[Any, ...]:
    """A literal tagged with its CEL type, so true, 1 and 1.0 (equal to Python) never compare equal,
    and -0.0 stays apart from 0.0."""
    if isinstance(v, bool):
        return ("lit", "bool", v)
    if isinstance(v, int):
        return ("lit", "int", v)
    if isinstance(v, float):
        return ("lit", "double", v, "-0") if v == 0 and math.copysign(1.0, v) < 0 else ("lit", "double", v)
    if v is None:
        return ("lit", "null", None)
    return ("lit", type(v).__name__, v)


def _numeric(n: Any) -> bool:
    """A node that can only be a number (so `+` on it is addition, not string or list concatenation)."""
    if not isinstance(n, tuple) or not n:
        return False
    if n[0] == "lit":
        return n[1] in ("int", "double")
    if n[0] == "bin" and n[1] in ("*", "/", "%", "-"):
        return True
    if n[0] == "nary" and n[1] == "*":
        return True
    if n[0] == "un" and n[1] == "-":
        return True
    return n[0] == "call" and n[1] in ("size", "int", "double", "uint", "inv.sum") and n[2] is None


def _mask(n: Any) -> str:
    """A sort key that ignores numeric literal values, so an edited threshold keeps its place."""
    if isinstance(n, tuple):
        if _num(n):
            return "#" + n[1]
        return "(" + ",".join(_mask(x) for x in n) + ")"
    if isinstance(n, list):
        return "[" + ",".join(_mask(x) for x in n) + "]"
    return repr(n)


def _order(xs: list[Any]) -> tuple[Any, ...]:
    return tuple(sorted(xs, key=lambda x: (_mask(x), repr(x))))


def _unique(xs: list[Any]) -> list[Any]:
    out: list[Any] = []
    for x in xs:
        if x not in out:                               # tagged literals: Python == is type-strict here
            out.append(x)
    return out


Typer = typing.Callable[[Any], "frozenset[str] | None"]
_INT64 = (-(2 ** 63), 2 ** 63 - 1)
_INTEGRAL = frozenset({"int", "uint"})
_MIRROR = {"<": ">", "<=": ">="}
_NEGATE = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}
_BOOL_OPS = frozenset({"&&", "||", "==", "!=", "<", "<=", ">", ">=", "in"})
_BOOL_CALLS = frozenset({"has", "exists", "all", "exists_one", "startsWith", "endsWith", "contains", "matches"})
_MACRO_CALLS = frozenset({"all", "exists", "exists_one", "map", "filter", "inv.unique_by"})


def make_typer(envs: Any) -> Typer | None:
    """The static CEL kinds of a subset expression in every one of a rule's type environments (one
    per enforcer, from the loader), or None when any environment cannot say (dyn, an error)."""
    if not envs:
        return None
    from .. import typecheck as tc

    def typer(raw: Any) -> frozenset[str] | None:
        kinds: set[str] = set()
        for env in envs:
            try:
                t = tc.infer(raw, env)
            except Exception:
                return None
            if t.kinds is None or t.refused is not None:
                return None
            kinds |= t.kinds
        return frozenset(kinds)
    return typer


def clause_types(reg: Any) -> dict[str, Typer | None]:
    """Rule id -> a typer for its clauses, from a loaded registry's static type environments."""
    from ..loader import _type_env
    out: dict[str, Typer | None] = {}
    for rid, rule in reg.rules.items():
        try:
            envs = [_type_env(rule, reg, i, names) for i, names in rule.names_for.items()]
        except Exception:
            envs = []
        out[rid] = make_typer(envs)
    return out


class _Canon:
    """canon_tree's walk. Rewrites that hold for every value CEL can give (De Morgan, `a < b` as
    `b > a`, `!(a == b)` as `a != b`, duplicate disjuncts, macro variable names) always apply; those
    that hold only for some types (`!(a < b)` as `a >= b`, which NaN breaks, and `a > 5` as
    `a >= 6`, which needs integers) apply only when both operands are provably of those types."""

    def __init__(self, typer: Typer | None = None) -> None:
        self.typer = typer

    def kinds(self, raw: Any, bound: frozenset[str]) -> frozenset[str] | None:
        if self.typer is None:
            return None
        from ..cel import subset
        try:
            if any(r[0] in bound for r in subset.references(raw)):
                return None                            # reads a macro variable: no static type here
        except Exception:
            return None
        return self.typer(raw)

    def integral(self, raw: Any, bound: frozenset[str]) -> bool:
        """Provably an int or uint (or an error): an int literal, size(), int(), uint(), or arithmetic
        on those. A path the rule's declared types make int does not count (see below)."""
        if not isinstance(raw, tuple) or not raw:
            return False
        k = raw[0]
        if k == "lit":
            return type(raw[1]) is int
        if k == "un" and raw[1] == "-":
            return self.integral(raw[2], bound)
        if k == "call" and (raw[1] == "size" or (raw[1] in ("int", "uint") and raw[2] is None)):
            return True
        if k == "bin" and raw[1] in ("+", "-", "*", "/", "%"):
            return self.integral(raw[2], bound) and self.integral(raw[3], bound)
        # Declared annotations are deliberately NOT trusted here: Python does not enforce them at
        # run time, so a caller can pass 10000.5 to an `int` parameter and `> 10000` / `>= 10001`
        # would then differ. A weakening gate must not rest on an unchecked assumption.
        return False

    def nan_free(self, raw: Any, bound: frozenset[str]) -> bool:
        if isinstance(raw, tuple) and raw[:1] == ("un",) and raw[1] == "-":
            return self.nan_free(raw[2], bound)
        if isinstance(raw, tuple) and raw[:1] == ("lit",) and type(raw[1]) in (int, float):
            return True
        return self.integral(raw, bound)

    def boolean(self, c: Any) -> bool:
        """A canonical node that is provably bool (or an error)."""
        if not isinstance(c, tuple) or not c:
            return False
        if c[0] == "lit":
            return c[1] == "bool"
        if c[0] in ("bin", "nary"):
            return c[1] in _BOOL_OPS
        if c[0] == "un":
            return c[1] == "!"
        if c[0] == "call" and c[1] in _BOOL_CALLS:
            return True
        if c[0] in ("id", "sel"):
            kinds = self.kinds(c, frozenset())
            return kinds == frozenset({"bool"})
        return False

    def canon(self, n: Any, ren: dict[str, str]) -> Any:
        if isinstance(n, list):
            return [self.canon(x, ren) for x in n]
        if not isinstance(n, tuple) or not n:
            return n
        k = n[0]
        if k == "lit":
            return _lit(n[1])
        if k == "id":
            return ("id", ren.get(n[1], n[1]))
        if k == "un" and n[1] == "!":
            return self.negate(n[2], ren)
        if k == "un" and n[1] == "-":
            x = n[2]
            if x[0] == "lit" and type(x[1]) in (int, float):
                return _lit(-x[1])
            return ("un", "-", self.canon(x, ren))
        if k == "bin":
            return self.binary(n, ren)
        if k == "call" and n[1] in _MACRO_CALLS:
            return self.macro(n, ren)
        if k == "cond":
            return ("cond", *(self.canon(x, ren) for x in n[1:]))
        return tuple(self.canon(x, ren) if isinstance(x, (tuple, list)) else x for x in n)

    def binary(self, n: Any, ren: dict[str, str]) -> Any:
        op = n[1]
        bound = frozenset(ren)
        if op in _MIRROR:                              # a < b is b > a, for every value
            return self.binary(("bin", _MIRROR[op], n[3], n[2]), ren)
        if op in ("+", "*"):                           # commuted, never regrouped: (a + b) + c and
            pair = [self.canon(n[2], ren), self.canon(n[3], ren)]   # a + (b + c) differ in rounding
            if op == "+" and not any(_numeric(x) for x in pair):    # and in int overflow
                return ("bin", op, *pair)              # may be string or list concatenation: keep order
            return ("nary", op, _order(pair))
        if op in ("||", "&&"):
            return self.chain(n, op, ren)
        left, right = self.canon(n[2], ren), self.canon(n[3], ren)
        if op == ">":                                  # integers: a > 5 is a >= 6
            if _int_lit(right) and self.integral(n[2], bound) and right[2] < _INT64[1]:
                return ("bin", ">=", left, _lit(right[2] + 1))
            if _int_lit(left) and self.integral(n[3], bound) and left[2] > _INT64[0]:
                return ("bin", ">=", _lit(left[2] - 1), right)
        if op in ("==", "!="):
            left, right = _order([left, right])
        if op == "in" and isinstance(right, tuple) and right[:1] == ("list",):
            right = ("list", list(_order(_unique(right[1]))))   # membership ignores order and repeats
        return ("bin", op, left, right)

    def chain(self, n: Any, op: str, ren: dict[str, str]) -> Any:
        """A chain of || (or of &&) flattened into one ("nary", op, operands), repeats dropped."""
        flat: list[Any] = []
        stack = [n]
        while stack:
            x = stack.pop()
            if isinstance(x, tuple) and x[:2] == ("bin", op):
                stack += [x[3], x[2]]
                continue
            c = self.canon(x, ren)
            flat += list(c[2]) if isinstance(c, tuple) and c[:2] == ("nary", op) else [c]
        flat = _unique(flat)                           # a || a is a
        # a && true and a || false are a for a bool (or an error, which true && and false || keep). The
        # unit is dropped from any chain: two or more operands are bool or an error already, and one left
        # that may not be a bool stays a one-operand chain below, apart from the bare value
        unit = _lit(op == "&&")
        flat = [c for c in flat if c != unit] or flat
        if len(flat) == 1 and self.boolean(flat[0]):
            return flat[0]
        return ("nary", op, _order(flat))

    def negate(self, x: Any, ren: dict[str, str]) -> Any:
        """The canonical form of !x."""
        bound = frozenset(ren)
        if isinstance(x, tuple) and x:
            if x[0] == "un" and x[1] == "!":
                inner = self.canon(x[2], ren)
                if self.boolean(inner):
                    return inner                       # !!e is e for a bool e
                return ("un", "!", self.negate(x[2], ren))
            if x[0] == "bin" and x[1] in ("&&", "||"):     # De Morgan
                dual = "||" if x[1] == "&&" else "&&"
                return self.binary(("bin", dual, ("un", "!", x[2]), ("un", "!", x[3])), ren)
            if x[0] == "bin" and x[1] in ("==", "!="):
                return self.binary(("bin", "!=" if x[1] == "==" else "==", x[2], x[3]), ren)
            if x[0] == "bin" and x[1] in _NEGATE and self.nan_free(x[2], bound) and self.nan_free(x[3], bound):
                return self.binary(("bin", _NEGATE[x[1]], x[2], x[3]), ren)
            if x[0] == "lit" and isinstance(x[1], bool):
                return _lit(not x[1])
        return ("un", "!", self.canon(x, ren))

    def macro(self, n: Any, ren: dict[str, str]) -> Any:
        """A comprehension with its variable renamed by nesting depth, so exists(x, ...) and
        exists(y, ...) with the same body are the same tree."""
        receiver = n[2] is not None
        args = n[3]
        tgt, var, body = (n[2], args[0], args[1:]) if receiver else (args[0], args[1], args[2:])
        if not (isinstance(var, tuple) and var[:1] == ("id",)):
            return tuple(self.canon(x, ren) if isinstance(x, (tuple, list)) else x for x in n)
        name = f"${1 + max((int(v[1:]) for v in ren.values()), default=-1)}"
        inner = {**ren, var[1]: name}
        ct = self.canon(tgt, ren)
        cb = [self.canon(b, inner) for b in body]
        if receiver:
            return ("call", n[1], ct, [("id", name), *cb])
        return ("call", n[1], None, [ct, ("id", name), *cb])


def _int_lit(c: Any) -> bool:
    return isinstance(c, tuple) and len(c) == 3 and c[:2] == ("lit", "int")


def canon_tree(n: Any, typer: Typer | None = None) -> Any:
    """The canonical form of a CEL subset parse tree. Equal canonical trees are equivalent.

    Literals are tagged with their type. Chains of || && are flattened into sorted ("nary", op,
    operands), with repeated operands dropped; the two operands of a numeric + or * are sorted (a
    commuted pair) but never regrouped, since floating-point rounding and int overflow depend on the
    grouping; == != and `in` lists are sorted;
    `a < b` is written `b > a`; ! is pushed inward (De Morgan, !! dropped on a bool, !(a == b) is
    a != b); macro variables are named by depth. With a typer (the rule's declared types), a
    comparison of provable integers or NaN-free numbers is also normalised: `a > 5` is `a >= 6`,
    and `!(a < b)` is `a >= b`."""
    return _Canon(typer).canon(n, {})


def _clause_key(text: str, parse: Any, typer: Typer | None = None) -> Any:
    try:
        return canon_tree(parse(text), typer)
    except Exception:
        return ("text", " ".join(text.split()))


def _disjuncts(tree: Any) -> list[Any]:
    if isinstance(tree, tuple) and tree[:2] == ("nary", "||"):
        return list(tree[2])
    return [tree]


def _num(n: Any) -> bool:
    """A tagged int or double literal (canonical form)."""
    return isinstance(n, tuple) and len(n) >= 3 and n[0] == "lit" and n[1] in ("int", "double")
