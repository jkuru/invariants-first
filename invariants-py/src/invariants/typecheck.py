"""Load-time type check of `bad` clauses against the types code declares (spec 2.1, 2.9).

Types come from the target's signature (args, result), the provider declarations, and the
model_call schema (reply). The check is conservative: a name or field whose type is unknown is
dyn and passes, and a clause fails only when an operation has no overload for any pair of
types its operands can have. That is what fails on every CEL backend at run time: ordering
(< <= > >=) and arithmetic across type families (number, string, bool, bytes, null, list, map),
equality across type families (null and dyn excepted), a field a closed type does not have,
logic on a non-bool, `in` on a non-container or with an element of another family than the
list's elements (or a map's keys), a has() test of a field a closed type does not have, string
functions on a non-string, a constant matches() pattern that is not valid RE2, size() on a
scalar, and a clause that is not a boolean. int, uint and double are kinds of their own: they
compare with each other (CEL's heterogeneous numeric comparison), but arithmetic across them and
double % have no overload in CEL, so `args.price * args.qty` (float, int) is refused with the
conversion to write.

Python types map to CEL types by the table in cel/values.py: datetime is a timestamp, timedelta a
duration, UUID a string, an Enum its values' type. A clause that reads a value whose annotation
is a type the mapping refuses (Decimal, date, time, pydantic's NaiveDatetime, a set), or reads a
record, list or map whole that holds one (the engines convert what a clause reads whole; inside a
list only the element fields a comprehension reads, see cel.subset.reads), is refused,
with the conversion to make. A constant timestamp('...') or duration('...') that does not parse is
refused too.
"""
from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import enum
import inspect
import types
import typing
import uuid
from typing import Any, Iterable

from .cel import subset, values
from .cel.errors import CelError

Node = tuple
_KINDS = ("int", "uint", "double", "string", "bool", "bytes", "list", "map", "null", "timestamp", "duration")
NUMERIC = frozenset({"int", "uint", "double"})
# CEL compares numbers across int, uint and double (heterogeneous equality and ordering), but has no
# arithmetic across them: 1.5 * 2 has no overload (convert one side: 1.5 * double(2)).
_CROSS = {(x, y) for x in NUMERIC for y in NUMERIC}
_ORDER_OK = _CROSS | {("string", "string"), ("bool", "bool"), ("bytes", "bytes"),
                      ("timestamp", "timestamp"), ("duration", "duration")}
_EQ_OK = {(k, k) for k in _KINDS} | {(k, "null") for k in _KINDS} | {("null", k) for k in _KINDS} | _CROSS
# (op, left kind, right kind) -> result kind, for every arithmetic overload CEL has
_ARITH: dict[tuple[str, str, str], str] = {
    **{(op, k, k): k for op in ("+", "-", "*", "/") for k in NUMERIC},
    ("%", "int", "int"): "int", ("%", "uint", "uint"): "uint",           # CEL has no double %
    ("+", "string", "string"): "string", ("+", "list", "list"): "list", ("+", "bytes", "bytes"): "bytes",
    ("+", "timestamp", "duration"): "timestamp", ("+", "duration", "timestamp"): "timestamp",
    ("+", "duration", "duration"): "duration", ("-", "timestamp", "timestamp"): "duration",
    ("-", "timestamp", "duration"): "timestamp", ("-", "duration", "duration"): "duration",
}
_DURATION_GETTERS = frozenset({"getHours", "getMinutes", "getSeconds", "getMilliseconds"})
# subset-matcher refusals that a full RE2 (cel-expr, celpy) may accept: not load errors
_RE2_MAYBE_OK = ("unsupported", "pattern too large", "invalid escape", "bad flag group")
_NAMES = {"int": "int", "uint": "uint", "double": "double", "string": "string", "bool": "bool", "bytes": "bytes", "null": "null",
          "list": "list", "map": "map", "timestamp": "timestamp", "duration": "duration"}


