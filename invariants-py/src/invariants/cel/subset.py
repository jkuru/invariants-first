"""A small, safe CEL subset evaluator (the stdlib fallback backend).

Not full CEL. No eval/exec: a tokenizer, a recursive-descent parser to tuples, and a
tree-walking evaluator with a cost budget. Supported: int/double/string/bool/null literals,
lists, maps, field access, indexing, ! - * / % + -, comparisons, in, && || (CEL's
commutative error absorption, also in all/exists), ?:, has(e.f), size(), contains/startsWith/endsWith,
matches, the conversions int(), double(), string(), bool(), dyn(), timestamp() and duration(),
timestamp and duration values (comparison, arithmetic, the getters), the macros
all/exists/exists_one/map/filter, and the fixed extensions inv.sum, inv.zip, inv.unique_by.
FUNCTIONS lists every function name it has; the loader refuses a clause that calls another.
Reading a NaN or +-Infinity is an evaluation error unless non-finite numbers are allowed.
"""
from __future__ import annotations

import datetime
import enum
import math
import re
from typing import Any, Iterator

from . import _regex, values
from .errors import ALLOW_NON_FINITE, CelError, CelSyntaxError, check_finite, check_result, raised

Node = tuple
DEFAULT_BUDGET = 10_000


class CostExceeded(CelError):
    """The cost budget is spent. Never absorbed: && || all exists exists_one re-raise it at once, so a
    clause that runs out of budget is a rule_error whatever its other branches say."""
INT_MIN, INT_MAX = -2**63, 2**63 - 1
INV_ARITY = {"inv.sum": 1, "inv.zip": 2, "inv.unique_by": 3}

_TOK = re.compile(r'\s*(?:(\d+\.\d+)|(\d+)|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|'
                  r'([A-Za-z_][A-Za-z0-9_]*)|(==|!=|<=|>=|&&|\|\||[-+*/%<>!()\[\]{},.:?]))',
                  re.ASCII)       # digits and spaces are ASCII only, as in CEL's grammar
_MACROS = {"all", "exists", "exists_one", "map", "filter"}
CONVERSIONS = frozenset({"int", "double", "string", "bool", "dyn", "timestamp", "duration"})
_GETTERS = ("getFullYear", "getMonth", "getDayOfYear", "getDayOfMonth", "getDate", "getDayOfWeek",
            "getHours", "getMinutes", "getSeconds", "getMilliseconds")
# every function name the subset evaluates (global or receiver form); the loader refuses others
FUNCTIONS = frozenset({"size", "contains", "startsWith", "endsWith", "matches", "has", *_MACROS, *CONVERSIONS,
                       *_GETTERS, *INV_ARITY})
# name -> (argument counts in global form f(...), argument counts in receiver form x.f(...)), per
# the CEL spec. An empty set: CEL has no such form. The loader refuses any other call shape of a
# known function at load, on every backend (it would be a rule_error on every evaluation).
ARITY: dict[str, tuple[frozenset[int], frozenset[int]]] = {
    "size": (frozenset({1}), frozenset({0})),
    **{n: (frozenset({2}), frozenset({1})) for n in ("contains", "startsWith", "endsWith", "matches")},
    "has": (frozenset({1}), frozenset()),
    **{n: (frozenset(), frozenset({2})) for n in _MACROS},
    **{n: (frozenset({1}), frozenset()) for n in ("int", "uint", "double", "string", "bytes", "bool", "dyn",
                                                   "type", "timestamp", "duration")},
    **{n: (frozenset(), frozenset({0, 1})) for n in _GETTERS},
    **{n: (frozenset({k}), frozenset()) for n, k in INV_ARITY.items()},
}


def arity_error(name: str, method: bool, n: int) -> str | None:
    """Why a call of a known function with n arguments (on a receiver if method) cannot evaluate, else None."""
    if name not in ARITY:
        return None
    glob, recv = ARITY[name]
    if (n in recv) if method else (n in glob):
        return None
    form = f"x.{name}()" if method else f"{name}()"
    if name in _MACROS and not method:
        return f"{name}() is a macro: write list.{name}(var, predicate), not {name}(list, var, predicate)"
    if method and not recv:
        return f"{form}: {name} is not a method; write {name}(x)"
    if not method and not glob:
        return f"{form}: {name} is a method; write x.{name}(...)"
    counts = sorted(recv if method else glob)
    words = {0: "no arguments", 1: "one argument", 2: "two arguments", 3: "three arguments"}
    return f"{form} takes {' or '.join(words.get(c, f'{c} arguments') for c in counts)}, got {n}"


