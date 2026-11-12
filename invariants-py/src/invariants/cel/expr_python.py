"""Backend for cel-expr-python (the CEL C++ runtime).

The shared front end (cel/_engine.py) parses the rule; the engine evaluates it. inv.sum and
inv.zip are native engine functions; inv.unique_by is rewritten to inv.__unique(xs.map(v, e)).

Cost: cel-expr-python has no settable cost limit, but the C++ runtime enforces a fixed
comprehension iteration budget on every evaluation: one clause may run at most
ITERATION_LIMIT - 1 (9,999) comprehension iterations in total, summed over every all/exists/
exists_one/map/filter in it, nested ones included (`xs.exists(x, ys.exists(y, ...))` costs
|xs| * |ys|). Past that the clause is a rule_error "cost limit exceeded". Work outside
comprehensions (string functions, matches() on RE2, list equality, size()) is not counted; it
is linear in the size of the data the clause reads, which the caller bounds.

Timestamps and durations: cel-expr-python converts a Python datetime or timedelta only where a
variable is declared with that type, so the adapter hands them to the engine as the protobuf
well-known types google.protobuf.Timestamp and Duration, which CEL reads as timestamp and
duration values anywhere in the data. That needs the protobuf package (in the cel extra).

NUL characters: cel-cpp reads a Python string or bytes value as C text, which ends at the first
NUL, so a value holding one would be cut short ('ab\\x00cd' == 'ab'). Such a value is refused: a
clause that reads one is a rule_error on cel-expr (the subset and celpy read it whole).

Nesting: cel-cpp refuses an expression nested more than RECURSION_LIMIT (32) levels deep at
compile time. The non-finite check adds one level per arithmetic tree, and a long +/- chain is
printed in cel.bind chunks (see _engine.unparse), so a 200-term sum loads; a rule nested deeper
than the limit anyway is a LoadError that names the limit.
"""
from __future__ import annotations

import datetime
import functools
from typing import Any, Callable

from ._engine import ENGINE_FUNCTIONS, FrontEnd, engine_error, ext_sum, ext_unique, ext_zip, project
from . import subset, values
from .errors import CelError, CelSyntaxError, check_result

RECURSION_LIMIT = 32               # cel-cpp's expression nesting limit (fixed, not settable from Python)
NO_PROTOBUF = ("cel-expr needs the protobuf package to read datetime and timedelta values "
               "(pip install 'invariants-first[cel]' or pip install protobuf)")


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
        except CelError as e:
            raise ValueError(str(e)) from None   # the engine turns a raised exception into an error value
    return impl


def _double_text(s: str) -> float:
    """double(string) with the subset's rules: decimal text only, a non-finite result is an error."""
    return float(subset._convert("double", s))


ITERATION_LIMIT = 10_000          # cel-cpp's comprehension iteration budget per evaluation (fixed)
_BUDGET_TEXT = "Iteration budget exceeded"


