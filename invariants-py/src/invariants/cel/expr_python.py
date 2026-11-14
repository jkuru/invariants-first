"""Backend for cel-expr-python (the CEL C++ runtime).

The shared front end (cel/_engine.py) parses the rule; the engine evaluates it. inv.sum and
inv.zip are native engine functions; inv.unique_by is rewritten to inv.__unique(xs.map(v, e)).

Cost: cel-expr-python has no settable cost limit, but the C++ runtime enforces a fixed
comprehension iteration budget on every evaluation: one clause may run at most
ITERATION_LIMIT - 1 (9,999) comprehension iterations in total, summed over every all/exists/
exists_one/map/filter in it, nested ones included (`xs.exists(x, ys.exists(y, ...))` costs
|xs| * |ys|). Past that the clause is a rule_error "cost limit exceeded". In a clause with a
comprehension each value check (see _templates) is a one-element map() and costs one iteration.
Other work (string functions, matches() on RE2, list equality, size()) is not counted: each call
is linear in its operands (contains/startsWith/endsWith run in Python, cel-cpp's contains() is
quadratic), every string operand of those functions is under the string-size limit
(values.sized), and a clause can make one call per iteration, which the caller bounds.

Timestamps and durations: cel-expr-python converts a Python datetime or timedelta only where a
variable is declared with that type, so the adapter hands them to the engine as the protobuf
well-known types google.protobuf.Timestamp and Duration, which CEL reads as timestamp and
duration values anywhere in the data. That needs the protobuf package (in the cel extra).

NUL characters: cel-cpp reads a Python string or bytes value as C text, which ends at the first
NUL, so a value holding one would be cut short ('ab\\x00cd' == 'ab'). Such a value is refused: a
clause that reads one is a rule_error on cel-expr (the subset reads it whole). A string holding
a surrogate code point is a rule_error on every backend (values.text): cel-cpp crashes on one.

Nesting: cel-cpp refuses an expression nested more than RECURSION_LIMIT (32) levels deep at
compile time. The non-finite check adds one level per arithmetic tree, and a long +/- chain is
printed in chunks (cel.bind, or one-element map()s in a clause with a comprehension: see
_engine.unparse), so a 200-term sum loads; a rule nested deeper than the limit anyway is a
LoadError that names the limit.
"""
from __future__ import annotations

import datetime
import functools
import re
import threading
from typing import Any, Callable

from ._engine import (ENGINE_FUNCTIONS, FrontEnd, engine_error, ext_sum, ext_unique, ext_zip,
                      has_comprehension, project, uses_inv)
from .subset.evaluator import evaluate_short
from ._engine import _map_kids as _engine_map_kids
from . import _regex, subset, values
from .errors import CelError, CelSyntaxError, check_result

RECURSION_LIMIT = 32               # cel-cpp's expression nesting limit (fixed, not settable from Python)
NO_PROTOBUF = ("cel-expr needs the protobuf package to read datetime and timedelta values "
               "(pip install 'invariants-first[cel]' or pip install protobuf)")


# cel-cpp holds a duration as int64 nanoseconds: one past that (about +-292 years; CEL allows
# +-10,000) is read saturated, so `d + duration('0s')` would not equal d. Such a duration is refused on
# cel-expr: in the data, from duration(text) and as a literal, a rule_error at run time (as on the subset).
MAX_DURATION_US = (2**63 - 1) // 1000
LONG_DURATION = ("duration out of range (cel-expr holds durations as int64 nanoseconds, about +-292 years; "
                 "the subset reads up to +-10,000 years)")


def protobuf_available() -> bool:
    """True if google.protobuf (needed for timestamp and duration values on cel-expr) imports."""
    try:
        _pb_types()
    except ImportError:
        return False
    return True


@functools.lru_cache(maxsize=1)
def _pb_types() -> tuple[Any, Any]:
    from google.protobuf import duration_pb2, timestamp_pb2
    return timestamp_pb2.Timestamp, duration_pb2.Duration


