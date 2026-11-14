"""Round 33 (lock and loader): each test failed before its fix, except where a docstring says it pins
behaviour an audit claim got wrong.

M6. A $ref added beside other keywords is judged by the new side's dialect: an OpenAPI 3.0 contract
    moved to 3.1 (JSON Schema 2020-12, where the siblings still apply) is no weakening.
M7. Clauses are type-checked in each evaluating enforcer's own environment; the rule-level map (typed
    as a decorator on subject.target) only when no enforcer has one, so a monitor's rows are not given
    the target's types.
M9. The "only restates the enum" note and the abstain check read Literal members under Annotated and
    PEP 695 aliases, compare them as values of one type (True is not 1, an Enum member is its value),
    and leave the abstain value out only when one is configured.
M10. A top-level allow_unresolved error names the file and line; YAML keys are compared as read
    (1 and 01, true and yes, null and ~, 1.0 and 1.00 are one key).
M11. The JSON Schema refuses an unknown subject key, a provider path with fields after the provider's
    name, and a provider argument that is no dotted name.
"""
from __future__ import annotations

import enum
import json
import sys
from typing import Annotated, Any, Literal

import pytest

import invariants as inv
import fixround as fx
from invariants.errors import LoadError
from invariants.loader import read_document, schema_path
from invariants.loader.policy import _abstain_in_schema, _restates_enum
from invariants_schema_diff.changes import _json_paths
from invariants.lock.rule_diff import _rule_changes
from invariants_schema_diff.schemas import constraint_weakening

M = "py:test_round33_lock"
NO_FEEDBACK = {"format_retries": 0, "semantic_resamples": 0, "feedback": {"format": "none"}}


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


# --- H7 (not reproduced): a changed subject.target is WEAKENED through the subject field ----------
def test_a_changed_default_door_is_weakened_through_the_subject():
    """Pins behaviour: with mediation.door left out, subject.target is the door, and changing it is
    WEAKENED as a subject change (rule.subject is WEAKENING), whatever the mediation diff says."""
    def pin(t: str, med: dict[str, Any]) -> dict[str, Any]:
        return {"rev": 1, "status": "detect", "targets": [t], "semantic": {"subject": {"target": t}, "mediation": med}}
    for ma, mb in [({"sink": "py:s.w"}, {"sink": "py:s.w"}),
                   ({"sink": "py:s.w", "allow": ["py:x"]}, {"sink": "py:s.w"})]:
        got = _rule_changes("R", pin("py:a.f", ma), pin("py:b.g", mb))
        assert any(c.what == "subject.target" and c.weakened for c in got), got


# --- M6 -------------------------------------------------------------------------------------------
def _contract(dialect: str | None, body: dict[str, Any]) -> dict[str, Any]:
    e: dict[str, Any] = {"type": "openapi", "components": {"N": {"type": "string", "maxLength": 3}},
                         "operations": {"op": {"method": "post", "path": "/x",
                                               "requestBody": {"content": {"application/json": body}}}}}
    if dialect:
        e["dialect"] = dialect
    return e


def _ref_verdicts(old_dialect: str | None, new_dialect: str | None) -> list[str | None]:
    old = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 5}}}
    new = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 5, "$ref": "#/components/N"}}}
    a, b = _contract(old_dialect, old), _contract(new_dialect, new)
    return [constraint_weakening(p, x, y, a, b) for p, x, y in _json_paths(a, b) if p.endswith("$ref")]


def test_a_ref_added_beside_siblings_in_openapi_3_1_is_no_weakening_after_3_0():
    assert _ref_verdicts("openapi-3.0", None) == [None]


@pytest.mark.parametrize("old,new", [("openapi-3.0", "openapi-3.0"), (None, "openapi-3.0")])
def test_a_ref_added_beside_siblings_where_the_new_side_ignores_them_is_still_weakened(old, new):
    (why,) = _ref_verdicts(old, new)
    assert why is not None and "ignored next to it" in why


# --- M7 -------------------------------------------------------------------------------------------
def _monitor(rid: str, bad: str) -> dict[str, Any]:
    return fx.doc(fx.rule(rid, "plain", bad, fx.enf("monitor", timing="post_effect", mode="detect"), status="detect"))


@pytest.mark.parametrize("bad", ['args.n == "x"', 'result > "x"'])
def test_a_monitor_rule_is_not_typed_from_its_target(iso, bad):
    reg = inv.load(_monitor("MO-1", bad))                # plain(n: int) -> int; a monitor's rows are dyn
    assert "MO-1" in reg.rules


