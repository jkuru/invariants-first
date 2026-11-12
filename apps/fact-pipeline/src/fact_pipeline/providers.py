"""Read-only providers the rules bind (declared under providers: in invariants.yaml)."""
from __future__ import annotations

import invariants as inv

from .context import current_store
from .models import ChunkInfo, Contradiction, Support
from .store import Store


@inv.provider("subject_known")
def subject_known(store: Store, entity_id: str) -> bool:
    """True when the resolver minted this subject id (K-18)."""
    return store.entity_known(entity_id)


@inv.provider("object_known")
def object_known(store: Store, entity_id: str) -> bool:
    """True when the resolver minted this object id (K-18)."""
    return store.entity_known(entity_id)


@inv.provider("contradiction")
def contradiction(store: Store, subject_id: str, predicate: str, obj: str, fact_id: str) -> Contradiction | None:
    """Another live value for the same subject and predicate (K-11)."""
    return store.contradiction(subject_id, predicate, obj, fact_id)


@inv.provider("support")
def support(store: Store, fact_id: str) -> Support:
    """The fact's status and distinct trusted publishers (K-12)."""
    return store.support(fact_id)


@inv.provider("stale_citations")
def stale_citations(store: Store, fact_id: str) -> int:
    """Evidence rows citing a superseded source version (K-16)."""
    return store.stale_citations(fact_id)


@inv.provider("orphans")
def orphans(store: Store, source_id: str) -> int:
    """Live facts resting only on deleted sources, among this source's facts (K-15)."""
    return store.orphans(source_id)


@inv.provider("chunk")
def chunk(chunk_id: str) -> ChunkInfo | None:
    """The chunk the model read, with its source's current hash (K-2)."""
    return current_store().chunk(chunk_id)


@inv.provider("span")
def span(chunk_id: str, start: int, end: int) -> str | None:
    """The chunk text at the model's offsets, or None when they fall outside it (K-2)."""
    c = current_store().chunk(chunk_id)
    if c is None or not 0 <= start <= end <= len(c.text):
        return None
    return c.text[start:end]