def to_engine(v: Any) -> Any:
    """A UTC datetime or a timedelta (from _engine.to_plain) as the protobuf type cel-expr reads."""
    try:
        ts, du = _pb_types()
    except ImportError:
        raise CelError(NO_PROTOBUF) from None
    if isinstance(v, datetime.datetime):
        d = v - values.EPOCH
        return ts(seconds=d.days * 86400 + d.seconds, nanos=d.microseconds * 1000)
    if isinstance(v, datetime.timedelta):
        us = v // datetime.timedelta(microseconds=1)
        if abs(us) > MAX_DURATION_US:
            raise CelError(LONG_DURATION)
        sec = abs(us) // 10**6
        sign = -1 if us < 0 else 1
        return du(seconds=sign * sec, nanos=sign * (abs(us) % 10**6) * 1000)
    if isinstance(v, list):
        return [to_engine(x) for x in v]
    return v


def project_engine(env: Any, refs: frozenset[tuple[str, ...]]) -> dict[str, Any]:
    """_engine.project, with timestamps and durations as protobuf values."""
    return project(env, refs, to_engine)


# ---- the cel-expr-python backend ----------------------------------------------------------

def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    def impl(*args: Any) -> Any:
        try:
            return to_engine(fn(*args))
        except CelError as e:            # the engine turns a raised exception into an error value; the
            raise ValueError(f"{e} ({e.detail})" if e.detail else str(e)) from None   # detail is redacted
    return impl


def _double_text(s: str) -> float:
    """double(string) with the subset's rules: decimal text only, a non-finite result is an error."""
    return float(subset._convert("double", s))


# cel-cpp evaluates cel.bind lazily: the bound value is computed at the first read of its variable,
# unless the variable's comprehension slot already holds a value. A comprehension that stops early
# (exists() at its first true element, all() at its first false one) leaves its loop variable in its
# slot, and a later cel.bind in that slot then reads the stale value instead of its own:
# `xs.exists(q, q > 50) && cel.bind(v, a + b, v) > 100` compared an element of xs with 100. So the
# checks below use cel.bind only in a clause without comprehensions (there is no stale slot then);
# in a clause with one they evaluate the value once as the one element of a map() instead,
# `[x].map(v, check(v))[0]`, an ordinary comprehension whose variable is always set from its list.
# The backend proves this when it starts (_check_slots); if it fails, a clause with a comprehension
# and a check is refused at load (fail closed).
SLOT_CANARIES = (       # each is true; a stale read makes it false (or an error)
    "[60, 1].exists(q, q > 50) && args.a + 1 == 8",
    "[[1, 2].exists(q, [3, 4].exists(r, r > 0))].exists(z, z) && [1].all(k, double(args.a) == 7.0)",
    "[1, 2].exists(q, [3, 4].exists(r, [5, 6].exists(s, s > 0))) && [0].all(k, [0].all(j, args.a * 2 == 14))",
    "[1, 2].exists(q, q > 0) && " + " + ".join(["args.a"] * 30) + " == 210",
    "[1, 2].all(q, q > 5) || duration(args.d) + duration('1s') == duration('3s')",
    "[1, 2].all(q, [1, 2].exists(r, r > 0) && q > 5) || timestamp(args.t) + duration(args.d) == "
    "timestamp('1970-01-01T00:00:03Z')",
    "{'a': 1, 'b': 2}.exists(k, true) && double(args.a * 2) + double(args.a) == 21.0",
)
SLOT_DATA = {"a": 7, "d": "2s", "t": "1970-01-01T00:00:01Z"}
STALE_SLOT = ("cel-expr: this cel-expr-python build can read a stale comprehension value in the checks the "
              "backend adds around arithmetic, double(), timestamp() and duration() in a clause that also "
              "has a comprehension (all, exists, exists_one, map, filter, inv.unique_by), so such a clause is "
              "refused on it; use the subset backend (INV_CEL_BACKEND=subset) or split the clause")