_ESC = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{3}|.)", re.S)
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a"}


def decode_string(raw: str) -> str:
    """Decode the escapes in a CEL string literal body (non-ASCII text is kept as is)."""
    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        if e[0] in "xuU":
            return chr(int(e[1:], 16))
        if len(e) == 3 and e.isdigit():
            return chr(int(e, 8))
        return _SIMPLE.get(e, e)
    return _ESC.sub(one, raw)


def tokenize(src: str) -> list[tuple[str, Any]]:
    """Split rule text into tokens."""
    out: list[tuple[str, Any]] = []
    pos, src = 0, src.strip()
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m or m.end() == pos:
            raise CelSyntaxError(f"bad character at {pos}: {src[pos:pos + 10]!r}")
        f, i, s1, s2, ident, op = m.groups()
        if f:
            out.append(("lit", float(f)))
        elif i:
            out.append(("lit", int(i)))
        elif s1 is not None or s2 is not None:
            out.append(("lit", decode_string(s1 if s1 is not None else s2)))
        elif ident:
            if ident in ("true", "false", "null"):
                out.append(("lit", {"true": True, "false": False, "null": None}[ident]))
            else:
                out.append(("id", ident))
        else:
            out.append(("op", op))
        pos = m.end()
    return out + [("end", None)]


class _Parser:
    _LEVELS = [("||",), ("&&",), ("==", "!=", "<", "<=", ">", ">=", "in"), ("+", "-"), ("*", "/", "%")]

    def __init__(self, src: str) -> None:
        self.t, self.i = tokenize(src), 0

    def peek(self, v: str | None = None) -> Any:
        k, x = self.t[self.i]
        return (k == "op" and x == v) if v else (k, x)

    def eat(self, v: str) -> None:
        if not self.peek(v):
            raise CelSyntaxError(f"expected {v!r}, got {self.t[self.i][1]!r}")
        self.i += 1

    def parse(self) -> Node:
        e = self.expr()
        if self.t[self.i][0] != "end":
            raise CelSyntaxError(f"trailing input at {self.t[self.i][1]!r}")
        return e

    def expr(self) -> Node:
        c = self.binary(0)
        if self.peek("?"):
            self.eat("?")
            a = self.expr()
            self.eat(":")
            return ("cond", c, a, self.expr())
        return c

    def binary(self, lvl: int) -> Node:
        if lvl == len(self._LEVELS):
            return self.unary()
        left = self.binary(lvl + 1)
        while True:
            k, x = self.peek()
            op = x if k == "op" else ("in" if (k, x) == ("id", "in") else None)
            if op not in self._LEVELS[lvl]:
                return left
            self.i += 1
            left = ("bin", op, left, self.binary(lvl + 1))

    def unary(self) -> Node:
        if self.peek("!"):
            self.i += 1
            return ("un", "!", self.unary())
        if self.peek("-"):
            self.i += 1
            return ("un", "-", self.unary())
        return self.member()

    def args(self, close: str) -> list[Node]:
        out = []
        while not self.peek(close):
            out.append(self.expr())
            if not self.peek(close):
                self.eat(",")
        self.eat(close)
        return out

    def member(self) -> Node:
        e = self.primary()
        while True:
            if self.peek("."):
                self.i += 1
                k, name = self.peek()
                if k != "id":
                    raise CelSyntaxError("expected field name after '.'")
                self.i += 1
                if self.peek("("):
                    self.i += 1
                    a = self.args(")")
                    e = ("call", "inv." + name, None, a) if e == ("id", "inv") else ("call", name, e, a)
                    if name in _MACROS and (len(a) != 2 or a[0][0] != "id"):
                        raise CelSyntaxError(f"{name}() needs (var, expr)")
                    _check_inv(e)
                else:
                    e = ("sel", e, name)
            elif self.peek("["):
                self.i += 1
                ix = self.expr()
                self.eat("]")
                e = ("idx", e, ix)
            else:
                return e

    def primary(self) -> Node:
        k, x = self.peek()
        self.i += 1
        if k == "lit":
            return ("lit", x)
        if k == "id":
            if self.peek("("):
                self.i += 1
                a = self.args(")")
                if x == "has" and (len(a) != 1 or a[0][0] != "sel"):
                    raise CelSyntaxError("has() takes a field selection, e.g. has(a.b)")
                if x in _MACROS:                 # the macros have only the receiver form
                    raise CelSyntaxError(f"{x}() is a macro: write list.{x}(var, predicate)")
                return ("call", x, None, a)
            return ("id", x)
        if (k, x) == ("op", "("):
            e = self.expr()
            self.eat(")")
            return e
        if (k, x) == ("op", "["):
            return ("list", self.args("]"))
        if (k, x) == ("op", "{"):
            items = []
            while not self.peek("}"):
                key = self.expr()
                self.eat(":")
                items.append((key, self.expr()))
                if not self.peek("}"):
                    self.eat(",")
            self.eat("}")
            return ("map", items)
        raise CelSyntaxError(f"unexpected {x!r}")


