"""Helpers for the engine backend (cel-expr), kept apart from it so another engine can share them.

Every engine backend uses the subset parser as its front end: it checks the rule's syntax and
inv.* shapes, computes `references()` and `negated_membership()`, and rewrites the inv.*
extensions into calls the engine knows. So a rule loads (or fails to load) the same way on
every backend. Rule text is parsed and re-printed, never eval'd or exec'd.

Env values reach an engine as plain data: only the field paths the rule references are read,
then converted by the one mapping in cel/values.py (dataclass, pydantic model or plain object ->
map, tuple -> list, Enum -> value, UUID -> string, aware datetime -> UTC timestamp, timedelta ->
duration; naive datetime, date, time and Decimal refused).
"""
from __future__ import annotations

import dataclasses
import enum
import json
import re
from collections.abc import Mapping
from typing import Any, Callable

from . import subset, values
from .errors import (NON_FINITE, NON_FINITE_RESULT, CelError, CelSyntaxError, check_finite, check_result,
                     raised)

Node = tuple
INV_FUNCS = ("inv.sum", "inv.zip", "inv.unique_by")
# The CEL standard functions the engine backend evaluates (global or receiver form), plus the
# macros and the inv.* extensions. The loader refuses a clause that calls any other name: the
# string extensions (split, lowerAscii, ...) and math/list extensions are not in the engine here.
ENGINE_FUNCTIONS = frozenset({
    "size", "contains", "startsWith", "endsWith", "matches", "has",
    "all", "exists", "exists_one", "map", "filter",
    "int", "uint", "double", "string", "bytes", "bool", "type", "dyn", "timestamp", "duration",
    "getFullYear", "getMonth", "getDayOfYear", "getDayOfMonth", "getDate", "getDayOfWeek",
    "getHours", "getMinutes", "getSeconds", "getMilliseconds", *INV_FUNCS})


# ---- shared: engine error text without request data --------------------------------------
# Engine messages quote map keys, regex patterns and values from the request. By default only a
# fixed text for the error's kind is reported; the raw text is kept in CelError.detail (shown with
# inv.configure(debug=True)). Every pattern is matched at the start of the message, after the
# engine's status codes (NOT_FOUND: ...), never anywhere in it: request data comes later in a
# message, so a key or pattern that reads "cost limit exceeded" cannot pick the kind. The specific
# kinds come first, and a kind whose text is reported is reported as the pattern matched it, which
# holds no request data.
_STATUS = re.compile(r"(?:[A-Z][A-Z_]*: )*")
_OWN = re.compile("|".join((                # the library's own texts, raised inside the engine
    r"cost limit exceeded(?: \(cel-expr: [0-9,]+ comprehension iterations per clause, nested comprehensions "
    r"multiply\))?",
    re.escape(NON_FINITE), re.escape(NON_FINITE_RESULT),
    r"(?:timestamp|duration)\(\): (?:not an RFC 3339 timestamp \(e\.g\. 2024-01-31T09:30:00Z\)|not a duration "
    r"\(e\.g\. '1h30m', '90s', '1\.5s'\)|sub-microsecond precision is not supported|out of range or not a "
    r"valid date and time)",
    r"(?:timestamp|duration) out of range",
    r"unknown timezone(?: \(an offset must be between -23:59 and \+23:59\))?|timezone must be a string",
    r"inv\.sum\(\) needs a list of numbers|inv\.zip\(\) needs two lists|inv\.zip\(\): length mismatch|"
    r"inv\.unique_by\(\) needs a list or map",
)))
_KINDS: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p, re.I), k) for p, k in (
    (r"(?:key not found|no such key)\b", "no such key"),
    (r"no such (?:member|field)\b", "no such field"),
    (r"(?:invalid )?regular expression|match error|bad pattern|matches\(\): bad pattern",
     "invalid regular expression"),
    (r"(?:divide|division|modulus)(?: or divide)? by zero", "division by zero"),
    (r"no (?:matching|such) overloads?(?: found)?\b", "no such overload"),
    (r"undeclared reference to '([A-Za-z_][A-Za-z0-9_.]*)'", "undeclared reference"),
    (r'no value with name "([A-Za-z_][A-Za-z0-9_.]*)"', "undeclared reference"),
    (r"index\b", "index out of range"),
    (r"(?:applying )?select\b|field selection", "field selection on a value that has no fields"),
    (r"integer overflow|(?:[a-z0-9]+ )?out of (?:int64 |uint64 )?range|[a-z0-9]+ overflow", "value out of range"),
    (r"cannot convert|type conversion error|[a-z]+\(\): cannot convert", "conversion failed"),
    (r"non-finite number", "non-finite number (NaN or Infinity)"),
))
# A signature such as _+_(string, int64) or size(list(dyn)): function and type names only.
_TYPE = r"[\w.]+(?:\([\w., ]*\))?"
_SIGNATURE = re.compile(rf"\s*:?\s*([\w.+\-*/%<>=!@&|?\[\]]+\((?:{_TYPE}(?:, {_TYPE})*)?\))\s*")