def _templates(bind: bool, uints: bool = False) -> dict[str, str]:
    """The text the backend prints around each value _engine.mark_finite marks ({0}: the value's text),
    as cel.bind(var, value, body) (bind) or as [value].map(var, body)[0]. uints: the clause has uint()
    or a uint literal, so a checked value may be a uint (data never holds one)."""
    def let(var: str, value: str, body: str) -> str:
        return f"cel.bind({var}, {value}, {body})" if bind else f"[{value}].map({var}, {body})[0]"

    def check(var: str, other: str, ints: bool = True) -> str:
        # a non-finite double, or an int or uint past 2^53 (EXACT_INT): inv.__fin decides in Python
        test = f"(type({var}) == double && {var} - {var} != 0.0)"
        if ints:
            big = f"(type({var}) == int && ({var} > {EXACT_INT} || {var} < -{EXACT_INT}))"
            if uints:
                big = f"{big} || (type({var}) == uint && {var} > {EXACT_INT}u)"
            test = f"({test} || {big})"
        return f"{test} ? inv.__fin({var}) : {other}"
    fin = let("__inv_v", "{0}", check("__inv_v", "__inv_v"))
    if bind:
        dbl = let("__inv_d", "{0}", "type(__inv_d) == string ? inv.__double(__inv_d) : "
                  + fin.format("double(__inv_d)"))
    else:       # double() of a double is that double: checking the argument is checking the result
        dbl = let("__inv_d", "{0}", "type(__inv_d) == string ? inv.__double(__inv_d) : ("
                  + check("__inv_d", "double(__inv_d)", ints=False) + ")")
    out = {"fin": fin, "double": dbl, "getter": "inv.__{name}({0}, {1})",
           "chunk": "", "text": "inv.__text({0})", "pattern": "inv.__pattern({0})",
           **{f: f"inv.__{f}({{0}}, {{1}})" for f in ("contains", "startsWith", "endsWith")}}
    for conv in ("timestamp", "duration"):
        out[conv] = let("__inv_t", "{0}", f"type(__inv_t) == string ? inv.__{conv}(__inv_t) : {conv}(__inv_t)")
    if not bind:
        out["let"] = "[{value}].map({var}, {body})[0]"
    return out


ITERATION_LIMIT = 10_000          # cel-cpp's comprehension iteration budget per evaluation (fixed)
_BUDGET_TEXT = "Iteration budget exceeded"


# ---- int and double compared exactly --------------------------------------------------------
# cel-cpp compares an int (or uint) with a double by first rounding it to a double, so past 2^53 it
# can be wrong: 2^53 + 1 > 2.0^53 is false there, true on the subset (Python compares exactly). Every
# int up to 2^53 in magnitude is exactly a double, so such a comparison can be wrong only when an int
# past 2^53 and a double are both in play. A clause may see a double only if its text has a double
# literal or a double() call, or the data it reads has a float; it may see an int past 2^53 only
# from its text (a literal), the data it reads, an arithmetic result, an int() or uint() result or
# inv.sum() -- the values the backend checks anyway (_templates). Each clause decides for itself,
# from the values it reads (ExprProgram._plan), never another clause's.
#
# When both can meet, the clause is still evaluated by the engine first: its comprehension budget
# (ITERATION_LIMIT) decides, so a clause past it is the same "cost limit exceeded" either way. If the
# engine stays within it and an int past 2^53 was in play (in the data or text the clause reads, or
# met by a check), the clause is then evaluated exactly by the subset, on the same data: with every
# function cel-expr has (uint(), bytes(), type() included), && || all exists short-circuiting as in
# cel-cpp, and a budget that covers ITERATION_LIMIT iterations of the clause (_exact_budget), so the
# exact evaluation answers wherever the engine did.
EXACT_INT = 2**53
_FLOAT, _BIG = 1, 2


class _Exact(threading.local):
    on = False          # this evaluation may compare an int with a double
    fired = False       # ... and a check met an int past 2^53


_EXACT = _Exact()


def _big_int(v: int) -> int:
    """inv.__fin(int or uint), called only for one past 2^53: noted, if a double may be in play (the
    clause is then evaluated exactly), and returned unchanged, so the engine's run (and its
    comprehension count) is the same as without the check."""
    if _EXACT.on:
        _EXACT.fired = True
    return v


def _exact_budget(ast: Any) -> int:
    """The subset budget of a clause's exact evaluation: one unit per node for each of ITERATION_LIMIT
    iterations and the clause itself, at least the subset's own (the engine has already kept the
    clause within ITERATION_LIMIT iterations)."""
    size, todo = 0, [ast]
    while todo:
        n = todo.pop()
        size += 1
        todo.extend(subset.kids(n))
    return max(subset.DEFAULT_BUDGET, (ITERATION_LIMIT + 1) * (size + 1))


def _sum(xs: Any) -> Any:
    total = ext_sum(xs)
    return _big_int(total) if type(total) is int and not -EXACT_INT <= total <= EXACT_INT else total


