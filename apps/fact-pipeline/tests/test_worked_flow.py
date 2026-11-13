"""A worked flow, step by step: Acme acquires Beta."""
from __future__ import annotations

import importlib
import pathlib
import sys

from fact_pipeline import monitor
from fact_pipeline.fake_model import FakeModel
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store

from .conftest import S1, S2

BYPASS = pathlib.Path(__file__).resolve().parents[1] / "bypass"


def test_worked_flow_7_6(store: Store, model: FakeModel) -> None:
    pipe = Pipeline(store, model)
    # Two registered companies share the name "Beta Ltd": the resolver must not guess.
    beta_uk = store.add_entity("Beta Ltd (UK)", aliases=("Beta Ltd",), xrefs=(("companies-house", "0123"),))
    store.add_entity("Beta Ltd (Cayman)", aliases=("Beta Ltd",), xrefs=(("cima", "77"),))

    # 1. Intake: code records hash, trust and publisher.  2. Extract.  3. Guard.
    model.inject("drop_qualifier")                  # P2 first quotes "for $2.1 billion"
    rep = pipe.process("news://newswire-a/acme-beta", S1, "newswire-a")
    s1 = store.source(rep.source_id or "")
    assert s1 is not None and s1.trust == "trusted" and len(s1.hash) == 64
    assert rep.extracted == 3 and rep.claims == 1   # P3 (denied talks) is a claim, not a fact
    assert store.claims()[0]["polarity"] == "DENIED"

    # 4. Resolve: the ambiguous "Beta Ltd" goes to review, never to the model.
    entity_reviews = store.reviews("entity")
    assert len(entity_reviews) == 2 and rep.stored == []
    assert set(entity_reviews[0]["payload"]["candidates"]) >= {beta_uk}
    rep = pipe.resolve_review(entity_reviews[0]["id"], beta_uk)   # a person rules
    assert len(rep.stored) == 2 and store.reviews("entity") == []
    facts = {f["predicate"]: f for f in store.facts()}
    p1, p2 = facts["acquired"], facts["acquisition_price"]
    assert p2["qualifier"] == "APPROX" and p2["retry_count"] == 1    # the fresh extraction
    assert store.evidence(p2["id"])[0]["quote"] == "about $2.1 billion"
    assert p1["object"] == beta_uk

    # Later S2 gives $2.0bn: K-11 fires and code opens a Conflict with an owner.
    rep = pipe.process("news://wire-b/acme-beta", S2, "wire-b")
    assert len(rep.conflicts) == 1
    (conflict,) = store.conflicts()
    assert conflict["owner"] == "team-knowledge" and p2["id"] in conflict["fact_ids"]

    # 5. Promote: two independent publishers confirm P1; P2 stays CHECKED and is audited.
    assert pipe.promote_all() == [p1["id"]]
    assert store.status(p1["id"]) == "BELIEVED" and store.status(p2["id"]) == "CHECKED"
    assert p2["id"] in pipe.audit_sample()

    # 6. Retraction: S1 is withdrawn. Facts resting only on S1 go; P1 drops back to CHECKED.
    r = pipe.delete_source(s1.id)
    assert r.retracted == [p2["id"]] and r.demoted == [p1["id"]] and r.live_orphans == 0
    assert store.status(p2["id"]) == "RETRACTED" and store.status(p1["id"]) == "CHECKED"

    # 7. Monitor: the nightly run catches a write that bypassed the door.
    assert monitor.run(store) == []
    sys.path.insert(0, str(BYPASS))
    try:
        importlib.import_module("fact_pipeline_backfill.backfill").force_believe(store.conn, p1["id"])
    finally:
        sys.path.remove(str(BYPASS))
    assert [(f.rule, f.key) for f in monitor.run(store)] == [("K-12", p1["id"])]

    # Answers cite only what was retrieved, and S1 is no longer retrievable.
    a = pipe.answer("What was the price of the acquisition of Beta Ltd?")
    assert a["status"] == "ANSWERED" and not any(c.startswith(s1.id) for c in a["retrieved"])