_CONVERSIONS: dict[str, T]  # filled below, once T exists


class TypeMismatch(Exception):
    """A clause applies an operation to types it has no overload for."""


class T:
    """A static type: a set of kinds (None: dyn), a list element or map value type, and fields.

    refused: why a value of this type cannot be read by a rule (a Python type the CEL mapping refuses)."""

    __slots__ = ("kinds", "elem", "fields", "py", "refused")

    def __init__(self, kinds: Iterable[str] | None, elem: T | None = None,
                 fields: dict[str, T] | None = None, py: Any = None, refused: str | None = None) -> None:
        self.kinds = frozenset(kinds) if kinds is not None else None
        self.elem, self.fields, self.py, self.refused = elem, fields, py, refused

    def __repr__(self) -> str:
        if self.refused is not None:
            return "a type CEL cannot read"
        if self.kinds is None:
            return "dyn"
        return " | ".join(_NAMES[k] for k in sorted(self.kinds))

    def field(self, name: str) -> T:
        """The type of `.name` on this type; raises TypeMismatch when no value of it has that field.

        A closed type (a dataclass, pydantic model or TypedDict with known fields, or a scalar or
        list, which have no fields) refuses an unknown name. dyn, a plain dict and an object whose
        fields cannot be listed stay open (dyn).
        """
        if self.fields is not None and name in self.fields:
            return self.fields[name]
        if self.py is not None:
            hints = _hints(self.py)
            if name in hints and name not in values.api_names(self.py):
                return from_py(hints[name])
            known = _field_names(self.py) if self.kinds is not None else None
            if known is not None and name not in known:
                raise TypeMismatch(f"no field {name!r} on {self.py.__name__} "
                                   f"(fields: {', '.join(sorted(known)) or 'none'})")
            return DYN
        if self.kinds is not None and self.kinds and "map" not in self.kinds and self.kinds != {"null"}:
            raise TypeMismatch(f"no field {name!r} on {self!r}: only a map or record has fields")
        if self.kinds == frozenset({"map"}) and self.elem is not None:
            return self.elem
        return DYN


DYN = T(None)
BOOL, STRING = T({"bool"}), T({"string"})
INT, UINT, DOUBLE, NUM = T({"int"}), T({"uint"}), T({"double"}), T({"int", "uint", "double"})
TIMESTAMP, DURATION = T({"timestamp"}), T({"duration"})
_CONVERSIONS = {"int": INT, "uint": UINT, "double": DOUBLE, "string": STRING, "bool": BOOL,
                "bytes": T({"bytes"}), "dyn": DYN, "timestamp": TIMESTAMP, "duration": DURATION}


