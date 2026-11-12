"""R-7: model call and edge (prototype checks 39-57)."""
from __future__ import annotations

import triage

import invariants as inv
from support import GOOD, load_error, load_variant, model, raises, rule


def r7_held_data_mismatch_caught() -> bool:
    """Falsifier r7_held_data_mismatch: a TIMEOUT reply for an OOM-killed job escalates at once."""
    _, esc = model('{"cause": "TIMEOUT", "message": "x"}', GOOD)
    return isinstance(esc, inv.Escalation) and len(esc.attempts) == 1


def r7_forged_checked_caught() -> bool:
    """Falsifier r7_forged_checked: the edge refuses a forged Checked carrying a bad value."""
    forged = object.__new__(triage.CheckedCause)
    for k, v in (("value", triage.CauseReply("TIMEOUT", "forged")), ("attempts", 1), ("rule", "R-7"),
                 ("verdict", "ok")):
        object.__setattr__(forged, k, v)
    e = raises(inv.Rejected, triage.remediate, forged, "J-oom")
    return e is not None and e.outcome.kind == "violation"


def test_good_reply(reg):
    c, _ = model(GOOD)
    assert isinstance(c, triage.CheckedCause) and c.verdict == "ok" and c.attempts == 1
    assert c.cause == "OUT_OF_MEMORY" and c.rule == "R-7"


def test_extra_field_retried_with_parser_error(reg):
    c, _ = model('{"cause": "OUT_OF_MEMORY", "message": "x", "job_id": "J9"}', GOOD)
    assert c and c.attempts == 2 and "extra fields" in (triage.client.calls[1]["feedback"] or "")
    assert triage.client.calls[0]["feedback"] is None


def test_made_up_cause_is_parse_error(reg):
    c, _ = model('{"cause": "COSMIC_RAY", "message": "x"}', GOOD)
    assert c and c.attempts == 2 and "COSMIC_RAY" in (triage.client.calls[1]["feedback"] or "")


def test_held_data_mismatch_escalates_without_resample(reg):
    triage.client.script('{"cause": "TIMEOUT", "message": "x"}', GOOD)
    _, esc = model('{"cause": "TIMEOUT", "message": "x"}', GOOD)
    assert isinstance(esc, inv.Escalation) and len(esc.attempts) == 1 and len(triage.client.calls) == 1
    assert len(esc.outcome.messages) == 2


def test_without_compares_to_one_fresh_resample(reg):
    with inv.using(load_variant(lambda d: (rule(d, "R-7").pop("compares_to"),
                                           rule(d, "R-7")["model_policy"].update(semantic_resamples=1)))):
        c, _ = model('{"cause": "TIMEOUT", "message": "x"}', GOOD)
    assert c and c.attempts == 2 and triage.client.calls[1]["feedback"] is None


def test_abstain_escalates_before_bad(reg):
    runs7 = reg.evidence["R-7"]["runs"]
    _, esc = model('{"cause": "CANNOT_COMPLY", "message": ""}')
    assert isinstance(esc, inv.Escalation) and len(esc.attempts) == 1 and reg.evidence["R-7"]["runs"] == runs7
    assert esc.attempts[0]["outcome"] == "abstain" and esc.rule == "R-7"


def test_three_parse_errors_then_on_violation(reg):
    _, esc = model("not json", '{"cause": 1, "message": "x"}', "{}")
    assert isinstance(esc, inv.Escalation) and len(esc.attempts) == 3
    assert esc.outcome.kind == "parse_error" and "missing fields" in esc.attempts[-1]["error"]


def test_rule_error_is_never_resampled(reg):
    _, esc = model('{"cause": "TIMEOUT", "message": "x"}', GOOD, job="J-unknown")
    assert isinstance(esc, inv.Escalation) and esc.outcome.kind == "rule_error" and len(triage.client.calls) == 1


def test_load_rejects_abstain_outside_enum():
    e = load_error(lambda d: rule(d, "R-7")["model_policy"]["abstain"].update(value="UNKNOWN"))
    assert e is not None and "closed enum" in str(e)


def test_load_rejects_on_exhaust():
    e = load_error(lambda d: rule(d, "R-7")["model_policy"].update(on_exhaust="escalate"))
    assert e is not None and "on_exhaust" in str(e)


def test_load_refuses_resample_on_held_data_rule():
    e = load_error(lambda d: rule(d, "R-7")["model_policy"].update(semantic_resamples=1))
    assert e is not None and "held_data" in str(e)