# The key or field a "no such key" message names, as the whole rest of it (cel-expr: Key not found in map : "f").
_QUOTED = re.compile(r"""(?:\s+in map)?\s*:?\s*(['"])([A-Za-z_][A-Za-z0-9_]{0,99})\1\s*""")


def redact(msg: str, names: frozenset[str] = frozenset()) -> str:
    """A fixed text for the kind of an engine error, without the request values its text may quote.

    names: identifiers of the rule text. A missing key or field, or an undeclared name, that is one
    of them is rule text, not request data, so it is named ("no such key 'zz'").
    """
    st = _STATUS.match(msg)          # always matches (possibly the empty string)
    at = st.end() if st else 0
    m = _OWN.match(msg, at)
    if m:
        return m.group(0)
    for pat, kind in _KINDS:
        m = pat.match(msg, at)
        if not m:
            continue
        if kind in ("no such key", "no such field"):
            q = _QUOTED.fullmatch(msg, m.end())
            return f"{kind} '{q.group(2)}'" if q and q.group(2) in names else kind
        if kind == "undeclared reference":
            name = m.group(1)
            return f"undeclared reference to '{name}'" if name.split(".")[0] in names else kind
        if kind == "no such overload":
            sig = _SIGNATURE.fullmatch(msg, m.end())
            return f"{kind}: {sig.group(1)}" if sig else kind
        return kind
    return "evaluation error"


def engine_error(msg: str, prefix: str | None = None, names: frozenset[str] = frozenset()) -> CelError:
    """CelError('[<prefix>: ]<kind>'), with the engine's full text kept in .detail."""
    err = CelError(f"{prefix}: {redact(msg, names)}" if prefix else redact(msg, names))
    err.detail = msg
    return err


# ---- shared: the inv.* extension functions, on plain Python values ----------------------

def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ext_sum(xs: Any) -> Any:
    """inv.sum(list): the sum of a list of numbers (0 for an empty list)."""
    if not isinstance(xs, (list, tuple)) or not all(_num(v) for v in xs):
        raise CelError("inv.sum() needs a list of numbers")
    total = sum(xs)
    return subset.check_int(total) if isinstance(total, int) else check_result(total)


def ext_zip(a: Any, b: Any) -> list[list[Any]]:
    """inv.zip(list, list): pairs; unequal lengths are an error, never a truncation."""
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
        raise CelError("inv.zip() needs two lists")
    if len(a) != len(b):
        raise zip_mismatch(len(a), len(b))
    return [[x, y] for x, y in zip(a, b)]


def zip_mismatch(n: int, m: int) -> CelError:
    """inv.zip()'s length error: the lengths come from the request, so they are only in .detail."""
    err = CelError("inv.zip(): length mismatch")
    err.detail = f"{n} vs {m}"
    return err


def ext_unique(vals: Any) -> bool:
    """True if no two values are equal (the tail of inv.unique_by after the map)."""
    if not isinstance(vals, (list, tuple)):
        raise CelError("inv.unique_by() needs a list or map")
    seen: list[Any] = []
    for v in vals:
        if any(subset._eq(v, s) for s in seen):
            return False
        seen.append(v)
    return True


# ---- shared: printing a subset tree back to CEL text, with the extensions renamed --------

