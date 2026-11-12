"""Ingest, promotion and deletion doors; the schema's own constraints are tested here as app code."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

import invariants as inv
import pytest

from fact_pipeline.models import EvidenceSpan, Proposal
from fact_pipeline.store import Store


def test_valid_proposal_is_stored_checked(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    assert store.status(fid) == "CHECKED"
    assert len(store.evidence(fid)) == 1


def test_same_fact_twice_adds_evidence_not_a_row(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    p = make_proposal()
    other = store.add_source("test://other", "Acme Corp is headquartered in Springfield.", "pub-b")
    ev = EvidenceSpan(other.id, other.hash, store.chunks_for(other.id)[0].id, 0, 9, "Acme Corp", "ASSERTED")
    store.ingest(p)
    store.ingest(make_proposal(evidence=[ev]))
    assert len(store.facts()) == 1 and len(store.evidence(p.id)) == 2


def test_k1_guard_refuses_fact_without_evidence(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    with pytest.raises(inv.Rejected) as e:
        store.ingest(make_proposal(evidence=[]))
    assert e.value.rule == "K-1"
    assert store.facts() == []


def test_k1_guard_refuses_empty_span(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    p = make_proposal()
    empty = EvidenceSpan(p.evidence[0].source_id, p.evidence[0].source_hash, p.evidence[0].chunk_id, 0, 0, "",
                         "ASSERTED")
    with pytest.raises(inv.Rejected, match="evidence span is empty"):
        store.ingest(make_proposal(evidence=[empty]))


def test_k1_trigger_refuses_raw_fact_without_evidence(store: Store) -> None:
    eid = store.add_entity("Acme Corp")
    with pytest.raises(sqlite3.IntegrityError, match="K-1"):
        store.conn.execute("INSERT INTO facts (id, subject_id, predicate, object, object_kind, qualifier, status) "
                           "VALUES ('fact_raw', ?, 'acquired', 'x', 'literal', 'NONE', 'CHECKED')", (eid,))


def test_k4_ingest_refuses_unknown_predicate(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    with pytest.raises(inv.Rejected) as e:
        store.ingest(make_proposal(predicate="owns"))
    assert e.value.rule == "K-4"


def test_k7_guard_refuses_inverted_interval(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    with pytest.raises(inv.Rejected) as e:
        store.ingest(make_proposal(valid_from="2024-06-30", valid_to="2023-01-01"))
    assert e.value.rule == "K-7"


def test_k7_open_and_equal_intervals_pass(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    store.ingest(make_proposal(valid_from="2024-01-01", valid_to="2024-01-01"))
    store.ingest(make_proposal(valid_from="2024-01-01", valid_to=None))
    assert len(store.facts()) == 2


def test_k7_check_refuses_raw_inverted_interval(store: Store) -> None:
    eid = store.add_entity("Acme Corp")
    c = store.conn
    c.execute("INSERT INTO evidence VALUES ('fact_raw', ?, 'h', 'c', 0, 4, 'Acme', 'ASSERTED')",
              (store.add_source("test://x", "Acme.", "p").id,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute("INSERT INTO facts (id, subject_id, predicate, object, object_kind, qualifier, valid_from, "
                  "valid_to, status) VALUES ('fact_raw', ?, 'ceo_of', 'x', 'literal', 'NONE', '2024-02-01', "
                  "'2024-01-01', 'CHECKED')", (eid,))


def test_k8_facts_are_append_only(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    for sql in ("UPDATE facts SET object = 'Shelbyville' WHERE id = ?", "DELETE FROM facts WHERE id = ?",
                "DELETE FROM evidence WHERE fact_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="K-8"):
            store.conn.execute(sql, (fid,))


def test_k9_one_external_record_one_entity(store: Store) -> None:
    a = store.add_entity("Beta Ltd (UK)", xrefs=(("companies-house", "0123"),))
    b = store.add_entity("Beta Ltd (US)")
    store.link_xref("sec-edgar", "999", a)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        store.link_xref("companies-house", "0123", b)


def test_k11_contradiction_routes(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    store.ingest(make_proposal())
    with pytest.raises(inv.Routed) as e:
        store.ingest(make_proposal(object="Shelbyville"))
    assert e.value.rule == "K-11" and e.value.review_id
    assert len(store.facts()) == 1            # the call stopped; nothing was written


def test_k11_recorded_conflict_lets_fact_in(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    first, second = make_proposal(), make_proposal(object="Shelbyville")
    store.ingest(first)
    store.open_conflict([first.id, second.id], "team-knowledge", "two cities")
    store.ingest(second)
    assert {f["id"] for f in store.facts()} == {first.id, second.id}


def test_k11_non_functional_predicate_never_routes(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    acme = make_proposal().subject_id
    gamma = store.add_entity("Gamma Inc")
    delta = store.add_entity("Delta Inc")
    store.ingest(make_proposal(predicate="acquired", object=gamma, object_kind="entity"))
    store.ingest(make_proposal(predicate="acquired", object=delta, object_kind="entity"))
    assert len([f for f in store.facts() if f["subject_id"] == acme]) == 2


def test_k12_single_source_is_not_believed(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    with pytest.raises(inv.Rejected, match="two independent trusted sources") as e:
        store.promote(fid)
    assert e.value.rule == "K-12" and store.status(fid) == "CHECKED"


def _second_source(store: Store, publisher: str, trust: str = "trusted") -> EvidenceSpan:
    src = store.add_source(f"test://{publisher}", "Acme Corp is headquartered in Springfield.", publisher, trust)
    return EvidenceSpan(src.id, src.hash, store.chunks_for(src.id)[0].id, 0, 9, "Acme Corp", "ASSERTED")


def test_k12_two_trusted_publishers_promote(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    store.ingest(make_proposal(evidence=[_second_source(store, "pub-b")]))
    store.promote(fid)
    assert store.status(fid) == "BELIEVED"


def test_k12_untrusted_second_source_does_not_count(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    store.ingest(make_proposal(evidence=[_second_source(store, "blog", "untrusted")]))
    with pytest.raises(inv.Rejected):
        store.promote(fid)


def test_k12_believed_cannot_be_promoted_again(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    fid = store.ingest(make_proposal())
    store.ingest(make_proposal(evidence=[_second_source(store, "pub-b")]))
    store.promote(fid)
    with pytest.raises(ValueError, match="only a CHECKED fact"):
        store.promote(fid)


def test_k15_delete_retracts_orphans(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    p = make_proposal()
    store.ingest(p)
    rep = store.delete_source(p.evidence[0].source_id)
    assert rep.retracted == [p.id] and rep.live_orphans == 0
    assert store.status(p.id) == "RETRACTED"


def test_k15_failed_retraction_is_reported(store: Store, make_proposal: Callable[..., Proposal],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """A post-condition of an external effect is reported, never rolled back by the library."""
    p = make_proposal()
    store.ingest(p)
    monkeypatch.setattr(Store, "_retract_orphans", lambda self, sid: [])     # a retraction bug
    with inv.capture() as events:
        store.delete_source(p.evidence[0].source_id)
    k15 = [e for e in events if e["rule"] == "K-15"]
    assert [e["outcome"] for e in k15] == ["violation"] and k15[0]["mode"] == "detect"


def test_k18_ingest_refuses_unminted_id(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    with pytest.raises(inv.Rejected, match="not minted") as e:
        store.ingest(make_proposal(subject_id="Q4115189"))
    assert e.value.rule == "K-18"


def test_k18_object_entity_must_be_minted(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    with pytest.raises(inv.Rejected, match="object id"):
        store.ingest(make_proposal(predicate="acquired", object="ent_madeup", object_kind="entity"))


def test_a_door_needs_no_transaction(store: Store, make_proposal: Callable[..., Proposal]) -> None:
    """The rules read the door's arguments and plain providers: the transaction is the store's own."""
    fid = store.ingest_door(make_proposal())
    assert [f["id"] for f in store.facts()] == [fid]
