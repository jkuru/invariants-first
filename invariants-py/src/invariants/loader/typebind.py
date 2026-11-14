"""Type binding: the static types of the names an enforcer's clauses read, and the load-time type check."""
from __future__ import annotations

from typing import Any

from .. import cel
from .. import typecheck as tc
from ..errors import LoadError
from ..model import DEFAULT_NAMES, Rule
from ..registry import Registry
from ..resolve import resolve


def _schema_type(schema: Any) -> tc.T:
    """The static type of `reply`: a closed pydantic schema is typed by its model's annotations
    (rules read the validated instance, see guard.rule_view), a dataclass by its fields."""
    if schema is None:
        return tc.DYN
    model = getattr(schema, "__inv_pydantic__", None)
    if model is not None:
        return tc.from_py(model)
    if isinstance(schema, dict):
        return tc.T({"map"}, fields={k: tc.from_py(v) for k, v in schema.items()}, closed=True)
    return tc.from_py(schema)


def _type_env(rule: Rule, reg: Registry, i: Any, names: dict[str, str]) -> dict[str, tc.T]:
    """Static types of the names one enforcer's clauses read (dyn where code declares none)."""
    enf = rule.enforcers[i] if i != "rule" else {}
    point = enf.get("point", "decorator")
    env = {n: tc.DYN for n in DEFAULT_NAMES}
    tk = enf.get("target", rule.target_key)
    if point != "monitor" and tk:                       # a monitor's rows are whatever the caller passes
        params, ret = tc.signature_types(resolve(tk))
        params.pop("self", None)
        env["args"] = tc.T({"map"}, fields=params)
        if point in ("decorator", "middleware") and not rule.policy:
            env["result"] = ret
        if rule.policy is not None or point == "edge":
            env["reply"] = _schema_type(getattr(rule.target, "__inv_schema__", None))
    for n, path in names.items():
        head, *rest = path.split(".")
        if head == "provider":
            prov = reg.providers[rest[0]]
            t = tc.from_py(prov.type)
            env[n] = tc.merge(t, tc.T({"null"})) if prov.nullable else t
            continue
        t = env.get(head, tc.DYN)
        for f in rest:
            try:
                t = t.field(f)
            except tc.TypeMismatch as e:     # view {x: args.n.m} with n: int
                where = "" if i == "rule" else f"enforcer {i} ({point}) "
                how = "view" if n in (enf.get("view") or {}) else "bind"
                raise LoadError(f"{rule.id}: {where}{how} {n}: {path!r} does not resolve: {e}") from None
        env[n] = t
    return env


def _typecheck(rule: Rule, reg: Registry, sources: list[tuple[str, str]]) -> None:
    """Refuse a clause whose operand types have no overload (spec 2.1: type-checked at load), in the
    environment of each enforcer that evaluates it (its own point and target); the rule-level map
    (inv.evaluate's) only when no enforcer has one: typed as a decorator on subject.target, it would
    give a monitor's rows the target's types (fix round 33)."""
    own = {i: m for i, m in rule.names_for.items() if i != "rule"}
    for i, names in (own or rule.names_for).items():
        env = _type_env(rule, reg, i, names)
        for src, msg in sources:
            try:
                read = tc.check_clause(src, env)
            except tc.TypeMismatch as e:
                where = "" if i == "rule" or len(own) == 1 else f" (enforcer {i}, point {rule.enforcers[i]['point']})"
                raise LoadError(f"{rule.id}: clause {msg!r} is ill-typed{where}: {e}") from None
            except cel.CelSyntaxError:
                continue                                # the backend parsed it; leave it to the backend
            if getattr(reg, "backend_name", None) == "cel-expr" and any(
                    t.kinds and t.kinds & {"timestamp", "duration"} for t in read):
                from ..cel.expr_python import NO_PROTOBUF, protobuf_available
                if not protobuf_available():
                    raise LoadError(f"{rule.id}: clause {msg!r} reads a datetime or timedelta, and {NO_PROTOBUF}")