def uses_inv(n: Node, names: tuple[str, ...] = INV_FUNCS) -> bool:
    """True if the tree calls any of names."""
    if n[0] == "call" and n[1] in names:
        return True
    return any(uses_inv(c, names) for c in subset.kids(n))


def _lit(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return repr(v)


ARITH = frozenset({"+", "-", "*", "/"})


def _map_kids(n: Node, f: Any) -> Node:
    """n with f applied to each child node (map items and call arguments included)."""
    k = n[0]
    if k == "list":
        return ("list", [f(x) for x in n[1]])
    if k == "map":
        return ("map", [(f(a), f(b)) for a, b in n[1]])
    if k == "sel":
        return ("sel", f(n[1]), n[2])
    if k == "idx":
        return ("idx", f(n[1]), f(n[2]))
    if k == "cond":
        return ("cond", f(n[1]), f(n[2]), f(n[3]))
    if k == "un":
        return ("un", n[1], f(n[2]))
    if k == "bin":
        return ("bin", n[1], f(n[2]), f(n[3]))
    if k == "call":
        if n[1] == "has":
            return n
        return ("call", n[1], None if n[2] is None else f(n[2]), [f(a) for a in n[3]])
    return n


def mark_finite(n: Node, inside: bool = False) -> Node:
    """The tree with a ("fin", node) check, which unparse prints, on each value evaluation can make
    a NaN or +-Infinity from finite input: the root of each arithmetic tree (+, -, *, / and unary -),
    each divisor, and each double() result. A one-argument double(x) becomes ("fin_double", x), so a
    backend can also convert strings.

    Checking the root of an arithmetic tree is enough because a NaN or +-Infinity operand of +, -
    or * makes a NaN or +-Infinity result, and so does one in a numerator; only a divisor can hide
    one (x / Infinity == 0.0), so a divisor that is itself arithmetic is checked on its own. This
    keeps the nesting the engine sees close to the rule's: a 50-term sum gets one check, not 49.
    inside: n is an operand of an arithmetic operator that passes a non-finite value up.
    """
    if n[0] == "bin" and n[1] in ARITH:
        m: Node = ("bin", n[1], mark_finite(n[2], True), mark_finite(n[3], n[1] != "/"))
        return m if inside else ("fin", m)
    if n[0] == "un" and n[1] == "-":
        return ("un", "-", mark_finite(n[2], inside))
    m = _map_kids(n, mark_finite)
    if m[0] == "call" and m[1] == "double" and m[2] is None and len(m[3]) == 1:
        return ("fin_double", m[3][0])
    if m[0] == "call" and m[1] == "double" and m[2] is None:
        return ("fin", m)
    return m


def needs_finite(n: Node) -> bool:
    """True if the tree has arithmetic or a double() call."""
    if (n[0] == "bin" and n[1] in ARITH) or (n[0] == "call" and n[1] == "double"):
        return True
    return any(needs_finite(c) for c in subset.kids(n))


def _is_text(x: Node) -> bool:
    return x[0] == "lit" and isinstance(x[1], str)


def zone_calls(n: Node) -> bool:
    """True if the tree calls a timestamp getter with a timezone that is not a string literal."""
    if n[0] == "call" and n[1] in subset._GETTERS and n[2] is not None and len(n[3]) == 1 \
            and not _is_text(n[3][0]):
        return True
    return any(zone_calls(c) for c in subset.kids(n))


def conv_calls(n: Node, names: frozenset[str]) -> bool:
    """True if the tree calls one of the conversions in names on something other than a string literal."""
    if n[0] == "call" and n[1] in names and n[2] is None and len(n[3]) == 1 and not _is_text(n[3][0]):
        return True
    return any(conv_calls(c, names) for c in subset.kids(n))


CHAIN = 8            # a +/- chain longer than this is printed in cel.bind chunks (see _chain)


def _chain(n: Node) -> list[tuple[str | None, Node]]:
    """A left-associative chain of + and -, as [(None, first), (op, term), ...]."""
    out: list[tuple[str | None, Node]] = []
    while n[0] == "bin" and n[1] in ("+", "-"):
        out.append((n[1], n[3]))
        n = n[2]
    out.append((None, n))
    return out[::-1]


def unparse(n: Node, rename: Mapping[str, str], fin: Mapping[str, str] | None = None,
            avoid: frozenset[str] = frozenset()) -> str:
    """CEL text for a subset tree. rename maps inv.sum, inv.zip and inv.unique to engine names.

    fin: format strings for the nodes of mark_finite: fin["fin"] ({0}: the node's text) and
    optionally fin["double"] ({0}: the argument's text; default: fin["fin"] around double(x)), and
    for an engine with a recursion limit: fin["timestamp"] and fin["duration"] ({0}: the argument
    of a timestamp() or duration() call that is not a string literal), and fin["chunk"]: print a
    +/- chain longer than CHAIN terms as nested cel.bind chunks, which keep its left-to-right order
    (and so its rounding and overflow) while its nesting grows with the square root of its length.
    avoid: identifiers of the rule text; a cel.bind variable never takes one of them.
    """
    def u(x: Node) -> str:
        return unparse(x, rename, fin, avoid)

    def p(x: Node) -> str:
        return u(x) if x[0] in ("lit", "id", "list", "map", "sel", "idx", "call", "fin", "fin_double") \
            else f"({u(x)})"

    k = n[0]
    if k == "fin":
        return fin["fin"].format(u(n[1])) if fin else u(n[1])
    if k == "fin_double":
        if fin and "double" in fin:
            return fin["double"].format(u(n[1]))
        text = f"double({u(n[1])})"
        return fin["fin"].format(text) if fin else text
    if k == "lit":
        return _lit(n[1])
    if k == "id":
        return n[1]
    if k == "list":
        return "[" + ", ".join(u(x) for x in n[1]) + "]"
    if k == "map":
        return "{" + ", ".join(f"{u(a)}: {u(b)}" for a, b in n[1]) + "}"
    if k == "sel":
        return f"{p(n[1])}.{n[2]}"
    if k == "idx":
        return f"{p(n[1])}[{u(n[2])}]"
    if k == "cond":
        return f"({u(n[1])}) ? ({u(n[2])}) : ({u(n[3])})"
    if k == "un":
        if n[1] == "-" and n[2][0] == "lit" and _num(n[2][1]):
            return f"-{_lit(n[2][1])}"
        return f"{n[1]}({u(n[2])})"
    if k == "bin":
        if n[1] in ("&&", "||"):        # printed flat, so the engine's parser can balance a long chain
            terms, x = [], n
            while x[0] == "bin" and x[1] == n[1]:
                terms.append(x[3])
                x = x[2]
            terms.append(x)
            return f" {n[1]} ".join(f"({u(t)})" for t in reversed(terms))
        if fin and "chunk" in fin and n[1] in ("+", "-"):
            terms = _chain(n)
            if len(terms) > CHAIN:
                return _chunked(terms, u, avoid)
        return f"({u(n[2])}) {n[1]} ({u(n[3])})"
    if k == "call":
        name, tgt, args = n[1], n[2], n[3]
        if fin and name in fin and name in ("timestamp", "duration") and tgt is None and len(args) == 1 \
                and not _is_text(args[0]):
            return fin[name].format(u(args[0]))
        if fin and "zone" in fin and name in subset._GETTERS and tgt is not None and len(args) == 1 \
                and not _is_text(args[0]):    # a timezone from data: checked by values.zone first
            return f"{p(tgt)}.{name}({fin['zone'].format(u(args[0]))})"
        if name == "inv.unique_by":
            lst, var, body = args[0], args[1], args[2]
            return f"{rename['inv.unique']}({p(lst)}.map({var[1]}, {u(body)}))"
        fn = rename.get(name, name)
        text = ", ".join(u(a) for a in args)
        return f"{p(tgt)}.{fn}({text})" if tgt is not None else f"{fn}({text})"
    raise CelSyntaxError(f"cannot print node {k}")


def _chunked(terms: list[tuple[str | None, Node]], u: Callable[[Node], str], avoid: frozenset[str]) -> str:
    """t0 op1 t1 ... as cel.bind(s0, <first chunk>, cel.bind(s1, s0 <next chunk>, ... s_k <last chunk>))."""
    size = max(CHAIN, int(len(terms) ** 0.5 + 0.999))
    chunks = [terms[i:i + size] for i in range(0, len(terms), size)]
    base = "__inv_s"
    while any(x.startswith(base) for x in avoid):
        base += "_"
    acc: str | None = None
    head: list[str] = []
    for i, chunk in enumerate(chunks):
        text = acc or ""
        for op, t in chunk:
            text = f"({u(t)})" if not text else f"{text} {op} ({u(t)})"
        if i == len(chunks) - 1:
            body = text
            break
        var = f"{base}{i}"
        head.append(f"cel.bind({var}, {text}, ")
        acc = var
    return "".join(head) + body + ")" * len(head)


# ---- shared: turning env values into plain data, reading only referenced paths -----------

_MAX_DEPTH = 64


def _fields(o: Any) -> dict[str, Any] | None:
    """Public data fields of a non-mapping object, or None if it is not record-like."""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o) if not f.name.startswith("_")}
    mf = getattr(type(o), "model_fields", None)
    if isinstance(mf, dict):
        return {k: getattr(o, k) for k in mf if not k.startswith("_")}
    out: dict[str, Any] = {}
    for k, v in getattr(o, "__dict__", {}).items():
        if not k.startswith("_") and not callable(v):
            out[k] = v
    for k in getattr(type(o), "__slots__", ()):
        if not k.startswith("_") and hasattr(o, k) and not callable(getattr(o, k)):
            out[k] = getattr(o, k)
    return out if (out or hasattr(o, "__dict__") or hasattr(type(o), "__slots__")) else None


