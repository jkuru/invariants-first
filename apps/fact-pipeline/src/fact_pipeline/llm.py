"""The extraction and answer calls' real-model path: Claude, a replayed trace, or a recording (APP_MODEL).

APP_MODEL picks the model client the pipeline uses:

- `fake` (the CLI's default): FakeModel, the deterministic regex stand-in the defect tests use;
- `replay`: answers from the trace file APP_TRACE, and fails with a diff on a request it does not hold;
- `anthropic`: Claude through the Messages API (needs the `anthropic` package and ANTHROPIC_API_KEY);
- `record`: Claude, with every call written to APP_TRACE.

The tests choose their own default (replay) and trace files; see tests/conftest.py.
"""
from __future__ import annotations

import os
from typing import Any

from invariants.integrations.anthropic import DEFAULT_MODEL, ChatModel, model_for

from .fake_model import FakeModel, ModelClient
from .models import PREDICATES

SYSTEM = ("You are one step of a pipeline that turns news text into cited facts and answers questions from "
          "it. Each message is one task. Reply with one JSON object and nothing else (no prose, no code fence).")

EXTRACT = """Task: extract one fact from the text below.

Return fact number {n} in reading order (counting from 1) as a JSON object with exactly these fields:
- "subject": the entity the fact is about, as written in the text
- "predicate": one of {predicates}
- "object": the other entity as written (object_kind "entity"), or a literal value (object_kind "literal"); \
an acquisition price is "USD <integer>", like "USD 2100000000"
- "object_kind": "entity" or "literal"
- "qualifier": "EXACT", "APPROX" ("about"), "UPPER_BOUND" ("up to"), "LOWER_BOUND" ("at least"), or "NONE" \
when the fact has no amount
- "polarity": "ASSERTED", "DENIED", "HYPOTHETICAL" or "REPORTED"
- "quote": the exact words of the text the fact rests on, copied character for character
- "start", "end": character offsets of the quote in the text, so that text[start:end] == quote
- "valid_from", "valid_to": "YYYY-MM-DD" dates the text gives for when the fact held, else null

When the text has fewer than {n} facts, answer with predicate "NONE", empty strings for subject, object and \
quote, object_kind "literal", qualifier "NONE", polarity "ASSERTED", start 0, end 0 and null dates. \
Answer with predicate "CANNOT_COMPLY" when you cannot do the task.

Text:
{text}"""

ANSWER = """Task: answer the question from the numbered chunks only.

Reply with exactly these fields:
- "status": "ANSWERED", or "CANNOT_ANSWER" when the chunks do not answer it
- "text": the answer in one sentence ("" when CANNOT_ANSWER)
- "citations": a list of the ids of the chunks the answer rests on ([] when CANNOT_ANSWER)

Question: {question}

Chunks:
{chunks}"""


def prompt_for(task: dict[str, Any]) -> str:
    """The user message for an extract or answer task (the same task dict the fake model reads)."""
    if task.get("task") == "extract":
        return EXTRACT.format(n=int(task["index"]) + 1, predicates=", ".join(f'"{p}"' for p in PREDICATES),
                              text=task["text"])
    if task.get("task") == "answer":
        chunks = "\n".join(f"[{c['id']}] {c['text']}" for c in task["chunks"])
        return ANSWER.format(question=task["question"], chunks=chunks)
    raise ValueError(f"unknown task {task.get('task')!r}")


class LLMClient:
    """A ModelClient over an integrations.anthropic model (AnthropicModel, ReplayModel, RecordingModel)."""

    def __init__(self, llm: ChatModel) -> None:
        self.llm = llm

    def complete(self, task: dict[str, Any], feedback: str | None = None) -> str:
        return self.llm.complete(prompt_for(task), feedback)

    async def acomplete(self, task: dict[str, Any], feedback: str | None = None) -> str:
        return await self.llm.acomplete(prompt_for(task), feedback)


def select(mode: str, trace: str | os.PathLike[str] | None = None) -> ModelClient:
    """The model client for a mode name (replay, record, anthropic, fake)."""
    llm = model_for(mode, trace, system=SYSTEM, model=os.environ.get("APP_MODEL_ID", DEFAULT_MODEL))
    return FakeModel() if llm is None else LLMClient(llm)


def from_env() -> ModelClient:
    """The model client APP_MODEL names (default fake), with the trace file APP_TRACE."""
    return select(os.environ.get("APP_MODEL", "fake"), os.environ.get("APP_TRACE"))
