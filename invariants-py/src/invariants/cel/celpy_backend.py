"""Backend for cel-python (import celpy), a pure-Python CEL interpreter.

Uses the shared front end in _engine (syntax, references, extension rewrite). The inv.*
extensions are rewritten to __inv_sum, __inv_zip and __inv_unique, since celpy resolves a
dotted call as a field of `inv`. celpy 0.5 predates CEL's heterogeneous equality, so ==, !=,
`in` and the ordering operators are overridden to the current spec: numbers compare across
int and double, and values of different types are unequal rather than an error.

Cost limit: every parse-tree node visited (macro bodies included) spends one unit of a
per-call budget, 100,000 by default (celpy's tree is about ten nodes per subset node, so this
is close to the subset's 10,000); when it runs out evaluation stops with CelError. Unlike the subset, a spent budget
is not absorbed by || or &&.

Values: celpy 0.5's arithmetic returns plain Python float, timedelta and datetime; the adapter
wraps them back into celtypes, so type(), string() and the duration and timestamp methods see
the same value a literal gives. timestamp(), duration(), string() of a timestamp or duration,
int(timestamp) and the getters run on the shared rules in cel/values.py, as on the other backends.
"""
from __future__ import annotations

import datetime
from typing import Any, Callable

from . import values
from .errors import CelError, CelSyntaxError, NON_FINITE_RESULT, check_result
from ._engine import ENGINE_FUNCTIONS, FrontEnd, engine_error, ext_sum, ext_unique, ext_zip, project
from .subset import _GETTERS, _convert, _eq, ieee_div_zero, lookup, map_key

DEFAULT_BUDGET = 100_000


class _CostExceeded(Exception):
    """Raised inside celpy's evaluator when the budget is spent; never caught by celpy."""