@inv.model_call("R-7", schema=triage.CauseReply)
def propose_without_feedback(log_excerpt: str, job_id: str) -> triage.CheckedCause:
    return triage.client.create(prompt=log_excerpt)


def test_load_requires_inv_feedback_param():
    e = load_error(lambda d: rule(d, "R-7")["subject"].update(target=f"py:{__name__}.propose_without_feedback"))
    assert e is not None and "inv_feedback" in str(e)


def test_enum_restating_clause_is_noted():
    enum_reg = load_variant(lambda d: rule(d, "R-7")["bad"].append(
        {"when": "!(reply.cause in [\"TIMEOUT\", \"OUT_OF_MEMORY\", \"BAD_INPUT\"])", "message": "closed set"}))
    assert len(enum_reg.notes) == 1 and "rung 1" in enum_reg.notes[0]
    assert enum_reg.rules["R-7"].notes == enum_reg.notes


def test_partial_enum_clause_is_not_noted():
    r = load_variant(lambda d: rule(d, "R-7")["bad"].append(
        {"when": "!(reply.cause in [\"TIMEOUT\"])", "message": "partial"}))
    assert r.notes == []


def test_detect_mode_verdict_refused_by_enforcing_edge(reg):
    with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(mode="detect"))):
        c, _ = model('{"cause": "TIMEOUT", "message": "x"}')
        e = raises(inv.Rejected, triage.remediate, c, "J-oom")
    assert c is not None and c.verdict == "violation" and e is not None


def test_detect_mode_exhausted_parse_returns_parse_error_verdict(reg):
    with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(mode="detect"))):
        c, _ = model("x", "y", "z")
    assert c is not None and c.verdict == "parse_error" and c.value == "z"


def test_off_mode_acts_as_detect(reg):
    with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(mode="off"))):
        c, _ = model('{"cause": "TIMEOUT", "message": "x"}')
    assert c is not None and c.verdict == "violation"


def test_fail_open_and_page_verdict_is_rule_error(reg):
    with inv.using(load_variant(lambda d: rule(d, "R-7")["enforcers"][0].update(on_error="fail_open_and_page"))):
        c, _ = model(GOOD, job="J-unknown")
    assert c is not None and c.verdict == "rule_error"


def test_edge_refuses_raw_reply(reg):
    assert raises(TypeError, triage.remediate, triage.CauseReply("TIMEOUT", ""), "J1") is not None


def test_checked_cannot_be_constructed(reg):
    assert raises(TypeError, triage.CheckedCause, None) is not None


def test_checked_is_immutable(reg):
    c, _ = model(GOOD)
    assert raises(AttributeError, setattr, c, "verdict", "ok") is not None


def test_edge_accepts_honest_checked(reg):
    c, _ = model(GOOD)
    assert triage.remediate(c, "J-oom") == "remediate J-oom: OUT_OF_MEMORY"


def test_edge_refuses_forged_checked(reg):
    assert r7_forged_checked_caught()


def test_edge_accepts_forged_checked_with_good_value(reg):
    forged = object.__new__(triage.CheckedCause)
    for k, v in (("value", triage.CauseReply("OUT_OF_MEMORY", "ok")), ("attempts", 1), ("rule", "R-7"),
                 ("verdict", "ok")):
        object.__setattr__(forged, k, v)
    assert triage.remediate(forged, "J-oom") == "remediate J-oom: OUT_OF_MEMORY"


def test_model_call_needs_checked_return():
    def bad(x: str) -> str: ...
    assert raises(TypeError, inv.model_call("R-7", schema=triage.CauseReply), bad) is not None


CheckedCause = triage.CheckedCause


def test_model_call_accepts_string_annotations():
    def f(x: str, inv_feedback: str | None = None) -> "CheckedCause": ...
    assert inv.model_call("R-7", schema=triage.CauseReply)(f).__inv_kind__ == "model_call"


def test_edge_needs_checked_param():
    def bad(x: str) -> None: ...
    assert raises(TypeError, inv.edge("R-7"), bad) is not None


def test_parse_dict_schema():
    assert inv.parse('{"a": 1}', {"a": int}) == {"a": 1}
    assert raises(inv.ParseError, inv.parse, '{"a": true}', {"a": int}) is not None
    assert raises(inv.ParseError, inv.parse, "[1]", {"a": int}) is not None