Leaf = Callable[[Any], Any]


def _same(v: Any) -> Any:
    return v


def to_plain(v: Any, _stack: tuple[int, ...] = (), leaf: Leaf = _same) -> Any:
    """Convert a Python value to plain CEL data (None, bool, int, float, str, bytes, list, dict, and
    UTC datetime / timedelta for timestamps and durations), by the mapping in cel/values.py.

    leaf: applied to each timestamp and duration (an engine that needs its own types for them)."""
    if isinstance(v, enum.Enum):
        return to_plain(v.value, _stack, leaf)
    if type(v) is int or (isinstance(v, int) and not isinstance(v, bool)):
        return subset.check_int(int(v))
    if type(v) is float:
        return check_finite(v)
    if v is None or type(v) in (bool, str, bytes):
        return v
    if isinstance(v, values.SCALARS):
        x = values.scalar(v)
        return x if isinstance(x, str) else leaf(x)
    for t in (bool, float, str, bytes):        # subclasses become the base type
        if isinstance(v, t):
            return check_finite(float(v)) if t is float else t(v)  # type: ignore[arg-type]
    if id(v) in _stack or len(_stack) > _MAX_DEPTH:
        raise CelError("value is cyclic or nested too deeply")
    st = _stack + (id(v),)
    if isinstance(v, Mapping):
        out = {}
        for k, x in v.items():
            k = to_plain(k, st)
            if not isinstance(k, (str, int, bool)):
                raise CelError(f"unsupported map key type {type(k).__name__}")
            out[k] = to_plain(x, st, leaf)
        return out
    if isinstance(v, (list, tuple)):
        return [to_plain(x, st, leaf) for x in v]
    if isinstance(v, (set, frozenset)):
        raise CelError(values.SET)
    f = _fields(v)
    if f is None:
        raise CelError(f"unsupported value type {type(v).__name__}")
    return {k: to_plain(x, st, leaf) for k, x in f.items()}