def _check_inv(e: Node) -> None:
    """Load-time shape check for inv.* calls: arity, and unique_by's variable."""
    want = INV_ARITY.get(e[1]) if e[2] is None else None
    if want is None:
        return
    if len(e[3]) != want:
        raise CelSyntaxError(f"{e[1]}() takes {want} argument(s), got {len(e[3])}")
    if e[1] == "inv.unique_by" and e[3][1][0] != "id":
        raise CelSyntaxError("inv.unique_by() needs (list, var, expr)")


def parse(src: str) -> Node:
    """Parse rule text to a tuple tree. Raises CelSyntaxError."""
    return _Parser(src).parse()


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


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TY = {datetime.datetime: "timestamp", datetime.timedelta: "duration", type(None): "null"}
_TIME = (datetime.datetime, datetime.timedelta)


def _nty(v: Any) -> str:
    """CEL's name for a value's type in an arithmetic error (int and double told apart)."""
    if _num(v):
        return "double" if isinstance(v, float) else "int"
    return _ty(v)


def _ty(v: Any) -> str:
    return "num" if _num(v) else _TY.get(type(v)) or type(v).__name__


_SCALAR_TYPES = (str, bytes, bool, int, float, datetime.datetime, datetime.timedelta, type(None))


def _eq(a: Any, b: Any) -> bool:
    """CEL equality. A list, map or record compared whole is first read whole, as the engines'
    conversion reads it (cel/values.py): a NaN or +-Infinity anywhere in it is an error (unless
    allowed), a record is the map of its fields, and nested values compare by CEL rules, never by
    Python's == (which calls a shared NaN object equal to itself)."""
    a, b = _data(a), _data(b)           # list elements and map values are read here too (UUID -> string, ...)
    if type(a) is float or type(b) is float:
        _finite(a)
        _finite(b)
    if _num(a) and _num(b):
        return a == b
    if not isinstance(a, _SCALAR_TYPES) or not isinstance(b, _SCALAR_TYPES):
        return _eq_plain(whole(a), whole(b))
    if _ty(a) != _ty(b):
        return False            # CEL heterogeneous equality is false, not error
    return a == b


def whole(v: Any) -> Any:
    """v read whole as plain CEL data (the engines' conversion, cel/_engine.to_plain)."""
    from ._engine import to_plain       # _engine imports this module
    return to_plain(v)


def _kind(v: Any) -> str:
    """The equality class of a plain CEL value: numbers form one (CEL compares int and double)."""
    if isinstance(v, bool):
        return "bool"
    return "num" if isinstance(v, (int, float)) else type(v).__name__