def _hints(obj: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(obj)
    except Exception:
        return {}


def _field_names(py: Any) -> frozenset[str] | None:
    """The public names a record type's values have (fields and class attributes), or None if open."""
    names: set[str] = set()
    if dataclasses.is_dataclass(py):
        names |= {f.name for f in dataclasses.fields(py)}
    elif isinstance(getattr(py, "model_fields", None), dict):
        if (getattr(py, "model_config", None) or {}).get("extra") == "allow":
            return None                          # extra fields are readable attributes
        names |= set(py.model_fields)
    elif typing.is_typeddict(py):
        names |= set(getattr(py, "__annotations__", {}))
    else:
        return None
    names |= set(getattr(py, "__annotations__", {}))
    names |= {n for n in dir(py) if not n.startswith("_")}   # properties and class attributes
    return frozenset(names - values.api_names(py))          # never pydantic's BaseModel API


def merge(a: T, b: T) -> T:
    """The union of two types."""
    if a.refused is not None or b.refused is not None:
        return a if a.refused is not None else b
    if a.kinds is None or b.kinds is None:
        return DYN
    for x, y in ((a, b), (b, a)):                # T | None keeps T's element, fields and record type
        if y.kinds == frozenset({"null"}) and y.elem is None and y.fields is None and y.py is None:
            return T(a.kinds | b.kinds, x.elem, x.fields, x.py)
    return T(a.kinds | b.kinds, a.elem if a.elem is b.elem else None,
             a.fields if a.fields is b.fields else None, a.py if a.py is b.py else None)


def from_py(ann: Any, _depth: int = 0) -> T:
    """The static type of a Python annotation (dyn when it cannot tell)."""
    if _depth > 8 or ann is Any or ann is inspect.Parameter.empty or isinstance(ann, (str, typing.TypeVar)):
        return DYN
    if ann is None or ann is type(None):
        return T({"null"})
    origin = typing.get_origin(ann)
    if origin is typing.Annotated:
        return from_py(typing.get_args(ann)[0], _depth + 1)
    if origin in (typing.Union, types.UnionType):
        out: T | None = None
        for a in typing.get_args(ann):
            t = from_py(a, _depth + 1)
            out = t if out is None else merge(out, t)
        return out or DYN
    if origin is typing.Literal:
        return T({_kind_of(v) for v in typing.get_args(ann)}) if all(_kind_of(v) for v in typing.get_args(ann)) \
            else DYN
    base = origin or ann
    if not isinstance(base, type):
        return DYN
    if getattr(base, "__module__", "").startswith("pydantic"):     # pydantic's datetime annotations
        if base.__name__ == "NaiveDatetime":
            return T(None, refused=values.NAIVE)
        if base.__name__ in ("AwareDatetime", "PastDatetime", "FutureDatetime"):
            return TIMESTAMP
    if issubclass(base, datetime.datetime):
        return TIMESTAMP
    if issubclass(base, datetime.timedelta):
        return DURATION
    if issubclass(base, uuid.UUID):
        return STRING
    for refused_type, why in values.REFUSED:
        if issubclass(base, refused_type):
            return T(None, refused=why)
    if issubclass(base, enum.Enum):
        kinds = {_kind_of(m.value) for m in base}
        return DYN if "" in kinds or not kinds else T(kinds)
    if base is bool:
        return BOOL
    if issubclass(base, float):
        return DOUBLE           # a float parameter is a CEL double (pass 2.0, not 2: CEL never converts)
    if issubclass(base, int):
        return INT
    if issubclass(base, str):
        return STRING
    if issubclass(base, bytes):
        return T({"bytes"})
    args = typing.get_args(ann)
    if issubclass(base, collections.abc.Mapping):
        return T({"map"}, elem=from_py(args[1], _depth + 1) if len(args) == 2 else DYN)
    if issubclass(base, (list, tuple, collections.abc.Sequence)) and not issubclass(base, (str, bytes)):
        if base is tuple and args and args[-1] is not Ellipsis:
            elem: T | None = None
            for a in args:
                t = from_py(a, _depth + 1)
                elem = t if elem is None else merge(elem, t)
            return T({"list"}, elem=elem or DYN)
        return T({"list"}, elem=from_py(args[0], _depth + 1) if args else DYN)
    if dataclasses.is_dataclass(base) or isinstance(getattr(base, "model_fields", None), dict):
        return T({"map"}, py=base)
    return T(None, py=base)        # some other object: its annotated fields are known, its kind is not


def _kind_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str):
        return "string"
    if isinstance(v, bytes):
        return "bytes"
    return ""


def _text(n: Node) -> str:
    from .cel._engine import unparse
    try:
        return unparse(n, {"inv.sum": "inv.sum", "inv.zip": "inv.zip", "inv.unique": "inv.unique"})
    except Exception:
        return "?"


def _need(n: Node, t: T, kinds: set[str], what: str) -> None:
    if t.kinds is not None and not (t.kinds & kinds):
        raise TypeMismatch(f"{what} needs {' or '.join(_NAMES[k] for k in sorted(kinds))}, "
                           f"but {_text(n)} is {t!r}")