_NO_FIELDS = (str, bytes, int, float, list, tuple, *values.SCALARS)
_LEAVES = (bool, int, float, str, bytes, list, tuple, enum.Enum, *values.SCALARS)


def _step(o: Any, name: str) -> tuple[bool, Any]:
    """Select field name as the subset does: map key, or public non-callable attribute."""
    if isinstance(o, Mapping):
        return (name in o, o.get(name))
    if o is None or name.startswith("_") or isinstance(o, _NO_FIELDS) or name in values.api_names(type(o)):
        return (False, None)
    try:
        v = getattr(o, name)
    except AttributeError:
        return (False, None)
    except Exception as e:      # a property that fails is an evaluation error
        raise raised(f"reading {name!r}", e) from e
    return (not callable(v), v)


_ELEM, _SIZE = subset.ELEM, subset.SIZE


def _project(v: Any, tails: list[tuple[str, ...]], leaf: Leaf) -> Any:
    if not tails or any(len(t) == 0 for t in tails):
        return to_plain(v, (), leaf)
    heads = {t[0] for t in tails}
    if heads <= {_ELEM, _SIZE} and isinstance(v, (list, tuple)):
        sub = [t[1:] for t in tails if t[0] == _ELEM]       # the fields each element is read for
        if not sub:
            return [None] * len(v)                          # only the length is read
        return [_project(x, sub, leaf) for x in v]
    if _ELEM in heads or _SIZE in heads:
        if isinstance(v, Mapping):
            return _project_keys(v, tails, leaf)
        return to_plain(v, (), leaf)                        # a record, string or other value: whole
    if v is None or isinstance(v, _LEAVES):
        return to_plain(v, (), leaf)
    out: dict[str, Any] = {}
    for name in sorted({t[0] for t in tails}):
        ok, x = _step(v, name)
        if ok:
            out[name] = _project(x, [t[1:] for t in tails if t[0] == name], leaf)
    return out