def _eq_plain(a: Any, b: Any) -> bool:
    """CEL equality of two plain values (to_plain output): no Python identity shortcut, NaN != NaN."""
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        return False
    if ka == "list":
        return len(a) == len(b) and all(map(_eq_plain, a, b))
    if ka == "dict":
        if len(a) != len(b):
            return False
        for k, x in a.items():
            ok, y = lookup(b, k)
            if not ok or not _eq_plain(x, y):
                return False
        return True
    return bool(a == b)


def lookup(m: Any, k: Any) -> tuple[bool, Any]:
    """CEL map lookup: (found, value). Keys compare as CEL values: an int key and an integral double
    index are the same key ({1: 'a'}[1.0] == 'a'), a bool is never a number ({1: 'a'}[true] is no
    such key), whatever Python's dict thinks of 1 == 1.0 == True."""
    if type(k) is str:
        return (k in m, m.get(k))
    kk = _kind(k)
    if kk not in ("bool", "num") or k not in m:
        return (False, None)
    for key in m:                       # Python merges 1, 1.0 and True: find the stored key's kind
        if _kind(key) == kk and key == k:
            return (True, m[key])
    return (False, None)


_KEY_TYPES = (str, int, bool)           # CEL map keys: int, uint, bool, string (never double or null)


def map_key(k: Any) -> Any:
    """k as a map literal key, or CelError: CEL keys are int, uint, bool or string."""
    if not isinstance(k, _KEY_TYPES):
        raise CelError(f"unsupported map key type {_nty(k) if _num(k) else _ty(k)}")
    return k


def check_int(v: int) -> int:
    """An int outside CEL's int64 range is an evaluation error on every backend."""
    if not INT_MIN <= v <= INT_MAX:
        raise CelError("integer value out of int64 range")
    return v


def _finite(v: Any) -> Any:
    if isinstance(v, float):
        check_finite(v)
    return v


def _data(v: Any) -> Any:
    """A Python value as CEL reads it (cel/values.py): an Enum member is its value, ints must fit int64,
    a NaN or +-Infinity is an error unless allowed (check_finite), a UUID is its string, an aware
    datetime a UTC timestamp, a timedelta a duration; a naive datetime, a date, a time and a Decimal
    are errors."""
    t = type(v)
    if t is str or t is bool or t is dict or t is list or v is None:
        return v
    if isinstance(v, enum.Enum):
        v = v.value
    if isinstance(v, int) and not isinstance(v, bool):
        check_int(v)
    elif isinstance(v, float):
        check_finite(v)
    elif isinstance(v, values.SCALARS):
        return values.scalar(v)
    elif isinstance(v, (set, frozenset)):
        raise CelError(values.SET)
    return v


_NO_FIELDS = (str, bytes, int, float, list, tuple, *values.SCALARS)


def _select(o: Any, f: str, test: bool = False) -> Any:
    if test and (o is None or isinstance(o, _NO_FIELDS)):
        # CEL has() needs a map or message: on null, a number, a string or a list it is an error (cel-expr)
        raise CelError(f"has() on {_ty(o)}: field selection on a value that has no fields")
    if isinstance(o, dict):
        ok, v = f in o, o.get(f)
    elif o is None or f.startswith("_") or isinstance(o, _NO_FIELDS) or f in values.api_names(type(o)):
        ok, v = False, None
    else:
        try:
            v = getattr(o, f)
        except AttributeError:
            ok, v = False, None
        except Exception as e:      # a property that fails is an evaluation error
            raise raised(f"reading {f!r}", e) from e
        else:
            ok = not callable(v)
    if test:
        return ok                   # CEL has(): presence only, a null value is present
    if not ok:
        raise CelError(f"no such field {f!r} on {type(o).__name__}")
    return _data(v)


_TIME_ARITH = {("+", "timestamp", "duration"), ("+", "duration", "timestamp"), ("+", "duration", "duration"),
               ("-", "timestamp", "timestamp"), ("-", "timestamp", "duration"), ("-", "duration", "duration")}


