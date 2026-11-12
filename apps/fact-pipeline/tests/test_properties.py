"""Hypothesis properties: each guard agrees with its rule, and the store's own schema agrees with it too."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import invariants as inv
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fact_pipeline.extract import propose_fact
from fact_pipeline.fake_model import read_facts
from fact_pipeline.models import AnswerReply, EvidenceSpan, Proposal
from fact_pipeline.pipeline import Pipeline
from fact_pipeline.store import Store, fact_id

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
dates = st.one_of(st.none(), st.dates(dt.date(1990, 1, 1), dt.date(2030, 12, 31)).map(dt.date.isoformat))


class Scripted:
    """A model client that always returns one given reply."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    def complete(self, task: dict[str, Any], feedback: str | None = None) -> str:
        return json.dumps(self.reply)


@SETTINGS
@given(valid_from=dates, valid_to=dates)
def test_k7_guard_and_check_constraint_agree(valid_from: str | None, valid_to: str | None) -> None:
    store = Store()
    src = store.add_source("t://d", "Jane Roe became CEO of Acme Corp in 2021.", "p")
    chunk = store.chunks_for(src.id)[0]
    acme, jane = store.add_entity("Acme Corp"), store.add_entity("Jane Roe")
    fid = fact_id(acme, "ceo_of", jane, "NONE", valid_from, valid_to)
    Pipeline(store, Scripted({}))                    # binds the store the providers read
    prop = Proposal(fid, acme, "ceo_of", jane, "entity", "NONE", valid_from, valid_to,
                    [EvidenceSpan(src.id, src.hash, chunk.id, 0, 8, "Jane Roe", "ASSERTED")])
    bad = inv.evaluate(inv.current().rules["K-7"], {"args": {"proposal": prop}}).kind == "violation"
    expected = valid_from is not None and valid_to is not None and valid_to < valid_from
    assert bad == expected
    try:                                             # guard
        store.ingest(prop)
        guarded_ok = True
    except inv.Rejected as e:
        assert e.rule == "K-7"
        guarded_ok = False
    assert guarded_ok == (not bad)
    raw = Store()                                    # storage alone: accepts a row exactly when bad is false
    raw.conn.execute("INSERT INTO entities VALUES (?, 'Acme Corp')", (acme,))
    rsrc = raw.add_source("t://d", "Jane Roe.", "p")
    raw.conn.execute("INSERT INTO evidence VALUES ('f', ?, 'h', 'c', 0, 4, 'Jane', 'ASSERTED')", (rsrc.id,))
    try:
        raw.conn.execute("INSERT INTO facts (id, subject_id, predicate, object, object_kind, qualifier, valid_from, "
                         "valid_to, status) VALUES ('f', ?, 'ceo_of', 'x', 'literal', 'NONE', ?, ?, 'CHECKED')",
                         (acme, valid_from, valid_to))
        stored = True
    except sqlite3.IntegrityError:
        stored = False
    assert stored == (not bad)


TEXT = "Acme Corp said Tuesday it completed its acquisition of Beta Ltd for about $2.1 billion."


@SETTINGS
@given(data=st.data())
def test_k2_ok_exactly_when_quote_is_at_offsets(data: st.DataObject) -> None:
    store = Store()
    src = store.add_source("t://d", TEXT, "p")
    chunk = store.chunks_for(src.id)[0]
    start = data.draw(st.integers(-3, len(TEXT) + 3))
    end = data.draw(st.integers(-3, len(TEXT) + 3))
    honest = data.draw(st.booleans())
    quote = TEXT[max(start, 0):max(end, 0)] if honest else data.draw(st.text(max_size=20))
    reply = {**read_facts(TEXT)[0], "start": start, "end": end, "quote": quote}
    Pipeline(store, Scripted(reply))
    at_offsets = 0 <= start <= end <= len(TEXT) and TEXT[start:end] == quote
    try:
        got = propose_fact(chunk.id, chunk.text, chunk.source_hash, 0)
        assert at_offsets and got.verdict == "ok"
    except inv.Escalation as e:
        assert not at_offsets and e.rule == "K-2"


ids = st.sampled_from([f"src_x:h:{i}" for i in range(6)])


@SETTINGS
@given(retrieved=st.lists(ids, max_size=4, unique=True), cited=st.lists(ids, max_size=4))
def test_k19_violation_iff_a_citation_was_not_retrieved(retrieved: list[str], cited: list[str]) -> None:
    reply = inv.parse({"status": "ANSWERED", "text": "t", "citations": cited}, AnswerReply)
    out = inv.evaluate(inv.current().rules["K-19"], {"args": {"retrieved_ids": retrieved}, "reply": reply})
    assert (out.kind == "violation") == (not cited or any(c not in retrieved for c in cited))


@SETTINGS
@given(sources=st.lists(st.tuples(st.sampled_from(["pub-a", "pub-b", "pub-c"]),
                                  st.sampled_from(["trusted", "untrusted"]), st.booleans()),
                        min_size=1, max_size=5))
def test_k12_promotes_iff_two_distinct_trusted_publishers(sources: list[tuple[str, str, bool]]) -> None:
    store = Store()
    Pipeline(store, Scripted({}))
    acme = store.add_entity("Acme Corp")
    evidence = []
    for i, (pub, trust, deleted) in enumerate(sources):
        src = store.add_source(f"t://{i}", "Acme Corp is headquartered in Springfield.", pub, trust)
        evidence.append((src, EvidenceSpan(src.id, src.hash, store.chunks_for(src.id)[0].id, 0, 9, "Acme Corp",
                                           "ASSERTED"), deleted))
    fid = fact_id(acme, "headquartered_in", "Springfield", "NONE", None, None)
    for _, ev, _ in evidence:
        store.ingest(Proposal(fid, acme, "headquartered_in", "Springfield", "literal", "NONE", None, None, [ev]))
    for src, _, deleted in evidence:
        if deleted and store.status(fid) != "RETRACTED":
            store.delete_source(src.id)
    live = {pub for (pub, trust, deleted) in sources if trust == "trusted" and not deleted}
    if store.status(fid) != "CHECKED":
        assert store.status(fid) == "RETRACTED" and all(d for _, _, d in sources)
        return
    try:
        store.promote(fid)
        promoted = True
    except inv.Rejected:
        promoted = False
    assert promoted == (len(live) >= 2)


NAMES = st.sampled_from(["Acme Corp", "Gamma Inc", "Delta Group", "Omega Ltd", "Zeta"])


@SETTINGS
@given(buyer=NAMES, target=NAMES, n=st.integers(1, 999), q=st.sampled_from(["", "about ", "up to ", "at least "]))
def test_fake_model_quotes_are_always_at_their_offsets(buyer: str, target: str, n: int, q: str) -> None:
    text = f"{buyer} completed its acquisition of {target} for {q}${n} million."
    facts = read_facts(text)
    assert [f["predicate"] for f in facts] == ["acquired", "acquisition_price"]
    for f in facts:
        assert text[f["start"]:f["end"]] == f["quote"]
