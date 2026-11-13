"""The pipeline: intake -> extract (model) -> resolve (code) -> ingest door -> promote -> answer."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import Any

import invariants as inv

from . import providers as _providers  # noqa: F401  (registers the providers the rules bind)
from .answer import compose_answer, render_answer
from . import context
from .extract import accept_fact, propose_fact
from .fake_model import ModelClient
from .models import ABSTAIN, NO_MORE, ChunkInfo, EvidenceSpan, Proposal, RetractionReport, SourceInfo
from .store import Store, fact_id


class NoSuchReview(LookupError):
    """resolve_review on an id that names no open entity review."""

MAX_FACTS_PER_CHUNK = 10


@dataclasses.dataclass
class Extracted:
    """One checked reply, as plain fields, with the chunk it came from."""

    chunk_id: str
    fields: dict[str, Any]
    attempts: int


@dataclasses.dataclass
class Report:
    """What one run did."""

    source_id: str | None = None
    extracted: int = 0
    stored: list[str] = dataclasses.field(default_factory=list)
    claims: int = 0
    conflicts: list[int] = dataclasses.field(default_factory=list)
    reviews: list[int] = dataclasses.field(default_factory=list)
    rejected: list[str] = dataclasses.field(default_factory=list)

    def merge(self, other: Report) -> Report:
        self.extracted += other.extracted
        self.stored += other.stored
        self.claims += other.claims
        self.conflicts += other.conflicts
        self.reviews += other.reviews
        self.rejected += other.rejected
        return self


class Pipeline:
    """Runs the stages against one store and one model client.

    The model calls read the store and the client as inv.Dependency values (context.STORE and
    context.CLIENT): each stage that makes one binds this pipeline's (self.bound()), so two
    pipelines in one thread or task never read each other's. The constructor also binds them for
    the rest of the caller's context, for a model call made directly after building one."""

    def __init__(self, store: Store, client: ModelClient, owner: str = "team-knowledge") -> None:
        self.store, self.client, self.owner = store, client, owner
        context.bind(store, client)

    def bound(self) -> contextlib.AbstractContextManager[None]:
        """This pipeline's store and client bound for a block: `with pipe.bound(): propose_fact(...)`."""
        return context.bound(self.store, self.client)

    # --- intake and extraction --------------------------------------------------------------------
    def intake(self, uri: str, text: str, publisher: str, trust: str = "trusted",
               generated_by_pipeline: bool = False) -> SourceInfo:
        return self.store.add_source(uri, text, publisher, trust, generated_by_pipeline)

    def _extract_chunk(self, chunk: ChunkInfo) -> tuple[list[Extracted], list[int]]:
        """Ask for facts one at a time until the model says there are no more."""
        out: list[Extracted] = []
        reviews: list[int] = []
        with self.bound():
            for index in range(MAX_FACTS_PER_CHUNK):
                try:
                    checked = propose_fact(chunk.id, chunk.text, chunk.source_hash, index)
                    reply = accept_fact(checked, chunk.id, chunk.source_hash)
                except inv.InvariantError as e:          # escalated or refused: a person looks at it
                    reviews.append(self.store.add_review("extraction", {
                        "chunk_id": chunk.id, "index": index, "error": str(e), "attempts": e.attempts},
                        rule=e.rule))
                    continue
                if reply.predicate in (NO_MORE, ABSTAIN):
                    break
                out.append(Extracted(chunk.id, reply.model_dump(mode="json"), checked.attempts))
        return out, reviews

    def extract(self, source_id: str) -> tuple[list[Extracted], list[int]]:
        items: list[Extracted] = []
        reviews: list[int] = []
        for chunk in self.store.chunks_for(source_id):
            got, rv = self._extract_chunk(chunk)
            items += got
            reviews += rv
        return items, reviews

    async def extract_async(self, source_id: str, concurrency: int = 4) -> tuple[list[Extracted], list[int]]:
        """Extract every chunk concurrently (worker threads), keeping chunk order in the result."""
        sem = asyncio.Semaphore(concurrency)

        async def one(chunk: ChunkInfo) -> tuple[list[Extracted], list[int]]:
            async with sem:
                return await asyncio.to_thread(self._extract_chunk, chunk)

        results = await asyncio.gather(*(one(c) for c in self.store.chunks_for(source_id)))
        items = [x for got, _ in results for x in got]
        reviews = [r for _, rv in results for r in rv]
        return items, reviews

    # --- resolution and ingest --------------------------------------------------------------------
    def resolve(self, mention: str) -> str | None:
        """Entity id for a mention: mint on first sight, None when ambiguous (a person decides)."""
        ids = self.store.candidates(mention)
        if not ids:
            return self.store.add_entity(mention)
        return ids[0] if len(ids) == 1 else None

    def build_proposal(self, x: Extracted, subject_id: str, object_value: str) -> Proposal:
        """Code builds the fact: absolute offsets, computed id, retry count."""
        chunk = self.store.chunk(x.chunk_id)
        assert chunk is not None
        f = x.fields
        ev = EvidenceSpan(chunk.source_id, chunk.source_hash, chunk.id, chunk.start + f["start"],
                          chunk.start + f["end"], f["quote"], f["polarity"])
        fid = fact_id(subject_id, f["predicate"], object_value, f["qualifier"], f["valid_from"], f["valid_to"])
        return Proposal(fid, subject_id, f["predicate"], object_value, f["object_kind"], f["qualifier"],
                        f["valid_from"], f["valid_to"], [ev], retry_count=max(0, x.attempts - 1))

    def store_extracted(self, items: list[Extracted]) -> Report:
        rep = Report(extracted=len(items))
        for x in items:
            f = x.fields
            if f["polarity"] != "ASSERTED":          # only ASSERTED becomes a fact (7.5)
                self.store.add_claim(f["subject"], f["predicate"], f["object"], f["polarity"], x.chunk_id, f["quote"])
                rep.claims += 1
                continue
            subject = self.resolve(f["subject"])
            obj = self.resolve(f["object"]) if f["object_kind"] == "entity" else f["object"]
            if subject is None or obj is None:
                mention = f["subject"] if subject is None else f["object"]
                rep.reviews.append(self.store.add_review("entity", {
                    "mention": mention, "candidates": self.store.candidates(mention),
                    "item": dataclasses.asdict(x)}))
                continue
            rep.merge(self.ingest(self.build_proposal(x, subject, obj)))
        return rep

    def ingest(self, proposal: Proposal) -> Report:
        """Store a proposal. A K-11 route opens a Conflict with an owner, then the fact is stored beside it."""
        rep = Report()
        try:
            rep.stored.append(self.store.ingest(proposal))
        except inv.Routed:
            rival = self.store.contradiction(proposal.subject_id, proposal.predicate, proposal.object, proposal.id)
            assert rival is not None
            rep.conflicts.append(self.store.open_conflict(
                [rival.fact_id, proposal.id], self.owner,
                f"{proposal.predicate}: {rival.object} vs {proposal.object}"))
            rep.stored.append(self.store.ingest(proposal))
        except inv.Rejected as e:
            rep.rejected.append(f"{e.rule}: {e}")
            rep.reviews.append(self.store.add_review("rejected", {"fact_id": proposal.id, "error": str(e)},
                                                     rule=e.rule))
        return rep

    def process(self, uri: str, text: str, publisher: str, trust: str = "trusted") -> Report:
        """Intake, extract and store one document."""
        src = self.intake(uri, text, publisher, trust)
        items, reviews = self.extract(src.id)
        rep = self.store_extracted(items)
        rep.source_id, rep.reviews = src.id, reviews + rep.reviews
        return rep

    async def process_async(self, uri: str, text: str, publisher: str, trust: str = "trusted") -> Report:
        src = self.intake(uri, text, publisher, trust)
        items, reviews = await self.extract_async(src.id)
        rep = self.store_extracted(items)
        rep.source_id, rep.reviews = src.id, reviews + rep.reviews
        return rep

    def resolve_review(self, review_id: int, entity: str) -> Report:
        """A person rules an ambiguous mention means `entity`; every waiting item with that mention goes on."""
        review = next((r for r in self.store.reviews("entity") if r["id"] == review_id), None)
        if review is None:
            open_ids = [r["id"] for r in self.store.reviews("entity")]
            raise NoSuchReview(f"no open entity review {review_id}; "
                               + (f"open ones: {', '.join(map(str, open_ids))}" if open_ids else
                                  "none is open (a mention opens one only when it matches two or more entities)")
                               + "; `reviews` lists them")
        mention = review["payload"]["mention"]
        self.store.pin_alias(mention, entity)
        rep = Report()
        for r in self.store.reviews("entity"):
            if r["payload"]["mention"].lower() == mention.lower():
                self.store.close_review(r["id"])
                rep.merge(self.store_extracted([Extracted(**r["payload"]["item"])]))
        return rep

    # --- promotion, retraction, answers -----------------------------------------------------------
    def promote_all(self) -> list[str]:
        """Try every CHECKED fact at the promotion door; those without support stay CHECKED."""
        done = []
        for f in self.store.facts("CHECKED"):
            try:
                self.store.promote(f["id"])
                done.append(f["id"])
            except inv.Rejected:
                pass
        return done

    def audit_sample(self) -> list[str]:
        """Facts accepted only after a retry: people sample these more (2.5)."""
        return [f["id"] for f in self.store.facts() if f["retry_count"] > 0 and f["status"] != "RETRACTED"]

    def delete_source(self, source_id: str) -> RetractionReport:
        return self.store.delete_source(source_id)

    def answer(self, question: str, k: int = 3) -> dict[str, Any]:
        """Answer from retrieved chunks; every citation must be one of them (K-19)."""
        chunks = self.store.retrieve(question, k)
        ids = [c.id for c in chunks]
        try:
            with self.bound():
                checked = compose_answer(question, ids, [{"id": c.id, "text": c.text} for c in chunks])
                return {"status": "ANSWERED", **render_answer(checked, ids), "retrieved": ids}
        except inv.Escalation as e:
            return {"status": "NO_ANSWER", "text": "", "citations": [], "retrieved": ids, "reason": str(e)}
        except inv.Rejected as e:
            return {"status": "REFUSED", "text": "", "citations": [], "retrieved": ids, "reason": str(e)}