def _project_keys(v: Mapping[str, Any], tails: list[tuple[str, ...]], leaf: Leaf) -> dict[Any, Any]:
    """A map whose keys are read (a comprehension's variable is a key; size() counts them): every key,
    and only the values read by field name (the others are null placeholders, never converted)."""
    named = {t[0] for t in tails if t[0] not in (_ELEM, _SIZE)}
    out: dict[Any, Any] = {}
    for k, x in v.items():
        pk = to_plain(k)
        if not isinstance(pk, (str, int, bool)):
            raise CelError(f"unsupported map key type {type(pk).__name__}")
        out[pk] = _project(x, [t[1:] for t in tails if t[0] == k], leaf) if k in named else None
    return out


def project(env: Mapping[str, Any], refs: frozenset[tuple[str, ...]], leaf: Leaf = _same) -> dict[str, Any]:
    """Plain-data env holding just the referenced paths (absent fields stay absent)."""
    out: dict[str, Any] = {}
    for root in sorted({r[0] for r in refs}):
        if root in env:
            out[root] = _project(env[root], [r[1:] for r in refs if r[0] == root], leaf)
    return out


_TIME_CONV = frozenset({"timestamp", "duration"})


class FrontEnd:
    """The parsed rule, shared by every engine backend: syntax, references, rewrite."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.ast = subset.parse(source)     # also checks inv.* shapes
        self.refs = frozenset(subset.references(self.ast))
        self.reads = frozenset(subset.reads(self.ast))      # what projection converts (inside lists too)
        self.names = frozenset(v for k, v in subset.tokenize(source) if k == "id")   # rule-text identifiers
        self._probe = subset.SubsetProgram(source, self.ast)

    def text_for(self, native: tuple[str, ...], rename: Mapping[str, str],
                 fin: Mapping[str, str] | None = None) -> str:
        """Source to hand the engine: original text unless a non-native extension needs a rewrite,
        or (with fin) the rule has arithmetic or double() whose results must be checked finite."""
        rest = tuple(f for f in INV_FUNCS if f not in native)
        if fin is not None and (needs_finite(self.ast) or conv_calls(self.ast, frozenset(fin) & _TIME_CONV)
                                or ("zone" in fin and zone_calls(self.ast))):
            return unparse(mark_finite(self.ast), rename, fin, self.names)
        if not uses_inv(self.ast, rest):
            return self.source
        return unparse(self.ast, rename)

    def negated_membership(self) -> tuple[tuple[str, ...], list[Any]] | None:
        """See SubsetProgram.negated_membership."""
        return self._probe.negated_membership()


__all__ = ["FrontEnd", "project", "to_plain", "unparse", "uses_inv", "ext_sum", "ext_zip", "ext_unique",
           "INV_FUNCS", "ENGINE_FUNCTIONS", "redact", "engine_error", "mark_finite", "needs_finite", "conv_calls"]
