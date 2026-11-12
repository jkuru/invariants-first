"""The async extraction path gives the same result as the sync one, guards included."""
from __future__ import annotations

import asyncio

import invariants as inv
import pytest

from fact_pipeline.fake_model import FakeModel
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store

from .conftest import S1, S2

DOC = S1 + " " + S2 + " Jane Roe became CEO of Acme Corp in 2021. Acme Corp is headquartered in Springfield."


def _facts(store: Store) -> list[tuple[str, str, str]]:
    return sorted((f["predicate"], f["object"], f["qualifier"]) for f in store.facts())


def test_async_matches_sync() -> None:
    a, b = Store(), Store()
    Pipeline(a, FakeModel()).process("test://doc", DOC, "pub")
    asyncio.run(Pipeline(b, FakeModel()).process_async("test://doc", DOC, "pub"))
    assert _facts(a) == _facts(b) and len(_facts(a)) >= 4


def test_async_path_runs_the_k2_check(store: Store) -> None:
    model = FakeModel({"misquote": 100})
    pipe = Pipeline(store, model)
    rep = asyncio.run(pipe.process_async("test://doc", DOC, "pub"))
    assert rep.stored == [] and rep.reviews
    assert {r["rule"] for r in store.reviews("extraction")} == {"K-2"}


def test_async_path_counts_in_the_current_registry(store: Store) -> None:
    before = inv.current().counts("K-2").get("runs", 0)
    asyncio.run(Pipeline(store, FakeModel()).extract_async(store.add_source("t://d", DOC, "p").id))
    assert inv.current().counts("K-2")["runs"] > before


@pytest.mark.parametrize("concurrency", [1, 3, 8])
def test_async_keeps_chunk_order(store: Store, concurrency: int) -> None:
    pipe = Pipeline(store, FakeModel())
    sid = store.add_source("t://d", DOC, "p").id
    items, _ = asyncio.run(pipe.extract_async(sid, concurrency))
    assert [x.chunk_id for x in items] == sorted((x.chunk_id for x in items), key=lambda c: int(c.rsplit(":", 1)[1]))


def test_fake_model_async_client() -> None:
    m = FakeModel()
    out = asyncio.run(m.acomplete({"task": "extract", "text": "Acme Corp is headquartered in Springfield.", "index": 0}))
    assert '"headquartered_in"' in out