def _pair(op: str, n: Node, a: T, b: T, ok: set[tuple[str, str]]) -> None:
    if a.kinds is None or b.kinds is None:
        return
    if not any((x, y) in ok for x in a.kinds for y in b.kinds):
        raise TypeMismatch(f"no overload for {op} on {a!r} and {b!r} in {_text(n)}")


def infer(n: Node, env: dict[str, T]) -> T:
    """The static type of a subset tree; raises TypeMismatch on an operation with no overload."""
    k = n[0]
    if k == "lit":
        kind = _kind_of(n[1])
        return T({kind}) if kind else DYN
    if k == "id":
        return env.get(n[1], DYN)
    if k == "list":
        elem: T | None = None
        for x in n[1]:
            t = infer(x, env)
            elem = t if elem is None else merge(elem, t)
        return T({"list"}, elem=elem or DYN)
    if k == "map":
        val: T | None = None
        for a, b in n[1]:
            infer(a, env)
            t = infer(b, env)
            val = t if val is None else merge(val, t)
        return T({"map"}, elem=val or DYN)
    if k == "sel":
        return infer(n[1], env).field(n[2])
    if k == "idx":
        base = infer(n[1], env)
        infer(n[2], env)
        if base.kinds is not None and base.kinds <= {"list", "map"} and base.elem is not None:
            return base.elem
        return DYN
    if k == "cond":
        _need(n[1], infer(n[1], env), {"bool"}, "?:")
        return merge(infer(n[2], env), infer(n[3], env))
    if k == "un":
        t = infer(n[2], env)
        if n[1] == "!":
            _need(n[2], t, {"bool"}, "!")
            return BOOL
        _need(n[2], t, {"int", "double"}, "unary -")          # CEL has no -duration, no -uint
        return T(t.kinds & {"int", "double"}) if t.kinds is not None else T({"int", "double"})
    if k == "bin":
        op, a, b = n[1], infer(n[2], env), infer(n[3], env)
        if op in ("&&", "||"):
            _need(n[2], a, {"bool"}, op)
            _need(n[3], b, {"bool"}, op)
            return BOOL
        if op in ("==", "!="):
            _pair(op, n, a, b, _EQ_OK)
            return BOOL
        if op == "in":
            _need(n[3], b, {"list", "map"}, "in")
            if b.kinds == frozenset({"list"}) and b.elem is not None:   # x in list: x == some element
                _pair("in", n, a, b.elem, _EQ_OK)
            elif b.kinds == frozenset({"map"}) and b.py is None:        # map keys: int, uint, bool, string
                _pair("in", n, a, T({"int", "uint", "bool", "string"}), _EQ_OK)
            return BOOL
        if op in ("<", "<=", ">", ">="):
            _pair(op, n, a, b, _ORDER_OK)
            return BOOL
        if a.kinds is None or b.kinds is None:          # conservative: dyn passes, and so does its result
            return DYN
        res = {_ARITH[(op, x, y)] for x in a.kinds for y in b.kinds if (op, x, y) in _ARITH}
        if not res:
            raise TypeMismatch(f"no overload for {op} on {a!r} and {b!r} in {_text(n)}{_numeric_hint(op, n, a, b)}")
        return T(res, elem=DYN if "list" in res else None)
    if k == "call":
        return _infer_call(n, env)
    return DYN


def _numeric_hint(op: str, n: Node, a: T, b: T) -> str:
    """How to fix arithmetic CEL has no overload for: across int and double, or double %."""
    if a.kinds is None or b.kinds is None or not (a.kinds <= NUMERIC and b.kinds <= NUMERIC):
        return ""
    if op == "%":
        return "; CEL has % on int and uint only: convert with int(...)"
    if a.kinds == {"double"} and b.kinds <= {"int", "uint"}:
        return f"; CEL has no implicit numeric conversion: convert one side, e.g. double({_text(n[3])})"
    if b.kinds == {"double"} and a.kinds <= {"int", "uint"}:
        return f"; CEL has no implicit numeric conversion: convert one side, e.g. double({_text(n[2])})"
    return "; CEL has no implicit numeric conversion: convert one side with double(...), int(...) or uint(...)"