def _time_arith(op: str, a: Any, b: Any) -> Any:
    """timestamp and duration arithmetic (CEL: ts + dur, dur + ts, dur +- dur, ts - ts, ts - dur), range-checked."""
    if (op, _ty(a), _ty(b)) not in _TIME_ARITH:
        raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
    if isinstance(a, datetime.datetime) and isinstance(b, datetime.datetime):
        return values.duration(a - b)
    if isinstance(b, datetime.datetime):
        return values.ts_add(b, a)
    if isinstance(a, datetime.datetime):
        return values.ts_add(a, b if op == "+" else -b)
    return values.duration(a + b if op == "+" else a - b)


def _arith(op: str, a: Any, b: Any) -> Any:
    if type(a) in _TIME or type(b) in _TIME:
        return _time_arith(op, a, b)
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if op == "+" and isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) + list(b)
    if not (_num(a) and _num(b)) or type(a) is not type(b) or (op == "%" and type(a) is float):
        raise CelError(f"no such overload: {_nty(a)} {op} {_nty(b)}")   # CEL: no int-double mix, no double %
    if op in "/%" and b == 0:
        if op == "/" and isinstance(a, float) and ALLOW_NON_FINITE.get():
            return ieee_div_zero(a, b)
        raise CelError("division by zero")
    if op == "/" and isinstance(a, int):
        q = abs(a) // abs(b)
        return _int_range(q if (a >= 0) == (b > 0) else -q)
    if op == "%" and isinstance(a, int):
        return a - b * _arith("/", a, b)
    if op == "+":
        r = a + b
    elif op == "-":
        r = a - b
    elif op == "*":
        r = a * b
    else:
        r = a / b if op == "/" else a % b
    return check_result(_int_range(r))       # a double overflow (1e200 * 1e200) is an error


def ieee_div_zero(a: float, b: float) -> float:
    """IEEE a / b for a zero double b (Python raises): +-Infinity by the signs, NaN for 0/0 or NaN/0."""
    return math.copysign(math.inf, a) * math.copysign(1.0, b) if a == a and a != 0.0 else math.nan


def _int_range(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool) and not INT_MIN <= v <= INT_MAX:
        raise CelError("integer overflow")
    return v


def _bool(v: Any, what: str) -> bool:
    if not isinstance(v, bool):
        raise CelError(f"{what} needs bool, got {_ty(v)}")
    return v


def evaluate(n: Node, env: dict[str, Any], budget: list[int] | None = None) -> Any:
    """Evaluate a tree against env. Raises CelError on any evaluation error."""
    budget = budget if budget is not None else [DEFAULT_BUDGET]
    budget[0] -= 1
    if budget[0] < 0:
        raise CostExceeded("cost limit exceeded")

    def ev(x: Node, e: dict[str, Any] = env) -> Any:
        return evaluate(x, e, budget)

    k = n[0]
    if k == "lit":
        return n[1]
    if k == "id":
        if n[1] not in env:
            raise CelError(f"undeclared reference {n[1]!r}")
        return _data(env[n[1]])
    if k == "list":
        return [ev(x) for x in n[1]]
    if k == "map":
        out: dict[Any, Any] = {}
        for a, b in n[1]:
            key = map_key(ev(a))
            if key in out:              # a repeated key, or a bool next to the number 1 or 0
                raise CelError("repeated map key" if lookup(out, key)[0] else
                               "a map literal cannot hold both a bool key and the number key it equals in Python")
            out[key] = ev(b)
        return out
    if k == "sel":
        return _select(ev(n[1]), n[2])
    if k == "idx":
        o, i = ev(n[1]), ev(n[2])
        if isinstance(o, dict):
            found, v = lookup(o, i)
            if not found:
                err = CelError("no such key")          # the key is request data: only in .detail
                err.detail = _safe_repr(i)
                raise err
            return _data(v)
        if type(i) is float and i.is_integer() and isinstance(o, (list, tuple)):
            i = int(i)                  # CEL: a list index may be an integral double ([7, 8][1.0] == 8)
        if not isinstance(o, (list, tuple)) or not isinstance(i, int) or isinstance(i, bool):
            raise CelError(f"cannot index {_ty(o)} with {_nty(i) if _num(i) else _ty(i)}")
        if not 0 <= i < len(o):
            err = CelError("index out of range")
            err.detail = f"index {i}, size {len(o)}"
            raise err
        return _data(o[i])
    if k == "cond":
        return ev(n[2]) if _bool(ev(n[1]), "?:") else ev(n[3])
    if k == "un":
        v = ev(n[2])
        if n[1] == "!":
            return not _bool(v, "!")
        if not _num(v):
            raise CelError(f"no such overload: -{_ty(v)}")
        return _int_range(-v)
    if k == "bin":
        op = n[1]
        if op in ("&&", "||"):                     # CEL: error absorbed if the other side decides
            res: list[Any] = []
            for side in (n[2], n[3]):
                try:
                    res.append(_bool(ev(side), op))
                except CostExceeded:
                    raise
                except CelError as e:
                    res.append(e)
            decider = op == "||"
            if decider in res:
                return decider
            for r in res:
                if isinstance(r, CelError):
                    raise r
            return not decider
        a, b = ev(n[2]), ev(n[3])
        if op == "==":
            return _eq(a, b)
        if op == "!=":
            return not _eq(a, b)
        if op == "in":                 # the list or map is read whole, as on the engines
            if isinstance(b, (list, tuple)):
                b = whole(b)
                return any(_eq(a, x) for x in b)
            if isinstance(b, dict):
                return lookup(whole(b), whole(a))[0]
            raise CelError(f"no such overload: in {_ty(b)}")
        if op in ("<", "<=", ">", ">="):
            if not ((_num(a) and _num(b)) or _ty(a) == _ty(b) in ("str", "bool", "timestamp", "duration")):
                raise CelError(f"no such overload: {_ty(a)} {op} {_ty(b)}")
            _finite(a)
            _finite(b)
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return _arith(op, a, b)
    if k == "call":
        return _call(n[1], n[2], n[3], env, budget)
    raise CelError(f"unknown node {k}")