def to_cel(v: Any, ct: Any) -> Any:
    """Plain Python data to celpy values."""
    if isinstance(v, bool):
        return ct.BoolType(v)
    if isinstance(v, bytes):
        return ct.BytesType(v)
    if isinstance(v, dict):
        return ct.MapType({to_cel(k, ct): to_cel(x, ct) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return ct.ListType([to_cel(x, ct) for x in v])
    if isinstance(v, int):
        return ct.IntType(v)
    if isinstance(v, float):
        return ct.DoubleType(v)
    if isinstance(v, str):
        return ct.StringType(v)
    if isinstance(v, datetime.datetime):
        return ct.TimestampType(v)
    if isinstance(v, datetime.timedelta):
        return ct.DurationType(v)
    return v


def from_cel(v: Any, ct: Any) -> Any:
    """celpy values to plain Python data."""
    if isinstance(v, ct.BoolType):
        return bool(v)
    if isinstance(v, dict):
        return {from_cel(k, ct): from_cel(x, ct) for k, x in v.items()}
    if isinstance(v, list):
        return [from_cel(x, ct) for x in v]
    for t in (int, float, str, bytes):
        if isinstance(v, t):
            return t(v)  # type: ignore[arg-type]
    # celpy arithmetic returns a plain timedelta or datetime where a literal is a DurationType or
    # TimestampType: one plain type each, so equality does not see two different types
    if isinstance(v, datetime.timedelta):
        return datetime.timedelta(days=v.days, seconds=v.seconds, microseconds=v.microseconds)
    if isinstance(v, datetime.datetime):
        return datetime.datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond, v.tzinfo)
    return v


def _text(e: BaseException) -> str:
    try:
        return str(e.args[0] if e.args else e)
    except Exception:
        return type(e).__name__


class CelpyProgram:
    """A rule compiled by celpy."""

    def __init__(self, front: FrontEnd, runner: Any, backend: "CelpyBackend") -> None:
        self.source, self._front, self._runner, self._b = front.source, front, runner, backend

    project = staticmethod(project)

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env; raises CelError."""
        return self.evaluate_data(project(env, self._front.reads))

    def evaluate_data(self, plain: dict[str, Any]) -> Any:
        """Evaluate against an env already projected to plain data; raises CelError."""
        ct, ev = self._b.ct, self._b.ev
        self._b.budget_box.value = [self._b.budget]
        try:
            data = {k: to_cel(v, ct) for k, v in plain.items()}
            r = self._runner.evaluate(data)
        except _CostExceeded:
            raise CelError("cost limit exceeded") from None
        except ev.CELEvalError as e:
            raise engine_error(_text(e), "celpy", self._front.names) from None
        except CelError:
            raise
        except (ValueError, OverflowError) as e:      # celtypes range checks (e.g. IntType(2**63))
            raise engine_error(f"{type(e).__name__}: {_text(e)}", "celpy", self._front.names) from None
        except Exception as e:
            raise engine_error(f"{type(e).__name__}: {_text(e)}", "celpy", self._front.names) from None
        finally:
            self._b.budget_box.value = None
        if isinstance(r, ev.CELEvalError):
            raise engine_error(_text(r), "celpy", self._front.names)
        try:
            v = from_cel(r, ct)
        except Exception as e:
            raise engine_error(f"{type(e).__name__}: {_text(e)}", "celpy", self._front.names) from None
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


class CelpyBackend:
    """Backend for cel-python, with a per-call cost budget (tree nodes visited)."""

    name = "celpy"
    functions = ENGINE_FUNCTIONS        # the loader refuses a call to any other function name
    cost_limit = True
    # the conversions are renamed too (a call only: `type(x) == int` still names celpy's int type)
    _RENAME = {"inv.sum": "__inv_sum", "inv.zip": "__inv_zip", "inv.unique": "__inv_unique",
               "int": "__inv_int", "double": "__inv_double", "bool": "__inv_bool", "string": "__inv_string",
               "timestamp": "__inv_timestamp", "duration": "__inv_duration"}

    def __init__(self, budget: int = DEFAULT_BUDGET) -> None:
        import threading

        import celpy
        from celpy import celtypes, evaluation
        self.budget, self.ct, self.ev = budget, celtypes, evaluation
        self.budget_box = threading.local()
        self._env = celpy.Environment(runner_class=self._runner_class())
        self._functions = self._make_functions()

    def _runner_class(self) -> type:
        import celpy
        ev, box, ct = self.ev, self.budget_box, self.ct

        class Counting(ev.Evaluator):  # type: ignore[name-defined]
            def _visit_tree(self, tree: Any) -> Any:
                b = getattr(box, "value", None)
                if b is not None:
                    b[0] -= 1
                    if b[0] < 0:
                        raise _CostExceeded()
                return super()._visit_tree(tree)

            def sub_evaluator(self, ast: Any) -> Any:
                return Counting(ast, activation=self.activation)

            def mapinits(self, tree: Any) -> Any:
                """A map literal with CEL's key rules (int, uint, bool or string; never double or null),
                as on the subset; celpy 0.5 takes any hashable key."""
                kv = self.visit_children(tree)
                for key in kv[0::2]:
                    if isinstance(key, ev.CELEvalError):
                        return key
                    try:
                        map_key(from_cel(key, ct))
                    except CelError as e:
                        return ev.CELEvalError(str(e), TypeError, None)
                return super().mapinits(tree)

        class Runner(celpy.InterpretedRunner):
            def evaluate(self, context: Any) -> Any:
                return Counting(ast=self.ast, activation=self.new_activation()).evaluate(context)
        return Runner

    def _make_functions(self) -> dict[str, Callable[..., Any]]:
        ct, ev = self.ct, self.ev

        def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
            def impl(*args: Any) -> Any:
                for a in args:
                    if isinstance(a, ev.CELEvalError):
                        return a
                try:
                    return to_cel(fn(*[from_cel(a, ct) for a in args]), ct)
                except CelError as e:
                    return ev.CELEvalError(str(e), ValueError, None)
            return impl

        def num(v: Any) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def order(op: str, base: Callable[..., Any]) -> Callable[..., Any]:
            def impl(a: Any, b: Any) -> Any:
                pa, pb = from_cel(a, ct), from_cel(b, ct)
                if num(pa) and num(pb) and type(pa) is not type(pb):
                    return ct.BoolType({"<": pa < pb, "<=": pa <= pb, ">": pa > pb, ">=": pa >= pb}[op])
                return base(a, b)
            return impl

        def contains(a: Any, b: Any) -> Any:
            pa, pb = from_cel(a, ct), from_cel(b, ct)
            if isinstance(pb, list):
                return ct.BoolType(any(_eq(pa, x) for x in pb))
            if isinstance(pb, dict):
                return ct.BoolType(any(_eq(pa, k) for k in pb))
            return ev.CELEvalError(f"no such overload: in {type(pb).__name__}", TypeError, None)

        def index(m: Any, i: Any) -> Any:
            """m[i] by CEL's rules, as on the subset: a map key matches across int and double
            ({1: 'a'}[1.0]) but never a bool; a list index is an int or an integral double."""
            pm, pi = from_cel(m, ct), from_cel(i, ct)
            if isinstance(pm, dict):
                if lookup(pm, pi)[0]:
                    if type(pi) is str:
                        return m[i]
                    for key in m:           # the stored celpy value, not its plain copy
                        if lookup({from_cel(key, ct): None}, pi)[0]:
                            return m[key]
                return ev.CELEvalError("no such key", KeyError, None)
            if isinstance(pm, list):
                if type(pi) is float and pi.is_integer():
                    pi = int(pi)
                if type(pi) is not int:
                    return ev.CELEvalError(f"no such overload: list[{type(pi).__name__}]", TypeError, None)
                if not 0 <= pi < len(pm):
                    return ev.CELEvalError("index out of range", IndexError, None)
                return m[pi]
            return ev.CELEvalError(f"no such overload: {type(pm).__name__}[{type(pi).__name__}]", TypeError, None)

        def size(v: Any) -> Any:
            pv = from_cel(v, ct)                 # celpy 0.5 gives size(null) == 0; CEL has no such overload
            if isinstance(pv, (str, bytes, list, dict)):
                return ct.IntType(len(pv))
            return ev.CELEvalError(f"no such overload: size({type(pv).__name__})", TypeError, None)

        def finite(r: Any) -> Any:
            """A double NaN or +-Infinity made by arithmetic or double() is an error, as on the subset."""
            if isinstance(r, float):            # DoubleType * DoubleType is a plain float in celpy 0.5
                try:
                    check_result(float(r))
                except CelError:
                    return ev.CELEvalError(NON_FINITE_RESULT, ValueError, None)
            return r

        def rewrap(r: Any) -> Any:
            """celpy 0.5 arithmetic gives plain float, int, timedelta and datetime: back to celtypes."""
            if isinstance(r, ev.CELEvalError) or type(r) in (ct.DoubleType, ct.IntType, ct.DurationType,
                                                                  ct.TimestampType):
                return r
            if isinstance(r, float):
                return ct.DoubleType(r)
            if isinstance(r, datetime.datetime):
                return ct.TimestampType(values.timestamp(r))
            if isinstance(r, datetime.timedelta):
                return ct.DurationType(values.duration(r))
            if isinstance(r, int) and not isinstance(r, bool):
                return ct.IntType(r)
            return r

        def numkind(v: Any) -> str:
            return "double" if isinstance(v, float) else "uint" if isinstance(v, ct.UintType) else "int"

        time_types = (ct.DurationType, ct.TimestampType, datetime.timedelta, datetime.datetime)

        def no_bool(name: str) -> Callable[..., Any]:
            base = bf[name]

            def impl(*args: Any) -> Any:            # celpy's BoolType is an int: true + 1 == 2
                if any(isinstance(a, ct.BoolType) for a in args) or (    # CEL has no -duration, duration * n
                        name in ("_*_", "_/_", "_%_", "-_") and any(isinstance(a, time_types) for a in args)):
                    kinds = ", ".join(type(from_cel(a, ct)).__name__ for a in args)
                    return ev.CELEvalError(f"no such overload: {name}({kinds})", TypeError, None)
                if len(args) == 2 and name != "-_" and all(isinstance(a, (int, float)) and not isinstance(a, bool)
                                                           for a in args):
                    ka, kb = (numkind(a) for a in args)    # CEL: no int-double (or uint) mix, no double %
                    if ka != kb or (name == "_%_" and ka == "double"):
                        return ev.CELEvalError(f"no such overload: {name}({ka}, {kb})", TypeError, None)
                if (name == "_/_" and len(args) == 2 and isinstance(args[0], float) and isinstance(args[1], float)
                        and args[1] == 0.0):        # celpy 0.5 gives +Infinity for any x / 0.0, 0.0 / 0.0 included
                    return finite(ct.DoubleType(ieee_div_zero(float(args[0]), float(args[1]))))
                try:
                    return finite(rewrap(base(*args)))
                except CelError as e:               # a timestamp or duration out of CEL's range
                    return ev.CELEvalError(str(e), ValueError, None)
            return impl


        def conv(name: str) -> Callable[..., Any]:
            """int(), double(), bool(), string(), timestamp() and duration() with the subset's (CEL's) rules
            where celpy 0.5 is looser or differs: text (Unicode digits, '1_0', '0x10'), int(double) at
            -2^63, bool(int), string(bool), timestamp and duration text, string(duration) ('0:00:01' in
            celpy), string(timestamp) (kept its offset), int(timestamp) (rounded toward zero)."""
            base = bf.get(name)

            def impl(*args: Any) -> Any:
                a = args[0] if len(args) == 1 else None
                if len(args) == 1 and a is None:      # celpy 0.5: string(null) == "None"
                    return ev.CELEvalError(f"no such overload: {name}(null)", TypeError, None)
                ours = isinstance(a, (ct.StringType, ct.BoolType)) or (name == "int" and isinstance(a, float)) \
                    or name in ("timestamp", "duration") or isinstance(a, time_types)
                if name == "bool" and not ours:
                    kind = type(from_cel(a, ct)).__name__ if len(args) == 1 else "?"
                    return ev.CELEvalError(f"no such overload: bool({kind})", TypeError, None)
                if ours:
                    try:
                        return to_cel(_convert(name, from_cel(a, ct)), ct)
                    except CelError as e:
                        return ev.CELEvalError(str(e), ValueError, None)
                if base is None:
                    return ev.CELEvalError(f"no such overload: {name}()", TypeError, None)
                return finite(base(*args))
            return impl

        def get(name: str) -> Callable[..., Any]:
            return guarded(lambda v, tz=None: values.getter(name, v, tz))

        bf = ev.base_functions
        return {
            **{g: guarded_err(get(g), ev) for g in _GETTERS},
            "__inv_sum": guarded(ext_sum), "__inv_zip": guarded(ext_zip), "__inv_unique": guarded(ext_unique),
            "_==_": guarded(_eq), "_!=_": guarded(lambda a, b: not _eq(a, b)), "_in_": guarded_err(contains, ev),
            "size": guarded_err(size, ev), "_[_]": guarded_err(index, ev),
            **{f"__inv_{n}": guarded_err(conv(n), ev) for n in ("int", "double", "bool", "string", "timestamp",
                                                                "duration")},
            **{n: guarded_err(no_bool(n), ev) for n in ("_+_", "_-_", "_*_", "_/_", "_%_", "-_")},
            **{f"_{op}_": guarded_err(order(op, bf[f"_{op}_"]), ev) for op in ("<", "<=", ">", ">=")},
        }

    def compile(self, expr: str) -> CelpyProgram:
        """Parse and compile expr; raises CelSyntaxError."""
        front = FrontEnd(expr)
        text = front.text_for((), self._RENAME)
        try:
            ast = self._env.compile(text)
            runner = self._env.program(ast, functions=self._functions)
        except Exception as e:
            raise CelSyntaxError(f"celpy: {e}") from e
        return CelpyProgram(front, runner, self)


def guarded_err(fn: Callable[..., Any], ev: Any) -> Callable[..., Any]:
    """Pass an error argument straight through instead of calling fn."""
    def impl(*args: Any) -> Any:
        for a in args:
            if isinstance(a, ev.CELEvalError):
                return a
        return fn(*args)
    return impl


__all__ = ["CelpyBackend", "CelpyProgram", "to_cel", "from_cel"]