def _infer_call(n: Node, env: dict[str, T]) -> T:
    name, tgt, args = n[1], n[2], n[3]
    if name in ("all", "exists", "exists_one", "map", "filter", "inv.unique_by"):
        lst, var, body = subset._macro_parts(n)
        coll = infer(lst, env)
        _need(lst, coll, {"list", "map"}, f"{name}()")
        item = coll.elem if coll.kinds == frozenset({"list"}) and coll.elem is not None else DYN
        bt = infer(body, {**env, var: item})
        if name == "map":
            return T({"list"}, elem=bt)
        if name == "filter":
            _need(body, bt, {"bool"}, "filter()")
            return coll
        if name != "inv.unique_by":
            _need(body, bt, {"bool"}, f"{name}()")
        return BOOL
    if name == "has":
        if len(args) == 1 and args[0][0] == "sel":    # has(x.f): f must be a field x's type can have
            infer(args[0], env)
        return BOOL
    recv = infer(tgt, env) if tgt is not None else None
    vals = [infer(a, env) for a in args]
    operands = ([(tgt, recv)] if tgt is not None and recv is not None else []) + list(zip(args, vals))
    if name == "size":
        for node, t in operands[:1]:
            _need(node, t, {"string", "list", "map", "bytes"}, "size()")
        return INT
    if name in ("contains", "startsWith", "endsWith", "matches"):
        for node, t in operands:
            _need(node, t, {"string"}, f"{name}()")
        if name == "matches" and args and args[-1][0] == "lit" and isinstance(args[-1][1], str):
            from .cel import _regex
            try:                     # a constant pattern that does not compile errors on every call
                _regex.compile_pattern(args[-1][1])
            except _regex.RegexError as e:
                if str(e).startswith(_RE2_MAYBE_OK):    # RE2 may take what the subset matcher does not
                    return BOOL
                raise TypeMismatch(f"matches(): bad pattern {args[-1][1]!r}: {e}") from None
        return BOOL
    if name == "inv.sum":
        for node, t in operands[:1]:
            _need(node, t, {"list"}, "inv.sum()")
            if t.kinds == frozenset({"list"}) and t.elem is not None and t.elem.kinds is not None \
                    and t.elem.kinds <= NUMERIC:
                return T(t.elem.kinds)
        return NUM
    if name == "inv.zip":
        for node, t in operands:
            _need(node, t, {"list"}, "inv.zip()")
        return T({"list"}, elem=T({"list"}))
    if name in _CONVERSIONS:
        if tgt is not None or len(args) != 1:
            raise TypeMismatch(f"{name}() takes exactly one argument, written {name}(x)")
        if name in ("timestamp", "duration") and args[0][0] == "lit" and isinstance(args[0][1], str):
            try:                     # a constant that does not parse errors on every call
                (values.parse_timestamp if name == "timestamp" else values.parse_duration)(args[0][1])
            except CelError as e:
                raise TypeMismatch(f"{name}({args[0][1]!r}): {e}") from None
        if name in ("timestamp", "duration"):
            want = {"timestamp": {"string", "timestamp", "int"}, "duration": {"string", "duration"}}[name]
            _need(args[0], vals[0], want, f"{name}()")
        return _CONVERSIONS[name]
    if name in subset._GETTERS and recv is not None and tgt is not None:
        kinds = {"timestamp", "duration"} if name in _DURATION_GETTERS else {"timestamp"}
        _need(tgt, recv, kinds, f"{name}()")
        if len(args) == 1:
            _need(args[0], vals[0], {"string"}, f"{name}() timezone")
            if args[0][0] == "lit" and isinstance(args[0][1], str):
                try:
                    values.zone(args[0][1])
                except CelError:
                    raise TypeMismatch(f"{name}({args[0][1]!r}): unknown timezone") from None
        return INT
    return DYN


