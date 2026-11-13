"""Pydantic v2 integration: closed schemas, ValidationError as parse_error, enums, contracts."""
from __future__ import annotations

import copy
import dataclasses
import gc
import importlib
import json
import sys
from typing import Any, Callable, Iterator, Literal, Optional

import pydantic
import pytest

import invariants as inv
from invariants.integrations import pydantic as inv_pd

GOOD = json.dumps({"cause": "OUT_OF_MEMORY", "message": "killed at 8 GiB", "confidence": 0.9,
                   "evidence": [{"source": "kernel", "line": 12}]})


app: Any = None     # the example app, imported per module (see _app)


@pytest.fixture(scope="module", autouse=True)
def _app() -> Iterator[None]:
    """Import the example app for this module only, then drop it.

    The app stays separate from the library and from other tests: inv.verify scopes itself to
    the loaded targets' packages, and dropping the module keeps its guards out of later tests."""
    global app
    app = importlib.import_module("integ_pydantic_app")
    yield                       # not the module: pytest keeps a fixture's value past teardown
    app = None
    sys.modules.pop("integ_pydantic_app", None)
    gc.collect()


def reply(**over: Any) -> str:
    d = json.loads(GOOD)
    d.update(over)
    return json.dumps(d)


def doc_with(mutate: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(app.RULES)
    if mutate:
        mutate(doc)
    return doc


@pytest.fixture
def preg() -> Iterator[inv.Registry]:
    with inv.isolated() as r:
        inv.load(doc_with())
        yield r


def call(*replies: str, job: str = "J-oom") -> tuple[Any, Any]:
    app.client.script(*replies)
    try:
        return app.propose_cause("trace", job), None
    except inv.InvariantError as e:
        return None, e


# --- closed schemas -----------------------------------------------------------------------------
def test_closed_is_a_cached_dataclass_named_after_the_model():
    s = inv_pd.closed(app.CauseReply)
    assert dataclasses.is_dataclass(s) and s.__name__ == "CauseReply"
    assert inv_pd.closed(app.CauseReply) is s and getattr(s, inv_pd.MODEL_ATTR) is app.CauseReply


def test_enum_field_is_presented_as_literal():
    hints = {f.name: f.type for f in dataclasses.fields(inv_pd.closed(app.CauseReply))}
    assert hints["cause"] == Literal["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT", "CANNOT_COMPLY"]
    assert hints["message"] is object


def test_enum_values_of_an_enum():
    assert inv_pd.enum_values(app.Cause) == [c.value for c in app.Cause]
    assert inv_pd.enum_values(app.Cause | None) == [c.value for c in app.Cause] + [None]


@pytest.mark.parametrize("ann,values", [
    (Literal["a", "b"], ["a", "b"]),
    (Optional[Literal["a"]], ["a", None]), (Literal["a"] | None, ["a", None]),
    (str, None), (int | str, None),
])
def test_enum_values(ann, values):
    assert inv_pd.enum_values(ann) == values


def test_model_allowing_extras_is_a_load_error():
    with pytest.raises(inv.LoadError, match="extra='forbid'"):
        inv_pd.closed(app.OpenReply)


def test_nested_model_allowing_extras_is_a_load_error():
    with pytest.raises(inv.LoadError, match="OpenReply"):
        inv_pd.closed(app.LeakyReply)


def test_aliases_are_a_load_error():
    class Aliased(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="forbid")
        x: int = pydantic.Field(alias="X")
    with pytest.raises(inv.LoadError, match="alias"):
        inv_pd.closed(Aliased)


def test_not_a_model_is_a_type_error():
    with pytest.raises(TypeError):
        inv_pd.closed(dict)  # type: ignore[arg-type]


def test_model_call_with_open_model_raises_at_decoration():
    with pytest.raises(inv.LoadError):
        @inv_pd.model_call("R-P7", schema=app.OpenReply)
        def f(x: str) -> app.CheckedCause: ...


def test_schema_for_passes_other_schemas_through():
    assert inv_pd.schema_for(app.JobFacts) is app.JobFacts


# --- parse --------------------------------------------------------------------------------------
def test_parse_good_reply_is_the_model_instance():
    r = inv_pd.parse(GOOD, app.CauseReply)
    assert isinstance(r, app.CauseReply) and r.cause is app.Cause.OUT_OF_MEMORY
    assert r.evidence == [app.Evidence(source="kernel", line=12)]
    m = inv_pd.model_of(r)
    assert isinstance(m, app.CauseReply) and m == r and m is not r


def test_parse_extra_field_forbidden():
    with pytest.raises(inv.ParseError, match="extra fields"):
        inv_pd.parse(reply(job_id="J9"), app.CauseReply)


def test_parse_nested_extra_field_forbidden_by_pydantic():
    # the model-facing feedback names the extra key; str() (events, logs) masks it as reply data
    with pytest.raises(inv.ParseError, match=r"evidence\.0\.\*: extra_forbidden") as ei:
        inv_pd.parse(reply(evidence=[{"source": "log", "line": 1, "extra": 1}]), app.CauseReply)
    assert "evidence.0.extra" in ei.value.feedback


def test_parse_enum_membership():
    with pytest.raises(inv.ParseError, match="cause: string is not one of the allowed values") as ei:
        inv_pd.parse(reply(cause="COSMIC_RAY"), app.CauseReply)
    assert "COSMIC_RAY" in ei.value.feedback and "COSMIC_RAY" not in str(ei.value)


def test_parse_validation_error_text_is_short():
    with pytest.raises(inv.ParseError) as ei:
        inv_pd.parse(reply(confidence=3), app.CauseReply)
    text = ei.value.feedback
    assert text.startswith("confidence: ") and "less than or equal to 1" in text
    assert "errors.pydantic.dev" not in text and "input_value" not in text
    assert str(ei.value) == "confidence: less_than_equal"


def test_model_of_rejects_plain_values():
    with pytest.raises(TypeError):
        inv_pd.model_of(app.JobFacts("J", False, 1, 2))


# --- model_call through the core ----------------------------------------------------------------
def test_good_reply(preg):
    c, err = call(GOOD)
    assert err is None and isinstance(c, app.CheckedCause) and c.verdict == "ok" and c.attempts == 1
    assert c.cause == "OUT_OF_MEMORY" and inv_pd.model_of(c).confidence == 0.9


def test_validation_error_is_parse_error_fed_back(preg):
    c, _ = call(reply(confidence=-1), GOOD)
    assert c.attempts == 2 and c.verdict == "ok"
    assert app.client.calls[0] is None
    assert app.client.calls[1].startswith("confidence: ") and "greater than or equal to 0" in app.client.calls[1]
    assert preg.sink[0]["outcome"] == "parse_error" and preg.sink[0]["action"] == "retry"


def test_retries_spent_applies_on_violation(preg):
    bad = reply(confidence=7)
    _, err = call(bad, bad, bad)
    assert isinstance(err, inv.Escalation) and err.outcome.kind == "parse_error" and len(err.attempts) == 3


def test_held_data_mismatch_escalates(preg):
    _, err = call(reply(cause="TIMEOUT"), GOOD)
    assert isinstance(err, inv.Escalation) and err.outcome.kind == "violation" and len(err.attempts) == 1


def test_held_data_mismatch_passes_when_ablated(preg):
    with inv.ablated("R-P7"):
        c, err = call(reply(cause="TIMEOUT"))
    assert err is None and c.verdict == "ok"


def test_abstain_on_enum_value_escalates(preg):
    _, err = call(reply(cause="CANNOT_COMPLY"))
    assert isinstance(err, inv.Escalation) and len(err.attempts) == 1


def test_abstain_value_outside_the_enum_is_a_load_error():
    doc = doc_with(lambda d: d["rules"][0]["model_policy"]["abstain"].update(value="DUNNO"))
    with pytest.raises(inv.LoadError, match="closed enum"):
        inv.load(doc, install=False)


def test_clause_restating_the_enum_is_held_by_the_schema():
    doc = doc_with(lambda d: d["rules"][0]["bad"].append(
        {"when": '!(reply.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])', "message": "unknown cause"}))
    reg = inv.load(doc, install=False)
    assert any("held by the schema" in n for n in reg.notes)


def test_verify_accepts_the_pydantic_schema(preg):
    """verify is process-wide (other fixture apps' guards show up); none of its problems is R-P7's."""
    try:
        inv.verify(preg)
    except inv.CoverageError as e:
        assert "R-P7" not in str(e)


def test_edge_accepts_good_and_refuses_forged(preg):
    c, _ = call(GOOD)
    assert app.remediate(c, "J-oom") == "remediate J-oom: OUT_OF_MEMORY"
    forged = object.__new__(app.CheckedCause)
    value = inv_pd.parse(reply(cause="TIMEOUT"), app.CauseReply)
    for k, v in (("value", value), ("attempts", 1), ("rule", "R-P7"), ("verdict", "ok")):
        object.__setattr__(forged, k, v)
    with pytest.raises(inv.Rejected) as ei:
        app.remediate(forged, "J-oom")
    assert ei.value.outcome.kind == "violation"


def test_edge_revalidates_a_forged_value(preg):
    schema = inv_pd.closed(app.CauseReply)
    value = inv_pd.parse(GOOD, app.CauseReply)
    object.__setattr__(value, "confidence", 5.0)       # tampered after parsing
    forged = object.__new__(app.CheckedCause)
    for k, v in (("value", value), ("attempts", 1), ("rule", "R-P7"), ("verdict", "ok")):
        object.__setattr__(forged, k, v)
    assert isinstance(value, app.CauseReply) and getattr(schema, "__inv_pydantic__") is app.CauseReply
    with pytest.raises(inv.Rejected) as ei:
        app.remediate(forged, "J-oom")
    assert ei.value.outcome.kind == "parse_error"


def test_edge_parse_error_for_an_unparsed_model_names_the_schema(preg):
    from invariants.testing import make_checked
    raw = {"cause": "OUT_OF_MEMORY", "message": "m", "confidence": 0.5, "evidence": []}   # not a model instance
    forged = make_checked(app.CheckedCause, raw, verdict="ok")
    with pytest.raises(inv.Rejected) as ei:
        app.remediate(forged, "J-oom")
    err = ei.value.outcome.error
    assert ei.value.outcome.kind == "parse_error"
    assert "not CauseReply" not in err                 # was: "value is CauseReply, not CauseReply"
    assert "inv.parse" in err and "closed schema CauseReply" in err


# --- contracts ----------------------------------------------------------------------------------
def test_resolve_ref():
    assert inv_pd.resolve_ref("integ_pydantic_app:CauseReply") is app.CauseReply


@pytest.mark.parametrize("ref", ["integ_pydantic_app.CauseReply", "nope_module_x:A", "integ_pydantic_app:Nope",
                                 "integ_pydantic_app:_private", ":A", 3])
def test_resolve_ref_errors(ref):
    with pytest.raises(inv.LoadError):
        inv_pd.resolve_ref(ref)


def test_check_contracts(preg):
    assert inv_pd.check_contracts(doc_with(), preg) == {"triage": app.CauseReply}


def test_contract_not_a_model():
    doc = doc_with(lambda d: d["contracts"]["triage"].update(ref="integ_pydantic_app:JobFacts"))
    with pytest.raises(inv.LoadError, match="not a pydantic"):
        inv_pd.check_contracts(doc, inv.Registry())


def test_contract_open_model():
    doc = doc_with(lambda d: d["contracts"]["triage"].update(ref="integ_pydantic_app:OpenReply"))
    with pytest.raises(inv.LoadError, match="extra='forbid'"):
        inv_pd.check_contracts(doc, inv.Registry())


def test_contract_differs_from_decorator_schema(preg):
    doc = doc_with(lambda d: d["contracts"].update(triage={"type": "pydantic", "ref": "integ_pydantic_app:Evidence"}))
    with pytest.raises(inv.LoadError) as ei:
        inv_pd.check_contracts(doc, preg)
    assert "model_policy.schema" in str(ei.value) and "parses into CauseReply" in str(ei.value)


def test_check_contracts_reads_a_file(tmp_path, preg):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(doc_with()))
    assert inv_pd.check_contracts(p, preg) == {"triage": app.CauseReply}


def test_other_contract_types_are_ignored():
    doc = {"contracts": {"x": {"type": "protobuf", "ref": "api/pool.proto"}}, "rules": []}
    assert inv_pd.check_contracts(doc, inv.Registry()) == {}


def test_example_app_is_loaded_during_the_module():
    """The guards of the example app are reachable only through the module while it is loaded."""
    from invariants.registry import WRAPPED
    assert any(getattr(w, "__module__", "") == "integ_pydantic_app" for w in WRAPPED)


def test_other_rule_files_verify_while_the_app_is_loaded():
    """The app's guards name R-P7, which the core rule file lacks; verify is scoped, so it still passes."""
    from support import RULE_FILE
    with inv.isolated():
        inv.load(RULE_FILE)
        assert inv.verify() == 6
        e = None
        try:
            inv.verify(None, ["integ_pydantic_app"])
        except inv.CoverageError as exc:
            e = exc
        assert e is not None and "R-P7" in str(e)
