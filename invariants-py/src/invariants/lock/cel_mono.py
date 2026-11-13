"""CEL `bad` conditions compared as trees: canon_tree writes a subset parse tree in a canonical form
(equal trees are equivalent), compare_bad decides whether an edit flags more cases, fewer, the same, or
cannot be decided (the polarity walk, _walk), and new_error_sites lists what an edit adds that can
raise. Pure, apart from the typer, which is built from the loader's static type environments."""
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


def _num(n: Any) -> bool:
    """A tagged int or double literal (canonical form)."""
    return isinstance(n, tuple) and len(n) >= 3 and n[0] == "lit" and n[1] in ("int", "double")


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