def refused_in(t: T, _depth: int = 0, _seen: frozenset[int] = frozenset()) -> tuple[str, str] | None:
    """(where, why) for the first refused type a value of type t can hold ('' for t itself), else None."""
    if t.refused is not None:
        return "", t.refused
    if _depth > 6:
        return None
    parts: list[tuple[str, T]] = []
    if t.elem is not None:
        parts.append(("[]", t.elem))
    for k, ft in (t.fields or {}).items():
        parts.append((f".{k}", ft))
    if t.py is not None and id(t.py) not in _seen:
        _seen = _seen | {id(t.py)}
        for k, ann in _hints(t.py).items():
            if not k.startswith("_") and typing.get_origin(ann) is not typing.ClassVar:
                parts.append((f".{k}", from_py(ann)))
    for where, sub in parts:
        hit = refused_in(sub, _depth + 1, _seen)
        if hit is not None:
            return where + hit[0], hit[1]
    return None


def _read_type(ref: tuple[str, ...], env: dict[str, T]) -> tuple[T | None, str]:
    """(the type of what a reads() path converts, the path to it), None when only a length or map
    keys are read. It mirrors _engine._project: ELEM steps into a list's elements, SIZE reads a list's
    or map's length, and either on another type (a record, dyn) converts the value there whole."""
    t = env.get(ref[0], DYN)
    done = [ref[0]]
    for f in ref[1:]:
        if t.refused is not None:
            break
        if f in (subset.ELEM, subset.SIZE):
            lists = t.kinds is not None and t.kinds <= {"list", "null"} and "list" in t.kinds
            maps = t.kinds is not None and t.kinds <= {"map", "null"} and "map" in t.kinds and t.py is None
            if f == subset.SIZE and (lists or maps) or (f == subset.ELEM and maps):
                return None, ".".join(done)
            if f == subset.ELEM and lists:
                t = t.elem if t.elem is not None else DYN
                done.append("[]")
                continue
            break                                   # the engine converts this value whole
        t = t.field(f)
        done.append(f)
    return t, ".".join(done).replace(".[]", "[]")


def check_clause(source: str, env: dict[str, T]) -> list[T]:
    """Type-check one clause; raises TypeMismatch. The clause must be able to be a bool.

    Returns the static types of the values the clause reads (what an engine converts: in a list
    read only through a comprehension, just the element fields the body reads)."""
    tree = subset.parse(source)
    read: list[T] = []
    for ref in sorted(subset.reads(tree)):          # every value a backend converts for the clause
        t, path = _read_type(ref, env)
        if t is None:
            continue
        hit = refused_in(t)
        if hit is not None:
            where, why = hit
            raise TypeMismatch(f"{path} cannot be read by a rule: {why}" if not where else
                               f"it reads {path} whole, and {path}{where} cannot be read by a rule: {why}")
        read.append(t)
    t = infer(tree, env)
    if t.kinds is not None and "bool" not in t.kinds:
        raise TypeMismatch(f"the clause is {t!r}, not a bool")
    return read


def signature_types(fn: Any) -> tuple[dict[str, T], T]:
    """({param: type}, return type) of a callable from its annotations (dyn where unknown)."""
    if fn is None:
        return {}, DYN
    target = inspect.unwrap(fn)
    target = getattr(target, "__func__", target)
    hints = _hints(target)
    params: dict[str, T] = {}
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return {}, DYN
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        params[p.name] = from_py(hints.get(p.name, inspect.Parameter.empty))
    return params, from_py(hints.get("return", inspect.Parameter.empty))


__all__ = ["T", "DYN", "TypeMismatch", "check_clause", "from_py", "infer", "signature_types"]