class ExprProgram:
    """A rule compiled by cel-expr-python."""

    def __init__(self, front: FrontEnd, expr: Any, cel: Any) -> None:
        self.source, self._front, self._cel = front.source, front, cel
        self._run = expr.eval       # the engine's Expression.eval, not Python's builtin eval

    project = staticmethod(project_engine)

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env; raises CelError."""
        return self.evaluate_data(project_engine(env, self._front.reads))

    def evaluate_data(self, data: dict[str, Any]) -> Any:
        """Evaluate against an env already projected to plain data; raises CelError."""
        _no_nul(data)
        try:
            r = self._run(data=data)
            if r.type() == self._cel.Type.ERROR:
                raise engine_error(_cost(str(r.value())), names=self._front.names)
            v = r.plain_value()
        except CelError:
            raise
        except Exception as e:
            raise engine_error(_cost(str(e)), "cel-expr", self._front.names) from None
        return v

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
            "would end the text there); use the subset or celpy backend, or strip NUL characters first")


def _no_nul(v: Any) -> None:
    """CelError if a string or bytes in the projected data holds a NUL: cel-expr's C++ runtime reads
    Python text as a C string, so 'ab\\x00cd' would reach the rule as 'ab' (and a map key the same)."""
    t = type(v)
    if t is str:
        if "\x00" in v:
            raise CelError(NUL_TEXT)
    elif t is bytes:
        if b"\x00" in v:
            raise CelError(NUL_TEXT)
    elif t is dict:
        for k, x in v.items():
            _no_nul(k)
            _no_nul(x)
    elif t is list:
        for x in v:
            _no_nul(x)


def _cost(msg: str) -> str:
    if _BUDGET_TEXT in msg:
        return (f"cost limit exceeded (cel-expr: {ITERATION_LIMIT:,} comprehension iterations per clause, "
                f"nested comprehensions multiply)")
    return msg


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
    # Every +, -, *, / and double() result is checked: a double NaN or +-Infinity made during
    # evaluation (0.0 / 0.0, 1e200 * 1e200, double("NaN")) is a rule_error, as on the subset.
    # cel.bind evaluates the operation once; the Python check runs only on a non-finite double.
    # double(string) follows the subset's text rules (cel-cpp's strtod also takes hex, '0x10').
    _CHECK = "cel.bind(__inv_v, {0}, (type(__inv_v) == double && __inv_v - __inv_v != 0.0) ? inv.__fin(__inv_v) : __inv_v)"
    # timestamp() and duration() of text that is not a literal parse with the subset's rules (a literal
    # is checked at load by the type checker); a +/- chain longer than _engine.CHAIN is chunked.
    _FIN = {"fin": _CHECK,
            "double": "cel.bind(__inv_d, {0}, type(__inv_d) == string ? inv.__double(__inv_d) : "
                      + _CHECK.format("double(__inv_d)") + ")",
            "timestamp": "cel.bind(__inv_t, {0}, type(__inv_t) == string ? inv.__timestamp(__inv_t) : "
                         "timestamp(__inv_t))",
            "duration": "cel.bind(__inv_t, {0}, type(__inv_t) == string ? inv.__duration(__inv_t) : "
                        "duration(__inv_t))",
            "zone": "inv.__zone({0})",
            "chunk": ""}

    def __init__(self) -> None:
        from cel_expr_python import cel
        self._cel = cel
        T = cel.Type
        dyn_list = T.List(T.DYN)

        def decl(name: str, oid: str, ret: Any, params: list[Any], fn: Callable[..., Any]) -> Any:
            return cel.FunctionDecl(name, [cel.Overload(oid, ret, params, impl=_wrap(fn))])
        self._env = cel.NewEnv(config=cel.NewEnvConfigFromYaml("extensions:\n  - name: bindings\n"), functions=[
            decl("inv.sum", "inv_sum_list", T.DYN, [dyn_list], ext_sum),
            decl("inv.zip", "inv_zip_list_list", T.List(dyn_list), [dyn_list, dyn_list], ext_zip),
            decl("inv.__unique", "inv_unique_list", T.BOOL, [dyn_list], ext_unique),
            decl("inv.__fin", "inv_fin_double", T.DOUBLE, [T.DOUBLE], check_result),
            decl("inv.__double", "inv_double_string", T.DOUBLE, [T.STRING], _double_text),
            decl("inv.__timestamp", "inv_timestamp_string", T.TIMESTAMP, [T.STRING], values.parse_timestamp),
            decl("inv.__duration", "inv_duration_string", T.DURATION, [T.STRING], values.parse_duration),
            decl("inv.__zone", "inv_zone_string", T.STRING, [T.STRING], values.check_zone),
        ])

    def compile(self, expr: str) -> ExprProgram:
        """Parse and compile expr; raises CelSyntaxError."""
        front = FrontEnd(expr)
        text = front.text_for(self._NATIVE, self._RENAME, self._FIN)
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


__all__ = ["ExprPythonBackend", "ExprProgram", "RECURSION_LIMIT", "protobuf_available", "to_engine"]