def _iter(v: Any, name: str) -> list[Any]:
    if isinstance(v, (dict, list, tuple)):
        return list(v)
    raise CelError(f"{name}() on {_ty(v)}")


def _call(name: str, tgt: Node | None, args: list[Node], env: dict[str, Any], budget: list[int]) -> Any:
    def ev(x: Node, e: dict[str, Any] = env) -> Any:
        return evaluate(x, e, budget)

    if name in _MACROS or name == "inv.unique_by":
        if name == "inv.unique_by":
            tgt, args = args[0], args[1:]
        if tgt is None:                   # the parser refuses this form; never evaluate it
            raise CelError(f"{name}() needs a receiver list")
        items, var, body = _iter(ev(tgt), name), args[0][1], args[1]
        if name in ("map", "inv.unique_by"):
            vals = [ev(body, {**env, var: x}) for x in items]
            if name == "map":
                return vals
            keys: list[Any] = []
            for v in vals:
                if any(_eq(v, key) for key in keys):
                    return False
                keys.append(v)
            return True
        if name == "filter":
            return [x for x in items if _bool(ev(body, {**env, var: x}), name)]
        hits: list[Any] = []
        for x in items:
            try:
                hits.append(_bool(ev(body, {**env, var: x}), name))
            except CostExceeded:
                raise
            except CelError as e:
                hits.append(e)
        errs = [h for h in hits if isinstance(h, CelError)]
        if name == "exists" and True in hits:       # CEL folds exists with ||, all with &&
            return True
        if name == "all" and False in hits:
            return False
        if errs:
            raise errs[0]
        return {"all": True, "exists": False, "exists_one": hits.count(True) == 1}[name]
    if name == "has":
        return _select(ev(args[0][1]), args[0][2], test=True)
    vals = ([ev(tgt)] if tgt is not None else []) + [ev(a) for a in args]
    if name == "size":
        if len(vals) != 1 or not isinstance(vals[0], (str, list, tuple, dict)):
            raise CelError("size() needs one string, list or map")
        return len(vals[0])
    if name in ("contains", "startsWith", "endsWith"):
        if len(vals) != 2 or not all(isinstance(v, str) for v in vals):
            raise CelError(f"{name}() needs strings")
        s, t = vals
        return {"contains": t in s, "startsWith": s.startswith(t), "endsWith": s.endswith(t)}[name]
    if name == "inv.sum":
        if len(vals) != 1 or not isinstance(vals[0], (list, tuple)) or not all(_num(v) for v in vals[0]):
            raise CelError("inv.sum() needs a list of numbers")
        return check_result(_int_range(sum(vals[0])))
    if name == "matches":
        if len(vals) != 2 or not all(isinstance(v, str) for v in vals):
            raise CelError("matches() needs a string and a pattern")
        return _matches(vals[0], vals[1], budget)
    if name == "inv.zip":
        if len(vals) != 2 or not all(isinstance(v, (list, tuple)) for v in vals):
            raise CelError("inv.zip() needs two lists")
        if len(vals[0]) != len(vals[1]):
            err = CelError("inv.zip(): length mismatch")      # the lengths are request data: only in .detail
            err.detail = f"{len(vals[0])} vs {len(vals[1])}"
            raise err
        return [[a, b] for a, b in zip(*vals)]
    if name in CONVERSIONS:
        if tgt is not None or len(vals) != 1:
            raise CelError(f"{name}() takes one argument")
        return _convert(name, vals[0])
    if name in _GETTERS:
        if tgt is None or len(vals) not in (1, 2):
            raise CelError(f"{name}() takes a receiver and an optional timezone")
        return values.getter(name, *vals)
    raise CelError(f"unknown function {name}()")


