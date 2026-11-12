"""Typed records: the model's reply schemas (closed) and the values code builds."""
from __future__ import annotations

import dataclasses
from typing import Literal

from pydantic import BaseModel, ConfigDict

# The closed vocabulary (K-4). invariants.yaml repeats it in K-4's ingest clause;
# tests/test_rules.py checks the two lists agree.
PREDICATES: tuple[str, ...] = ("acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in")
FUNCTIONAL: tuple[str, ...] = ("acquisition_price", "ceo_of", "headquartered_in")   # one value per subject (K-11)
NO_MORE = "NONE"              # the model has no further fact in this chunk
ABSTAIN = "CANNOT_COMPLY"     # the model cannot do the task: escalates at once

Predicate = Literal["acquired", "acquisition_price", "in_talks", "ceo_of", "headquartered_in",
                    "NONE", "CANNOT_COMPLY"]
Polarity = Literal["ASSERTED", "DENIED", "HYPOTHETICAL", "REPORTED"]
Qualifier = Literal["EXACT", "APPROX", "UPPER_BOUND", "LOWER_BOUND", "NONE"]


class FactReply(BaseModel):
    """One proposed fact from the model. No id fields: the model cannot supply one (K-18)."""

    model_config = ConfigDict(extra="forbid")
    subject: str
    predicate: Predicate
    object: str
    object_kind: Literal["entity", "literal"]
    qualifier: Qualifier
    polarity: Polarity
    quote: str
    start: int            # offsets into the chunk text the model was given
    end: int
    valid_from: str | None
    valid_to: str | None


class AnswerReply(BaseModel):
    """The model's answer to a question: text plus the chunk ids it cites."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ANSWERED", "CANNOT_ANSWER"]
    text: str
    citations: list[str]


@dataclasses.dataclass(frozen=True)
class SourceInfo:
    """A source as intake recorded it."""

    id: str
    uri: str
    hash: str
    publisher_root: str
    trust: str
    generated_by_pipeline: bool
    deleted: bool


@dataclasses.dataclass(frozen=True)
class ChunkInfo:
    """A chunk (one sentence) of a source version; source_hash_current is the source's hash now."""

    id: str
    source_id: str
    source_hash: str
    source_hash_current: str
    start: int
    text: str


@dataclasses.dataclass(frozen=True)
class EvidenceSpan:
    """Where a fact was read: absolute offsets into the source text."""

    source_id: str
    source_hash: str
    chunk_id: str
    start: int
    end: int
    quote: str
    polarity: str


@dataclasses.dataclass(frozen=True)
class Proposal:
    """A fact built by code from a checked reply; id is computed, never supplied."""

    id: str
    subject_id: str
    predicate: str
    object: str
    object_kind: str
    qualifier: str
    valid_from: str | None
    valid_to: str | None
    evidence: list[EvidenceSpan]
    retry_count: int = 0


@dataclasses.dataclass(frozen=True)
class Contradiction:
    """An existing live value for a functional property, and whether a Conflict records it."""

    fact_id: str
    object: str
    conflict_recorded: bool


@dataclasses.dataclass(frozen=True)
class Support:
    """What a fact rests on: its status and the distinct trusted publishers behind it."""

    fact_id: str
    status: str
    trusted_publishers: list[str]


@dataclasses.dataclass(frozen=True)
class RetractionReport:
    """What deleting a source did; live_orphans is re-counted after the retraction."""

    source_id: str
    retracted: list[str]
    demoted: list[str]
    live_orphans: int
