"""Answers cite only retrieved chunks (K-19)."""
from __future__ import annotations

import invariants as inv
from invariants.testing import make_checked
import pytest

from fact_pipeline.answer import CheckedAnswer, compose_answer, render_answer
from fact_pipeline.fake_model import FakeModel
from fact_pipeline.models import AnswerReply
from fact_pipeline.pipeline import Pipeline

from .conftest import S1, S2


@pytest.fixture
def loaded(pipe: Pipeline) -> Pipeline:
    pipe.process("test://s1", S1, "newswire-a")
    pipe.process("test://s2", S2, "wire-b")
    return pipe


def test_answer_cites_a_retrieved_chunk(loaded: Pipeline) -> None:
    a = loaded.answer("Who completed the acquisition of Beta Ltd?")
    assert a["status"] == "ANSWERED" and a["citations"]
    assert set(a["citations"]) <= set(a["retrieved"])


def test_uncited_chunk_once_is_resampled(loaded: Pipeline, model: FakeModel) -> None:
    model.inject("uncited_chunk")
    a = loaded.answer("Who completed the acquisition of Beta Ltd?")
    assert a["status"] == "ANSWERED" and set(a["citations"]) <= set(a["retrieved"])


def test_k19_uncited_chunk_is_refused(loaded: Pipeline, model: FakeModel) -> None:
    model.inject("uncited_chunk", 5)
    chunks = loaded.store.retrieve("Who completed the acquisition of Beta Ltd?")
    ids = [c.id for c in chunks]
    with pytest.raises(inv.Rejected) as e:
        compose_answer("Who completed the acquisition of Beta Ltd?", ids, [{"id": c.id, "text": c.text} for c in chunks])
    assert e.value.rule == "K-19"


def test_pipeline_reports_refused_answer(loaded: Pipeline, model: FakeModel) -> None:
    model.inject("no_citation", 5)
    assert loaded.answer("Who completed the acquisition of Beta Ltd?")["status"] == "REFUSED"


def test_unanswerable_question_escalates_to_no_answer(loaded: Pipeline) -> None:
    assert loaded.answer("zzzz qqqq")["status"] == "NO_ANSWER"


def test_garbage_answer_retried(loaded: Pipeline, model: FakeModel) -> None:
    model.inject("answer_garbage")
    assert loaded.answer("Who completed the acquisition of Beta Ltd?")["status"] == "ANSWERED"


def test_edge_refuses_forged_answer(loaded: Pipeline) -> None:
    reply = inv.parse({"status": "ANSWERED", "text": "x", "citations": ["nope"]}, AnswerReply)
    forged = make_checked(CheckedAnswer, reply, verdict="ok", rule="K-19")
    with pytest.raises(inv.Rejected, match="not retrieved"):
        render_answer(forged, ["a", "b"])


def test_deleted_source_is_never_retrieved(loaded: Pipeline) -> None:
    s1 = loaded.store.chunks_for(loaded.store.retrieve("acquisition Beta")[0].source_id)[0].source_id
    loaded.delete_source(s1)
    assert all(c.source_id != s1 for c in loaded.store.retrieve("acquisition Beta Acme"))