def test_a_decorator_rule_is_still_type_checked(iso):
    with pytest.raises(inv.LoadError, match="ill-typed"):
        inv.load(fx.doc(fx.rule("D-1", "plain", 'args.n == "x"', fx.enf("decorator"))))


def test_each_enforcer_of_a_rule_is_type_checked_and_named(iso):
    with pytest.raises(inv.LoadError, match=r"ill-typed \(enforcer 0, point decorator\)"):
        inv.load(fx.doc(fx.rule("D-2", "plain", 'args.n == "x"', fx.enf("decorator"),
                                fx.enf("monitor", timing="post_effect", mode="detect"))))


# --- M8 (not reproduced): an empty model_policy is refused before any type check -------------------
@pytest.mark.parametrize("enf,match", [("model_call", "needs model_policy"), ("decorator", "needs a model_call")])
def test_an_empty_model_policy_is_refused(iso, enf, match):
    """Pins behaviour: `model_policy: {}` never reaches the type check (where `not rule.policy` and
    `rule.policy is not None` would disagree)."""
    target = "smodel" if enf == "model_call" else "plain"
    with pytest.raises(inv.LoadError, match=match):
        inv.load(fx.doc(fx.rule("M-8", target, "args.n > 1", fx.enf(enf, on_violation="escalate"), model_policy={})))


# --- M9 -------------------------------------------------------------------------------------------
class _Probe:
    def __init__(self, field: str, values: list[Any]) -> None:
        self.got = (("reply", field), values)

    def negated_membership(self) -> Any:
        return self.got


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


@pytest.mark.parametrize("t,values,ab,noted", [
    (Literal["A", "B", None], ["A", "B"], None, False),                    # null still fires the clause
    (Literal["A", "B", None], ["A", "B"], {"field": "cause", "value": None}, True),
    (Literal["A", "B"], ["A", "B"], None, True),
    (Literal[1, 2], [True, 2], None, False),                                # true is not 1
    (Literal[True, False], [1, 0], None, False),
    (Literal[Color.RED, Color.BLUE], ["red", "blue"], None, True),          # an Enum member is its value
    (Annotated[Literal["A", "B"], "meta"], ["A", "B"], None, True),
    (Literal["A", "B"], ["A"], None, False),
])
def test_restates_enum(t, values, ab, noted):
    assert (_restates_enum(_Probe("cause", values), {"cause": t}, ab) == "cause") is noted


def test_an_abstain_value_outside_an_annotated_enum_is_refused():
    with pytest.raises(LoadError, match="not a member of the closed enum"):
        _abstain_in_schema("R", {"field": "cause", "value": "NOPE"},
                           {"cause": Annotated[Literal["A", "B"], "meta"]}, "Reply")
    _abstain_in_schema("R", {"field": "cause", "value": "A"}, {"cause": Annotated[Literal["A", "B"], "meta"]}, "Reply")


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 aliases need Python 3.12")
def test_a_pep_695_alias_of_an_enum_is_read():
    ns: dict[str, Any] = {"Literal": Literal}
    exec("type Cause = Literal['A', 'B']", ns)
    assert _restates_enum(_Probe("cause", ["A", "B"]), {"cause": ns["Cause"]}, None) == "cause"
    with pytest.raises(LoadError, match="not a member"):
        _abstain_in_schema("R", {"field": "cause", "value": "NOPE"}, {"cause": ns["Cause"]}, "Reply")


class OptCause(inv.Checked): ...


@inv.model_call("M9-N", schema={"cause": Literal["A", "B", None]})
def opt_cause(prompt: str) -> OptCause:
    return '{"cause": "A"}'  # type: ignore[return-value]


def test_no_note_when_null_is_left_out_and_no_abstain_is_configured(iso):
    reg = inv.load({"spec": "invariant/v0.1", "rules": [{
        "id": "M9-N", "status": "detect", "subject": {"target": f"{M}.opt_cause"},
        "bad": '!(reply.cause in ["A", "B"])', "model_policy": NO_FEEDBACK,
        "enforcers": [{"point": "model_call", "timing": "post_effect", "mode": "detect", "on_violation": "escalate"}],
        "falsified_by": ["t"]}]})
    assert reg.notes == []