_INT_TEXT = re.compile(r"\s*[+-]?[0-9]+\s*")
_FLOAT_TEXT = re.compile(r"\s*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*")
_BOOL_TEXT = {"1": True, "t": True, "true": True, "TRUE": True, "True": True,
              "0": False, "f": False, "false": False, "FALSE": False, "False": False}


def _safe_repr(v: Any) -> str:
    try:
        return repr(v)[:80]
    except Exception:
        return "<unprintable>"


def _convert(name: str, v: Any) -> Any:
    """CEL's type conversions on the subset's values. Errors never quote the value (it is request data).

    int(): int, bool, double (truncated toward zero; NaN, +-Infinity and values outside int64 are
    errors) and base-10 text. double(): int, double and decimal text (NaN and Infinity text are
    errors unless non-finite numbers are allowed). string(): string, int, bool, bytes (UTF-8);
    string(double) is refused because CEL engines print doubles differently (cel-expr '2', others
    '2.0'); string(timestamp) and string(duration) print as CEL does (values.format_*).
    timestamp(): RFC 3339 text or int seconds; duration(): text such as '1h30m'; int(timestamp):
    Unix seconds, rounded down. bool(): bool and the text 1, t, true, TRUE, True, 0, f, false, FALSE, False. dyn(): any value.
    """
    if name == "dyn":
        return v
    if name == "timestamp":
        if isinstance(v, datetime.datetime):
            return v
        if isinstance(v, str):
            return values.parse_timestamp(v)
        if isinstance(v, int) and not isinstance(v, bool):
            return values.ts_add(values.EPOCH, _seconds(v))
    elif name == "duration":
        if isinstance(v, datetime.timedelta):
            return v
        if isinstance(v, str):
            return values.parse_duration(v)
    elif name == "int":
        if isinstance(v, datetime.datetime):
            return values.unix_seconds(v)
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            if v - v != 0.0 or not -2.0**63 < v < 2.0**63:
                raise CelError("int(): double out of int64 range")
            return check_int(int(v))
        if isinstance(v, str):
            if not _INT_TEXT.fullmatch(v):
                raise CelError("int(): cannot convert string to int")
            return _int_range(int(v))
    elif name == "double":
        if isinstance(v, int) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, float):
            return v
        if isinstance(v, str):
            if _FLOAT_TEXT.fullmatch(v):
                return check_result(float(v))      # '1e999' is Infinity
            if v.strip().lower().lstrip("+-") in ("nan", "inf", "infinity"):
                return check_result(float(v.strip()))
            raise CelError("double(): cannot convert string to double")
    elif name == "string":
        if isinstance(v, str):
            return v
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except UnicodeDecodeError:
                raise CelError("string(): bytes are not valid UTF-8") from None
        if isinstance(v, datetime.datetime):
            return values.format_timestamp(v)
        if isinstance(v, datetime.timedelta):
            return values.format_duration(v)
        if isinstance(v, float):
            raise CelError("string(double) is not supported by the subset backend (CEL engines print doubles "
                           "differently); compare the number instead")
    elif name == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            if v not in _BOOL_TEXT:
                raise CelError("bool(): cannot convert string to bool")
            return _BOOL_TEXT[v]
    raise CelError(f"no such overload: {name}({_ty(v)})")


