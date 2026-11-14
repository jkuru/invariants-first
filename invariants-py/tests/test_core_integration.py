"""Core changes made when integrating the CLI, CEL engines, pydantic/ASGI and pytest plugin."""
from __future__ import annotations

import dataclasses
import typing

import pydantic
import pytest

import invariants as inv
import invariants_cel as cel
from invariants import registry
from invariants_cel import subset
from support import raises

MOD = __name__


def _doc(point: str = "decorator", bad: str = "args.n < 0") -> dict[str, typing.Any]:
    return {"spec": "invariant/v0.1",
            "contracts": {"me": {"type": "python", "ref": MOD}},
            "targets": {f"py:{MOD}.handler": {"effects": "none"}},
            "rules": [{"id": "M-1", "status": "enforce", "subject": {"target": f"py:{MOD}.handler"}, "bad": bad,
                       "falsified_by": ["tests/x.py::t"],
                       "enforcers": [{"point": point, "timing": "pre_effect", "mode": "enforce"}]}]}


@inv.guard(f"py:{MOD}.handler", points=("decorator", "middleware"))
def handler(n: int) -> int:
    return n


@inv.guard(f"py:{MOD}.plain")
def plain(n: int) -> int:
    return n


# --- guard points ----------------------------------------------------------------------------------
def test_guard_evaluates_middleware_point():
    with inv.isolated():
        inv.load(_doc("middleware"))
        assert inv.verify() == 1
        assert handler(1) == 1
        e = raises(inv.Rejected, handler, -1)
        assert e is not None and e.rule == "M-1" and e.outcome.kind == "violation"


def test_verify_requires_guard_to_evaluate_the_point():
    doc = _doc("middleware")
    doc["targets"] = {f"py:{MOD}.plain": {"effects": "none"}}
    doc["rules"][0]["subject"]["target"] = f"py:{MOD}.plain"
    with inv.isolated():
        inv.load(doc)
        e = raises(inv.CoverageError, inv.verify)
        assert e is not None and "does not evaluate point middleware" in str(e)
        assert plain(-1) == -1                 # a decorator-only guard skips middleware enforcers


def test_guard_rejects_unknown_points():
    with pytest.raises(ValueError):
        inv.guard("X-1", points=("storage",))


# --- registry: contracts, scope, observers, global ablation -----------------------------------------
def test_registry_keeps_contracts_and_scope():
    with inv.isolated() as reg:
        inv.load(_doc())
        assert reg.contracts == {"me": {"type": "python", "ref": MOD}}
        assert reg.scope() == {MOD}


def test_observe_sees_every_registry():
    seen: list[tuple[str, str]] = []
    stop = registry.observe(lambda rid, kind, ev: seen.append((rid, kind)))
    try:
        with inv.isolated():
            inv.load(_doc())
            handler(1)
            raises(inv.Rejected, handler, -1)
    finally:
        stop()
    stop()                                     # a second unsubscribe is harmless
    assert seen == [("M-1", "ok"), ("M-1", "violation")]
    assert not registry.OBSERVERS


def test_ablated_everywhere_reaches_existing_and_new_registries():
    with inv.isolated() as before:
        inv.load(_doc())
        with registry.ablated_everywhere("M-1"), registry.ablated_everywhere("M-1"):
            assert before.is_ablated("M-1") and handler(-1) == -1
            with inv.isolated():
                inv.load(_doc())
                assert handler(-1) == -1
        assert not before.is_ablated("M-1") and not registry.GLOBAL_ABLATED
        assert raises(inv.Rejected, handler, -1) is not None


def test_standing_reason_names_the_scan():
    with inv.isolated():
        inv.load(_doc())
        _, why = inv.standing("M-1")
        assert "inv scan not run" in why


# --- pydantic schema accepted by the core -----------------------------------------------------------
class Reply(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cause: typing.Literal["A", "B"]
    n: int


def test_core_parse_accepts_pydantic_model():
    r = inv.parse('{"cause": "A", "n": 1}', Reply)
    assert isinstance(r, Reply) and r.cause == "A"           # the validated model instance
    assert raises(inv.ParseError, inv.parse, '{"cause": "C", "n": 1}', Reply) is not None
    assert raises(inv.ParseError, inv.parse, '{"cause": "A", "n": 1, "x": 0}', Reply) is not None


class CheckedReply(inv.Checked): ...


def test_core_model_call_accepts_pydantic_model():
    @inv.model_call("R-X", schema=Reply)
    def ask(q: str, inv_feedback: str | None = None) -> CheckedReply:
        return q

    from invariants.integrations.pydantic import closed
    assert ask.__inv_schema__ is closed(Reply)


# --- the subset evaluator, as fixed during integration --------------------------------------------
@pytest.mark.parametrize("src,env,want", [
    ("[0, 1].exists(x, 1 / x > 0)", {}, True),
    ("[0, -1].all(x, 1 / x > 0)", {}, False),
    ("has(a.b)", {"a": {"b": None}}, True),
    ('size("héllo") == 5', {}, True),
    ('"\\u00e9" == "é"', {}, True),
])
def test_subset_cel_semantics(src, env, want):
    assert cel.get_backend("subset").compile(src).evaluate(env) is want


@pytest.mark.parametrize("src", ["9223372036854775807 + 1", "-(-9223372036854775807 - 1)",
                                 "[0, 1].exists_one(x, 1 / x > 0)", "x.broken == 1"])
def test_subset_cel_errors(src):
    class Broken:
        @property
        def broken(self) -> int:
            raise RuntimeError("db down")
    with pytest.raises(cel.CelError):
        cel.get_backend("subset").compile(src).evaluate({"x": Broken()})


@pytest.mark.parametrize("src", ["inv.sum(a, b) > 0", "inv.zip(a) == []", "inv.unique_by(xs, 1, 1)"])
def test_subset_checks_inv_shapes_at_load(src):
    with pytest.raises(cel.CelSyntaxError):
        subset.parse(src)
