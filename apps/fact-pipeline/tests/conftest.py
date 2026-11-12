"""Shared fixtures. The pytest plugin loads invariants.yaml (ini: inv_rules) into the default registry."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import invariants as inv
import pytest

from fact_pipeline import rules_path
from fact_pipeline.fake_model import FakeModel
from fact_pipeline.models import EvidenceSpan, Proposal
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store, fact_id

S1 = ("Acme Corp said Tuesday it completed its acquisition of Beta Ltd for about $2.1 billion. "
      "Beta had denied talks in 2024.")
S2 = "Acme Corp completed its acquisition of Beta Ltd for $2.0 billion."


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