def _static_flags(n: Any) -> int:
    """_FLOAT if the rule text has a double literal or a double() call, | _BIG for an int (or uint)
    literal past 2^53."""
    f = 0
    if n[0] == "lit":
        if type(n[1]) is float:
            f = _FLOAT
        elif type(n[1]) is int and not -EXACT_INT <= n[1] <= EXACT_INT:
            f = _BIG
    elif n[0] == "call" and n[1] == "double":
        f = _FLOAT
    for c in subset.kids(n):
        f |= _static_flags(c)
    return f


def _long_duration_literal(n: Any) -> bool:
    """True if n is duration('<text>') past cel-cpp's int64-nanosecond range."""
    if n[0] == "call" and n[1] == "duration" and n[2] is None and len(n[3]) == 1 and n[3][0][0] == "lit" \
            and isinstance(n[3][0][1], str):
        try:
            d = values.parse_duration(n[3][0][1])
        except CelError:
            return False                # not a duration: the type checker or the engine says so
        return abs(d // datetime.timedelta(microseconds=1)) > MAX_DURATION_US
    return False


def _long_durations_at_run_time(n: Any) -> Any:
    """The tree with each duration literal past cel-cpp's range read through dyn(), so the backend
    converts it in Python (values.parse_duration, then to_engine): a rule_error when evaluated, as
    on the subset, which refuses a duration past CEL's own range at run time too."""
    if _long_duration_literal(n):
        return ("call", "duration", None, [("call", "dyn", None, [n[3][0]])])
    return _engine_map_kids(n, _long_durations_at_run_time)


def from_engine(v: Any) -> Any:
    """Projected engine data back as the subset reads it (protobuf Timestamp and Duration as datetime
    and timedelta)."""
    t = type(v)
    if t is dict:
        return {k: from_engine(x) for k, x in v.items()}
    if t is list:
        return [from_engine(x) for x in v]
    if v is None or t in (str, int, float, bool, bytes):
        return v
    ts, du = _pb_types()            # a value of another type here is a protobuf value: protobuf imports
    if isinstance(v, ts):
        return values.EPOCH + datetime.timedelta(seconds=v.seconds, microseconds=v.nanos // 1000)
    if isinstance(v, du):
        return datetime.timedelta(seconds=v.seconds, microseconds=int(v.nanos / 1000))
    return v


class ExprProgram:
    """A rule compiled by cel-expr-python."""

    def __init__(self, front: FrontEnd, expr: Any, cel: Any) -> None:
        self.source, self._front, self._cel = front.source, front, cel
        self._run = expr.eval       # the engine's Expression.eval, not Python's builtin eval
        self._static = _static_flags(front.ast)
        self._plan = scan_plan(front.reads)     # what this clause reads of the (shared) data
        self._exact_prog = subset.SubsetProgram(front.source, front.exact.ast, _exact_budget(front.exact.ast))

    project = staticmethod(project_engine)

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env; raises CelError."""
        return self.evaluate_data(project_engine(env, self._front.reads))

    def evaluate_data(self, data: dict[str, Any]) -> Any:
        """Evaluate against an env already projected to plain data (this clause's projection, or one
        shared by the rule's clauses: only what this clause reads is checked); raises CelError."""
        flags = _scan_read(data, self._plan) | self._static
        if not flags & _FLOAT:              # no double in play: every int compares exactly
            try:                            # (self._eval, inlined: the common path)
                r = self._run(data=data)
                if r.type() == self._cel.Type.ERROR:
                    raise engine_error(_cost(str(r.value())), names=self._front.names)
                return r.plain_value()
            except CelError:
                raise
            except Exception as e:
                raise _raised(e, self._front.names) from None
        prev = (_EXACT.on, _EXACT.fired)
        _EXACT.on, _EXACT.fired = True, False
        try:
            v = self._eval(data)
            if not (_EXACT.fired or flags & _BIG):
                return v
        except CelError as e:
            if not (_EXACT.fired or flags & _BIG) or _COST_ERROR.match(str(e)):
                raise                       # the engine's budget decides, exact or not
        finally:
            _EXACT.on, _EXACT.fired = prev
        return self._exact(data)            # an int past 2^53 may have met a double (see EXACT_INT)

    def _eval(self, data: dict[str, Any]) -> Any:
        try:
            r = self._run(data=data)
            if r.type() == self._cel.Type.ERROR:
                raise engine_error(_cost(str(r.value())), names=self._front.names)
            v = r.plain_value()
        except CelError:
            raise
        except Exception as e:
            raise _raised(e, self._front.names) from None
        return v

    def _exact(self, data: dict[str, Any]) -> Any:
        """The clause evaluated by the subset (exact int and double comparison), on the same data, with
        cel-expr's type names, short-circuiting as cel-cpp and the budget of _exact_budget."""
        env = {**subset.TYPE_NAMES, **from_engine(data)}
        return evaluate_short(self._exact_prog, env)

    def references(self) -> frozenset[tuple[str, ...]]:
        """Free identifiers with their static field paths."""
        return self._front.refs

    def reads(self) -> frozenset[tuple[str, ...]]:
        """What evaluation converts, inside lists too (subset.reads): unread element fields never are."""
        return self._front.reads

    def negated_membership(self) -> tuple[tuple[str, ...], list[Any]] | None:
        """If the expression is `!(path in [literals])`, return (path, literals)."""
        return self._front.negated_membership()


NUL_TEXT = ("a string or bytes value holding a NUL character cannot be read on cel-expr (its C++ runtime "
            "would end the text there); use the subset backend, or strip NUL characters first")


def scan_plan(reads: frozenset[tuple[str, ...]]) -> dict[str, Any]:
    """What a clause reads of its data, as _scan_read walks it: {root: plan}, a plan None (the value
    whole) or {step: plan} (a field name, ELEM or SIZE), as _engine.project reads the same paths."""
    def plan(tails: list[tuple[str, ...]]) -> Any:
        if not tails or any(len(t) == 0 for t in tails):
            return None
        return {h: plan([t[1:] for t in tails if t[0] == h]) for h in sorted({t[0] for t in tails})}
    return {root: plan([r[1:] for r in reads if r[0] == root]) for root in sorted({r[0] for r in reads})}


def _scan_read(data: dict[str, Any], plan: dict[str, Any]) -> int:
    """_scan of only what a clause reads of data (a projection shared by the rule's clauses holds what
    the others read too): a value another clause reads never errors this one."""
    f = 0
    for root, p in plan.items():
        if root in data:
            f |= _scan(data[root]) if p is None else _scan_at(data[root], p)
    return f


_ELEM, _SIZE = subset.ELEM, subset.SIZE


def _scan_at(v: Any, plan: dict[str, Any]) -> int:
    """_scan of the parts of v that plan reads (as _engine._project projects them)."""
    t = type(v)
    if t is list:
        if plan.keys() <= {_ELEM, _SIZE}:
            if _ELEM not in plan:
                return 0                    # only the length is read
            sub, f = plan[_ELEM], 0
            if sub is None:
                for x in v:
                    if type(x) is not int or not -EXACT_INT <= x <= EXACT_INT:     # (an int is the usual value)
                        f |= _scan(x)
                return f
            for x in v:
                f |= _scan_at(x, sub)
            return f
        return _scan(v)                     # a list read by field name: the engine reads it whole
    if t is dict:
        f = 0
        if _ELEM in plan or _SIZE in plan:  # the keys are read
            for k in v:
                if type(k) is not str or not k.isascii() or "\x00" in k:
                    f |= _scan(k)
        for name, sub in plan.items():
            if name not in (_ELEM, _SIZE) and name in v:
                x = v[name]
                if sub is not None:
                    f |= _scan_at(x, sub)
                elif type(x) is not int or not -EXACT_INT <= x <= EXACT_INT:
                    f |= _scan(x)
        return f
    return _scan(v)


def _scan(v: Any) -> int:
    """Check the projected data on its way to the engine, and say what numbers it holds (_FLOAT,
    _BIG). CelError for a string or bytes holding a NUL (cel-cpp's C++ runtime reads Python text as a
    C string, so 'ab\\x00cd' would reach the rule as 'ab', a map key the same) and for a string
    holding a surrogate code point (values.text: cel-cpp crashes converting one)."""
    t = type(v)
    if t is str:
        if "\x00" in v:
            raise CelError(NUL_TEXT)
        if not v.isascii():
            values.text(v)
        return 0
    if t is int:
        return 0 if -EXACT_INT <= v <= EXACT_INT else _BIG
    if t is float:
        return _FLOAT
    if t is bytes:
        if b"\x00" in v:
            raise CelError(NUL_TEXT)
        return 0
    f = 0
    if t is dict:
        for k, x in v.items():
            if type(k) is not str or not k.isascii() or "\x00" in k:
                f |= _scan(k)
            if type(x) is not int or not -EXACT_INT <= x <= EXACT_INT:     # (an int is the usual value)
                f |= _scan(x)
    elif t is list:
        for x in v:
            if type(x) is not int or not -EXACT_INT <= x <= EXACT_INT:
                f |= _scan(x)
    return f


_COST_ERROR = re.compile(r"(?:cel-expr: )?cost limit exceeded")      # the engine's budget, as _cost words it
_BUDGET = re.compile(r"(?:[A-Z][A-Z_]*: )*" + re.escape(_BUDGET_TEXT))


def _cost(msg: str) -> str:
    if _BUDGET.match(msg):                  # at the start: a key or value holding the text is not it
        return (f"cost limit exceeded (cel-expr: at most {ITERATION_LIMIT - 1:,} comprehension iterations per "
                f"clause, nested comprehensions multiply)")
    return msg


def _raised(e: Exception, names: frozenset[str]) -> CelError:
    """The CelError for an exception the engine raised: 'cel-expr: <kind>'; the cost message names
    cel-expr itself, so it is not prefixed again (fix round 30: 'cel-expr: cost limit exceeded
    (cel-expr: ...)')."""
    msg = str(e)
    cost = _cost(msg)
    return engine_error(cost, None if cost is not msg else "cel-expr", names)


# ---- the string functions, under the string-size limit (values.sized) ---------------------------
# cel-cpp's contains() is quadratic in its operands (2,000,000 characters: 17 s), so contains(),
# startsWith() and endsWith() run in Python (linear: str.find), and matches() runs on RE2 (linear)
# after its operands pass the limit, as on the subset.

def _contains(s: str, t: str) -> bool:
    return values.sized("contains", t) in values.sized("contains", s)


def _starts(s: str, t: str) -> bool:
    return values.sized("startsWith", s).startswith(values.sized("startsWith", t))


def _ends(s: str, t: str) -> bool:
    return values.sized("endsWith", s).endswith(values.sized("endsWith", t))


def _text(s: str) -> str:
    return values.sized("matches", s)


def _pattern(p: str) -> str:
    """A matches() pattern from the data: under the string-size limit and the pattern limit (as on the
    subset: _regex.pattern_size), before cel-cpp compiles it."""
    try:
        _regex.pattern_size(values.sized("matches", p))
    except _regex.PatternLimit as e:
        err = CelError(_regex.PATTERN_LIMIT)
        err.detail = str(e)
        raise err from None
    return p


class ExprPythonBackend:
    """Backend for cel-expr-python. inv.sum and inv.zip are native functions; inv.unique_by is
    rewritten to inv.__unique(list.map(v, body)). The cost limit is the engine's fixed
    comprehension iteration budget (ITERATION_LIMIT per clause), not settable from Python."""

    name = "cel-expr"
    functions = ENGINE_FUNCTIONS        # the loader refuses a call to any other function name
    cost_limit = False                  # no settable budget...
    iteration_limit = ITERATION_LIMIT   # ...but this one is always enforced, per clause evaluation
    _NATIVE = ("inv.sum", "inv.zip")
    _RENAME = {"inv.unique": "inv.__unique"}
    # Every +, -, *, /, double() and int() result is checked: a double NaN or +-Infinity made during
    # evaluation (0.0 / 0.0, 1e200 * 1e200, double("NaN")) is a rule_error, as on the subset, and an
    # int past 2^53 may send the clause to the exact evaluation (see EXACT_INT).
    # The operation is evaluated once (see _templates); the Python check runs only on such a value.
    # double(string) follows the subset's text rules (cel-cpp's strtod also takes hex, '0x10').
    # timestamp() and duration() of text that is not a literal parse with the subset's rules (a literal
    # is checked at load by the type checker); a +/- chain longer than _engine.CHAIN is chunked.
    # _FIN for a clause without comprehensions, _FIN_LOOP (no cel.bind) for one with them.
    _FIN = _templates(bind=True)
    _FIN_LOOP = _templates(bind=False)
    _FIN_U, _FIN_LOOP_U = _templates(bind=True, uints=True), _templates(bind=False, uints=True)

    def __init__(self) -> None:
        from cel_expr_python import cel
        self._cel = cel
        T = cel.Type
        dyn_list = T.List(T.DYN)

        def ovl(oid: str, ret: Any, params: list[Any], fn: Callable[..., Any]) -> Any:
            return cel.Overload(oid, ret, params, impl=_wrap(fn))

        def decl(name: str, oid: str, ret: Any, params: list[Any], fn: Callable[..., Any]) -> Any:
            return cel.FunctionDecl(name, [ovl(oid, ret, params, fn)])
        S = T.STRING
        self._env = cel.NewEnv(config=cel.NewEnvConfigFromYaml("extensions:\n  - name: bindings\n"), functions=[
            decl("inv.sum", "inv_sum_list", T.DYN, [dyn_list], _sum),
            decl("inv.zip", "inv_zip_list_list", T.List(dyn_list), [dyn_list, dyn_list], ext_zip),
            decl("inv.__unique", "inv_unique_list", T.BOOL, [dyn_list], ext_unique),
            cel.FunctionDecl("inv.__fin", [ovl("inv_fin_double", T.DOUBLE, [T.DOUBLE], check_result),
                                           ovl("inv_fin_int", T.INT, [T.INT], _big_int),
                                           ovl("inv_fin_uint", T.UINT, [T.UINT], _big_int)]),
            decl("inv.__double", "inv_double_string", T.DOUBLE, [S], _double_text),
            decl("inv.__timestamp", "inv_timestamp_string", T.TIMESTAMP, [S], values.parse_timestamp),
            decl("inv.__duration", "inv_duration_string", T.DURATION, [S], values.parse_duration),
            decl("inv.__contains", "inv_contains_string_string", T.BOOL, [S, S], _contains),
            decl("inv.__startsWith", "inv_starts_string_string", T.BOOL, [S, S], _starts),
            decl("inv.__endsWith", "inv_ends_string_string", T.BOOL, [S, S], _ends),
            decl("inv.__text", "inv_text_string", S, [S], _text),
            decl("inv.__pattern", "inv_pattern_string", S, [S], _pattern),
            *(decl(f"inv.__{g}", f"inv_{g}_timestamp_string", T.INT, [T.TIMESTAMP, S],
                   functools.partial(values.getter, g)) for g in subset._GETTERS),
        ])

        self._slots_ok = True
        self._slots_ok = self._check_slots()

    def _check_slots(self) -> bool:
        """True if every SLOT_CANARIES clause is true on this engine (no check reads a stale slot)."""
        time_ok = protobuf_available()       # (without protobuf a timestamp or duration is refused anyway)
        for src in SLOT_CANARIES:
            if not time_ok and ("duration(" in src or "timestamp(" in src):
                continue
            try:
                if self.compile(src).evaluate_data({"args": dict(SLOT_DATA)}) is not True:
                    return False
            except (CelError, CelSyntaxError):
                return False
        return True

    def compile(self, expr: str) -> ExprProgram:
        """Parse and compile expr; raises CelSyntaxError."""
        front = FrontEnd(expr)
        front.ast = _long_durations_at_run_time(front.ast)    # (front.exact keeps the rule's own tree)
        loop = has_comprehension(front.ast)
        uints = uses_inv(front.ast, ("uint",))
        fin = (self._FIN_LOOP_U if uints else self._FIN_LOOP) if loop else (self._FIN_U if uints else self._FIN)
        text = front.text_for(self._NATIVE, self._RENAME, fin)
        if loop and not self._slots_ok and ".map(__inv_" in text:
            raise CelSyntaxError(STALE_SLOT)
        try:
            compiled = self._env.compile(text, disable_check=True)
        except Exception as e:
            if "recursion limit" in str(e).lower():
                raise CelSyntaxError(
                    f"cel-expr: the clause is nested more than {RECURSION_LIMIT} levels deep, cel-cpp's fixed "
                    f"expression recursion limit (deeply nested parentheses, calls or &&/|| chains, or a "
                    f"very long sum); split it into several clauses or into smaller expressions") from None
            raise CelSyntaxError(f"cel-expr: {e}") from e
        return ExprProgram(front, compiled, self._cel)


__all__ = ["ExprPythonBackend", "ExprProgram", "RECURSION_LIMIT", "protobuf_available", "to_engine", "from_engine",
           "EXACT_INT"]
