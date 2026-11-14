"""model_policy: its shape, held_data, the inv_feedback parameter, and the abstain field against the closed schema."""
from __future__ import annotations

import typing
from typing import Any

from invariants_cel.typeinfo import strip

from ..errors import LoadError
from ..model import POLICY_FIELDS, Rule
from ..registry import Registry
from .schemas import _params, literal_eq, schema_fields, schema_name
from .shapes import _check_fields


def _enum_members(t: Any) -> tuple[Any, ...] | None:
    """The members of a closed enum field: a Literal[...] under any Annotated layer or PEP 695 alias."""
    t = strip(t)
    return typing.get_args(t) if typing.get_origin(t) is typing.Literal else None


def _restates_enum(prog: Any, fields: dict[str, Any], ab: Any) -> str | None:
    """A clause `!(reply.F in [...])` whose list covers F's closed enum (minus the abstain value, when
    one is configured). Members are matched as literal_eq does: True is not 1, an Enum member stands
    for its value."""
    probe = getattr(prog, "negated_membership", None)
    got = probe() if probe else None
    if not got:
        return None
    path, values = got
    if len(path) != 2 or path[0] != "reply":
        return None
    members = _enum_members(fields.get(path[1]))
    if members is None:
        return None
    left = [m for m in members if not (ab is not None and literal_eq(ab["value"], m))]
    return path[1] if all(any(literal_eq(v, m) for v in values) for m in left) else None


def _policy_shape(rid: str, pol: Any) -> tuple[dict[str, Any], Any, Any]:
    """The policy's fields, retry counts, feedback and abstain shape; returns (policy, feedback, abstain)."""
    if isinstance(pol, dict) and "on_exhaust" in pol:
        raise LoadError(f"{rid}: model_policy has no on_exhaust; the model_call enforcer's on_violation "
                        f"decides once retries are spent")
    _check_fields(pol, POLICY_FIELDS, f"{rid}.model_policy")
    if not isinstance(pol, dict):
        raise LoadError(f"{rid}: model_policy must be a mapping")
    for k in ("format_retries", "semantic_resamples"):
        v = pol.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise LoadError(f"{rid}: model_policy.{k} must be a non-negative integer")
    fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
    _check_fields(fb, {"format", "semantic"}, f"{rid}.model_policy.feedback")
    if fb.get("format", "parser_error") not in ("parser_error", "none") or fb.get("semantic", "none") != "none":
        raise LoadError(f"{rid}: feedback.format must be parser_error|none and feedback.semantic none")
    ab = pol.get("abstain")
    if ab is not None:
        _abstain_shape(rid, ab)
    return pol, fb, ab


def _abstain_shape(rid: str, ab: Any) -> None:
    _check_fields(ab, {"field", "value"}, f"{rid}.model_policy.abstain")
    if {"field", "value"} - set(ab):
        raise LoadError(f"{rid}: abstain needs field and value")
    if not isinstance(ab["field"], str) or not ab["field"]:
        raise LoadError(f"{rid}: abstain field must be a field name, not {ab['field']!r}")
    if ab["value"] is not None and type(ab["value"]) not in (str, int, float, bool):
        raise LoadError(f"{rid}: abstain value must be a string, number, bool or null, not {ab['value']!r}")


def _abstain_in_schema(rid: str, ab: Any, fields: dict[str, Any], name: str) -> None:
    """The abstain field is a schema field, and its value a member of the field's closed enum."""
    if ab["field"] not in fields:
        raise LoadError(f"{rid}: abstain field {ab['field']!r} is not in {name} (fields: {sorted(fields)})")
    members = _enum_members(fields[ab["field"]])
    if members is not None and not any(literal_eq(ab["value"], a) for a in members):
        raise LoadError(f"{rid}: abstain value {ab['value']!r} is not a member of the closed enum "
                        f"{name}.{ab['field']} {list(members)}")


def _load_policy(rule: Rule, reg: Registry) -> None:
    rid = rule.id
    pol, fb, ab = _policy_shape(rid, rule.policy)
    held = rule.raw.get("compares_to") == "held_data"
    if held and (pol.get("semantic_resamples", 0) != 0 or fb.get("semantic", "none") != "none"):
        raise LoadError(f"{rid}: compares_to: held_data forces semantic_resamples: 0 and feedback.semantic: none "
                        f"(declared {pol.get('semantic_resamples')})")
    rule.semantic_resamples = 0 if held else pol.get("semantic_resamples", 1)
    tk = rule.target_key
    if fb.get("format", "parser_error") == "parser_error" and "inv_feedback" not in _params(rule.target):
        raise LoadError(f"{rid}: feedback.format parser_error needs a parameter inv_feedback: str | None on {tk}")
    schema = getattr(rule.target, "__inv_schema__", None)
    if schema is None:
        if any(e.get("point") == "edge" for e in rule.enforcers):   # an edge would have nothing to re-check against
            raise LoadError(f"{rid}: {tk} has no inv.model_call schema (__inv_schema__), and the rule's edge "
                            f"enforcer re-checks what it receives against it: decorate {tk} with "
                            f"@inv.model_call, or give a decorator above it functools.wraps")
        return                                       # verify reports a missing @inv.model_call
    fields = schema_fields(schema)
    name = schema_name(schema)
    if ab is not None:
        _abstain_in_schema(rid, ab, fields, name)
    for prog, msg in rule.clauses:
        f = _restates_enum(prog, fields, ab)
        if f:
            note = f"{rid}: clause {msg!r} only restates the enum {name}.{f}: held by the schema (rung 1)"
            rule.notes.append(note)
            reg.notes.append(note)
