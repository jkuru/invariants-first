"""Extraction: the model proposes one fact per call; code checks (K-2) and decides."""
from __future__ import annotations

import invariants as inv

from .context import current_client
from .models import FactReply


class CheckedFact(inv.Checked):
    """A FactReply that passed parse, abstain and K-2. Built only by propose_fact."""


@inv.model_call("K-2", schema=FactReply, returns=CheckedFact)
def propose_fact(chunk_id: str, text: str, source_hash: str, index: int,
                 inv_feedback: str | None = None) -> str:
    """Ask the model for the index-th fact in one chunk. Returns the raw reply; the decorator checks it."""
    return current_client().complete({"task": "extract", "text": text, "index": index}, inv_feedback)


@inv.edge("K-2")
def accept_fact(fact: CheckedFact, chunk_id: str, source_hash: str) -> FactReply:
    """The consumer of a CheckedFact: re-checks K-2 on what it receives, then hands on the typed reply."""
    return fact.value  # type: ignore[no-any-return]