# --- M10 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [("1", "01"), ("true", "yes"), ("null", "~"), ("1.0", "1.00"), ("x", "x")])
def test_keys_yaml_reads_as_one_are_refused(tmp_path, a, b):
    f = tmp_path / "r.yaml"
    f.write_text(f"spec: invariant/v0.1\nx:\n  {a}: 1\n  {b}: 2\n")
    with pytest.raises(LoadError, match=r"r\.yaml:4: .*appears twice in one mapping \(lines 3 and 4"):
        read_document(f)


def test_keys_of_different_types_are_two_keys(tmp_path):
    f = tmp_path / "r.yaml"
    f.write_text("spec: invariant/v0.1\nrules: []\nx:\n  '1': 1\n  1: 2\n")
    assert read_document(f)["x"] == {"1": 1, 1: 2}


SHARED = ("spec: invariant/v0.1\nrules:\n- id: R\n  status: detect\nallow_unresolved:\n"
          "  - {at: 'a.py:1', code: unresolved/reflection, sha: 0123456789abcdef, why: w, rules: [R]}\n"
          "  - {at: 'a.py:2', code: unresolved/reflection, sha: 0123456789abcdef, why: w}\n")


def test_a_shared_allow_unresolved_error_names_the_file_and_line(tmp_path):
    f = tmp_path / "r.yaml"
    f.write_text(SHARED)
    with pytest.raises(LoadError, match=r"r\.yaml:6: allow_unresolved\[0\]: rule R is not a path law"):
        read_document(f)
    f.write_text(SHARED.replace("rules: [R]", "rules: [R, R]"))
    with pytest.raises(LoadError, match=r"r\.yaml:6: allow_unresolved\[0\]: rule R is listed twice"):
        read_document(f)
    f.write_text(SHARED.replace("  status: detect\n", "  status: detect\n  door: py:m.d\n  sink: py:m.s\n"))
    with pytest.raises(LoadError, match=r"r\.yaml:9: allow_unresolved\[1\]: an entry here names its rules"):
        read_document(f)                             # the second entry's own line
    f.write_text("spec: invariant/v0.1\nrules: []\nallow_unresolved: {}\n")
    with pytest.raises(LoadError, match=r"r\.yaml:3: allow_unresolved must be a list"):
        read_document(f)


def test_a_shared_allow_unresolved_error_in_json_names_the_file(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"spec": "invariant/v0.1", "rules": [], "allow_unresolved": [{"at": "a.py:1"}]}))
    with pytest.raises(LoadError, match=r"r\.json: allow_unresolved\[0\]: an entry here names its rules"):
        read_document(f)


# --- M11 ------------------------------------------------------------------------------------------
def _schema_errors(doc: dict[str, Any]) -> list[str]:
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(json.loads(schema_path().read_text(encoding="utf-8")))
    return [e.message for e in v.iter_errors(doc)]


def _rule(**kw: Any) -> dict[str, Any]:
    return {"spec": "invariant/v0.1", "rules": [{"id": "R", "status": "detect", "bad": "args.n > 1", **kw}]}


def test_the_schema_refuses_an_unknown_subject_key():
    assert _schema_errors(_rule(subject={"targets": "py:m.f"}))
    assert not _schema_errors(_rule(subject={"target": "py:m.f", "contract": "c", "operation": "op"}))


@pytest.mark.parametrize("path,ok", [("provider.p", True), ("provider.p.x", False), ("provider", False),
                                     ("args.n.m", True), ("old_self.a", True), ("nope.a", False)])
def test_the_schema_path_matches_the_loader(path, ok):
    assert (not _schema_errors(_rule(bind={"x": path}))) is ok


@pytest.mark.parametrize("arg,ok", [("args.account", True), ("self", True), ("acct.id", True), ("", False),
                                    ("args.", False), ("args..x", False)])
def test_the_schema_provider_args_are_dotted_names(arg, ok):
    doc = {"spec": "invariant/v0.1", "providers": {"p": {"type": "int", "from": "py:m.p", "args": {"a": arg}}}}
    assert (not _schema_errors(doc)) is ok


def test_an_unknown_subject_key_is_a_load_error():
    import invariants as inv
    import pytest as _pt
    from invariants.errors import LoadError
    with inv.isolated():
        with _pt.raises(LoadError, match=r"subject.*targets.*target"):
            inv.load({"rules": [{"id": "R-1", "status": "detect", "subject": {"targets": "py:json.dumps"},
                                 "bad": "false", "enforcers": [{"point": "monitor", "mode": "detect"}]}]})