def _seconds(n: int) -> datetime.timedelta:
    try:
        return datetime.timedelta(seconds=n)
    except OverflowError:
        raise CelError("timestamp out of range") from None


_RE_CACHE: dict[str, list[list]] = {}
STEPS_PER_COST = 16          # regex VM steps charged as one unit of the cost budget


def _matches(text: str, pattern: str, budget: list[int] | None = None) -> bool:
    """CEL matches(): an unanchored search with RE2 syntax, in linear time (see _regex).

    Its steps are charged to the cost budget, so no pattern or input can run past it. That is
    stricter than cel-expr: an input past about STEPS_PER_COST * DEFAULT_BUDGET VM
    steps (some tens of thousands of characters for a small pattern) is a rule_error here and a
    result there. The bound is deliberate: free steps per input character would let a caller
    buy CPU time with input size, once per matches() in a loop.
    """
    code = _RE_CACHE.get(pattern)
    if code is None:
        try:
            code = _regex.compile_pattern(pattern)
        except _regex.RegexError as e:
            err = CelError("matches(): bad pattern")   # the pattern may be request data: only in .detail
            err.detail = f"{e} in {_safe_repr(pattern)}"
            raise err from None
        if len(_RE_CACHE) < 256:
            _RE_CACHE[pattern] = code
    left = budget[0] if budget is not None else DEFAULT_BUDGET
    try:
        found, steps = _regex.search(code, text, max(0, left) * STEPS_PER_COST)
    except _regex.StepLimit:
        if budget is not None:
            budget[0] = -1
        raise CostExceeded("cost limit exceeded (matches)") from None
    if budget is not None:
        budget[0] -= steps // STEPS_PER_COST
    return found


class SubsetProgram:
    """A compiled subset expression."""

    __slots__ = ("source", "ast", "budget")

    def __init__(self, source: str, ast: Node, budget: int = DEFAULT_BUDGET) -> None:
        self.source, self.ast, self.budget = source, ast, budget

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env; raises CelError."""
        return evaluate(self.ast, env, [self.budget])

    def references(self) -> frozenset[tuple[str, ...]]:
        """Free identifiers with their static field paths."""
        return frozenset(references(self.ast))

    def reads(self) -> frozenset[tuple[str, ...]]:
        """What the clause reads, inside lists too (see reads())."""
        return frozenset(reads(self.ast))

    def negated_membership(self) -> tuple[tuple[str, ...], list[Any]] | None:
        """If the expression is `!(path in [literals])`, return (path, literals)."""
        a = self.ast
        if not (a[0] == "un" and a[1] == "!" and a[2][0] == "bin" and a[2][1] == "in"):
            return None
        lhs, rhs = a[2][2], a[2][3]
        if rhs[0] != "list" or not all(x[0] == "lit" for x in rhs[1]):
            return None
        refs = references(lhs)
        if lhs[0] not in ("sel", "id") or len(refs) != 1:
            return None
        return next(iter(refs)), [x[1] for x in rhs[1]]


class SubsetBackend:
    """Backend for the stdlib subset evaluator."""

    name = "subset"
    functions = FUNCTIONS               # the loader refuses a call to any other function name

    def __init__(self, budget: int = DEFAULT_BUDGET) -> None:
        self.budget = budget

    def compile(self, expr: str) -> SubsetProgram:
        """Parse expr; raises CelSyntaxError."""
        return SubsetProgram(expr, parse(expr), self.budget)


__all__ = ["CostExceeded", "parse", "evaluate", "references", "reads", "ELEM", "SIZE", "free_names", "SubsetProgram", "SubsetBackend", "FUNCTIONS",
           "ARITY", "arity_error"]
