"""The happy-path flows on a real-model path: a replayed trace by default (APP_MODEL=replay).

The committed traces in tests/fixtures/traces/ are hand-written fixtures in the shape of a recording,
not recordings (see the README there). `pytest --record` with ANTHROPIC_API_KEY set replaces them
with real ones, marked "fixture": "recorded".
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from fact_pipeline.fake_model import FakeModel, ModelClient
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store

from .conftest import S1, S2

Traced = Callable[[str], ModelClient]
QUESTION = "Who completed the acquisition of Beta Ltd?"


def test_extraction_of_s1_on_the_model(store: Store, traced: Traced) -> None:
    rep = Pipeline(store, traced("extract_s1")).process("news://newswire-a/acme-beta", S1, "newswire-a")
    assert rep.extracted == 3 and rep.claims == 1 and len(rep.stored) == 2
    facts = {f["predicate"]: f for f in store.facts()}
    assert facts["acquisition_price"]["qualifier"] == "APPROX"
    assert store.evidence(facts["acquisition_price"]["id"])[0]["quote"] == "about $2.1 billion"
    assert store.claims()[0]["polarity"] == "DENIED"


def test_concurrent_extraction_replays_in_any_order(store: Store, traced: Traced) -> None:
    pipe = Pipeline(store, traced("extract_s1"))
    rep = asyncio.run(pipe.process_async("news://newswire-a/acme-beta", S1, "newswire-a"))
    assert rep.extracted == 3 and rep.claims == 1


def loaded(store: Store) -> None:
    """S1 and S2 extracted by the fake, so the answer traces hold only the answer calls."""
    pipe = Pipeline(store, FakeModel())
    pipe.process("test://s1", S1, "newswire-a")
    pipe.process("test://s2", S2, "wire-b")


def test_an_answer_with_a_citation_fixed_on_the_second_turn(store: Store, traced: Traced, model_mode: str) -> None:
    """The first reply gives citations as a string, not a list; the parser error goes back as a
    follow-up turn in the same conversation, and the second reply is well formed."""
    if model_mode == "record":
        pytest.skip("a scripted format failure: a recording would replace it with whatever the model does")
    loaded(store)
    a = Pipeline(store, traced("answer_format_fix")).answer(QUESTION)
    assert a["status"] == "ANSWERED" and a["citations"] and set(a["citations"]) <= set(a["retrieved"])
    assert "Acme" in a["text"]


def test_a_question_the_chunks_do_not_answer(store: Store, traced: Traced, model_mode: str) -> None:
    if model_mode == "fake":
        pytest.skip("the fake answers from any chunk that shares a word with the question")
    loaded(store)
    a = Pipeline(store, traced("cannot_answer")).answer("Where is Beta Ltd headquartered?")
    assert a["status"] == "NO_ANSWER" and a["citations"] == []
