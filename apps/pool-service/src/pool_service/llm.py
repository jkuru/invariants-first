"""Triage's real-model path: a Claude model, a replayed trace, or a recording (APP_MODEL).

APP_MODEL picks the model triage.propose_cause talks to:

- `fake` (the app's default): FakeModelClient, the deterministic keyword stand-in the defect tests use;
- `replay`: answers from the trace file APP_TRACE, and fails with a diff on a request it does not hold;
- `anthropic`: Claude through the Messages API (needs the `anthropic` package and ANTHROPIC_API_KEY);
- `record`: Claude, with every call written to APP_TRACE.

main.py applies it to the served app; the tests choose their own default (replay) and trace files.
"""
from __future__ import annotations

import os
from typing import Protocol

from invariants.integrations.anthropic import DEFAULT_MODEL, model_for

from .fake_model import FakeModelClient

SYSTEM = """You triage failed batch jobs for a worker-pool service. You are given a job id and an \
excerpt of its log.

Reply with one JSON object and nothing else (no prose, no code fence), with exactly these fields:
- "cause": one of "OUT_OF_MEMORY", "TIMEOUT", "BAD_INPUT", "CANNOT_COMPLY"
- "summary": one sentence (at most 200 characters) on what in the log points to that cause

Answer CANNOT_COMPLY when the log does not show the cause."""


class ModelClient(Protocol):
    """What triage needs: a raw JSON reply for a prompt, given the parser feedback on a retry."""

    def complete(self, prompt: str, feedback: str | None = None) -> str: ...


def select(mode: str, trace: str | os.PathLike[str] | None = None) -> ModelClient:
    """The triage model for a mode name (replay, record, anthropic, fake)."""
    llm = model_for(mode, trace, system=SYSTEM, model=os.environ.get("APP_MODEL_ID", DEFAULT_MODEL))
    return FakeModelClient() if llm is None else llm


def from_env() -> ModelClient:
    """The triage model APP_MODEL names (default fake), with the trace file APP_TRACE."""
    return select(os.environ.get("APP_MODEL", "fake"), os.environ.get("APP_TRACE"))
