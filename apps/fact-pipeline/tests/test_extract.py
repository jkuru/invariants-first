"""Extraction: parse, abstain, K-2 with a bounded resample, and the K-2 edge."""
from __future__ import annotations

import json

import invariants as inv
from invariants.testing import make_checked
import pytest

from fact_pipeline.extract import CheckedFact, accept_fact, propose_fact
from fact_pipeline.fake_model import FakeModel, read_facts
from fact_pipeline.models import FactReply
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store

from .conftest import S1


def _chunk(pipe: Pipeline, text: str = S1, i: int = 0) -> tuple[str, str, str]:
    src = pipe.intake("test://s1", text, "newswire-a")
    c = pipe.store.chunks_for(src.id)[i]
    return c.id, c.text, c.source_hash


def test_fake_model_reads_the_worked_example() -> None:
    facts = read_facts(S1.split(". ")[0] + ".")
    assert [f["predicate"] for f in facts] == ["acquired", "acquisition_price"]
    assert facts[1]["quote"] == "about $2.1 billion" and facts[1]["qualifier"] == "APPROX"


def test_clean_extraction_is_ok_first_time(pipe: Pipeline) -> None:
    cid, text, h = _chunk(pipe)
    got = propose_fact(cid, text, h, 0)
    assert isinstance(got, CheckedFact) and got.verdict == "ok" and got.attempts == 1
    assert got.predicate == "acquired" and got.subject == "Acme Corp"


def test_misquote_once_is_rescued_by_a_fresh_resample(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("misquote")
    got = propose_fact(cid, text, h, 0)
    assert got.verdict == "ok" and got.attempts == 2
    assert model.calls[-1]["feedback"] is None        # semantic feedback: none


def test_k2_persistent_misquote_escalates(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("misquote", 5)
    with pytest.raises(inv.Escalation) as e:
        propose_fact(cid, text, h, 0)
    assert e.value.rule == "K-2" and len(e.value.attempts) == 2


def test_k2_stale_source_hash(pipe: Pipeline) -> None:
    cid, text, _ = _chunk(pipe)
    with pytest.raises(inv.Escalation, match="source hash is stale"):
        propose_fact(cid, text, "0" * 64, 0)


def test_model_supplied_id_is_a_parse_error_then_retried(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("model_id")
    got = propose_fact(cid, text, h, 0)
    assert got.verdict == "ok" and got.attempts == 2
    assert "extra fields forbidden" in (model.calls[-1]["feedback"] or "")


def test_k18_model_id_every_time_never_gets_through(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("model_id", 10)
    with pytest.raises(inv.Escalation) as e:
        propose_fact(cid, text, h, 0)
    assert [a["outcome"] for a in e.value.attempts] == ["parse_error"] * 3


def test_k4_unknown_predicate_is_a_parse_error(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("bad_predicate")
    got = propose_fact(cid, text, h, 0)
    assert got.attempts == 2 and "predicate" in (model.calls[-1]["feedback"] or "")


def test_garbage_reply_is_retried_with_parser_error(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("garbage")
    assert propose_fact(cid, text, h, 0).attempts == 2
    assert (model.calls[-1]["feedback"] or "").startswith("not JSON")


def test_abstain_escalates_at_once(pipe: Pipeline, model: FakeModel) -> None:
    cid, text, h = _chunk(pipe)
    model.inject("abstain")
    with pytest.raises(inv.Escalation, match="abstained"):
        propose_fact(cid, text, h, 0)
    assert len(model.calls) == 1


def test_k2_edge_refuses_forged_checked(pipe: Pipeline) -> None:
    cid, text, h = _chunk(pipe)
    f = read_facts(text)[0]
    bad = inv.parse(json.dumps({**f, "quote": "Acme Corp bought Beta"}), FactReply)
    forged = make_checked(CheckedFact, bad, verdict="ok", rule="K-2")
    with pytest.raises(inv.Rejected) as e:
        accept_fact(forged, cid, h)
    assert e.value.rule == "K-2"


def test_edge_refuses_non_ok_verdict(pipe: Pipeline) -> None:
    cid, text, h = _chunk(pipe)
    got = propose_fact(cid, text, h, 0)
    forged = make_checked(CheckedFact, got.value, verdict="violation", rule="K-2")
    with pytest.raises(inv.Rejected, match="verdict violation"):
        accept_fact(forged, cid, h)


def test_pipeline_turns_escalations_into_reviews(pipe: Pipeline, model: FakeModel) -> None:
    model.inject("abstain")
    rep = pipe.process("test://s1", S1, "newswire-a")
    assert [r["kind"] for r in pipe.store.reviews()] == ["extraction"]
    assert pipe.store.reviews()[0]["rule"] == "K-2"
    assert rep.claims == 1


def test_pipeline_rejects_inverted_interval_at_ingest(pipe: Pipeline, model: FakeModel) -> None:
    model.inject("inverted_interval")
    rep = pipe.process("test://ceo", "Jane Roe became CEO of Acme Corp in 2021.", "newswire-a")
    assert rep.stored == [] and rep.rejected and rep.rejected[0].startswith("K-7")


def test_pipeline_rejects_empty_quote_at_ingest(pipe: Pipeline, model: FakeModel) -> None:
    model.inject("empty_quote")
    rep = pipe.process("test://hq", "Acme Corp is headquartered in Springfield.", "newswire-a")
    assert rep.stored == [] and rep.rejected[0].startswith("K-1")


def test_denied_statement_becomes_a_claim(pipe: Pipeline) -> None:
    pipe.process("test://s1", S1, "newswire-a")
    claims = pipe.store.claims()
    assert len(claims) == 1 and claims[0]["polarity"] == "DENIED" and claims[0]["predicate"] == "in_talks"


def test_evidence_offsets_are_absolute_into_the_source(pipe: Pipeline) -> None:
    rep = pipe.process("test://s1", S1, "newswire-a")
    for fid in rep.stored:
        for e in pipe.store.evidence(fid):
            assert S1[e["start"]:e["end_"]] == e["quote"]
