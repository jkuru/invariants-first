"""Shared fixtures. The pytest plugin loads invariants.yaml (ini: inv_rules) into the default registry.

The defect tests run on the fake model (it can be told to misbehave). The happy-path flows in
test_replay.py run on the model APP_MODEL names: replay (the default) answers from
tests/fixtures/traces/; `--record` (or APP_MODEL=record) calls Claude and rewrites those traces.
"""
from __future__ import annotations

import os
import pathlib
from collections.abc import Callable, Iterator
from typing import Any

import invariants as inv
import pytest

from fact_pipeline import llm, rules_path
from fact_pipeline.fake_model import FakeModel, ModelClient
from fact_pipeline.models import EvidenceSpan, Proposal
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store, fact_id

S1 = ("Acme Corp said Tuesday it completed its acquisition of Beta Ltd for about $2.1 billion. "
      "Beta had denied talks in 2024.")
S2 = "Acme Corp completed its acquisition of Beta Ltd for $2.0 billion."
TRACES = pathlib.Path(__file__).resolve().parent / "fixtures" / "traces"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--record", action="store_true", default=False,
                     help="APP_MODEL=record: run the replay flows on Claude and rewrite tests/fixtures/traces/ "
                          "(needs the anthropic package and ANTHROPIC_API_KEY)")


@pytest.fixture
def model_mode(request: pytest.FixtureRequest) -> str:
    """replay (default), record, anthropic or fake: what the replay flows run on."""
    return "record" if request.config.getoption("--record") else os.environ.get("APP_MODEL", "replay")


@pytest.fixture
def traced(model_mode: str) -> Iterator[Callable[[str], ModelClient]]:
    """traced(name): the APP_MODEL model client for trace tests/fixtures/traces/<name>.json.
    On replay, every call the trace holds must have been made by the end of the test."""
    made: list[ModelClient] = []

    def make(name: str) -> ModelClient:
        m = llm.select(model_mode, TRACES / f"{name}.json")
        made.append(m)
        return m
    yield make
    for m in made:
        if isinstance(m, llm.LLMClient) and hasattr(m.llm, "assert_done"):
            m.llm.assert_done()


@pytest.fixture(autouse=True, scope="session")
def _rules() -> None:
    """Load the rules if the plugin did not (for example, run with -p no:invariants)."""
    if not inv.current().rules:
        inv.load(rules_path())


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store()
    yield s
    s.close()


@pytest.fixture
def pipe(store: Store, model: FakeModel) -> Pipeline:
    return Pipeline(store, model)


@pytest.fixture
def make_proposal(store: Store) -> Callable[..., Proposal]:
    """A valid proposal on a real chunk; keyword overrides make it bad."""
    src = store.add_source("test://doc", "Acme Corp is headquartered in Springfield.", "pub-a")
    chunk = store.chunks_for(src.id)[0]
    acme = store.add_entity("Acme Corp")

    def make(**kw: Any) -> Proposal:
        f: dict[str, Any] = {"subject_id": acme, "predicate": "headquartered_in", "object": "Springfield",
                             "object_kind": "literal", "qualifier": "NONE", "valid_from": None, "valid_to": None}
        ev = kw.pop("evidence", None)
        f.update(kw)
        if ev is None:
            ev = [EvidenceSpan(src.id, src.hash, chunk.id, 0, 9, "Acme Corp", "ASSERTED")]
        fid = fact_id(f["subject_id"], f["predicate"], f["object"], f["qualifier"], f["valid_from"], f["valid_to"])
        return Proposal(fid, evidence=ev, **f)
    return make



