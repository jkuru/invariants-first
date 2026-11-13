"""Answering: retrieve chunks, let the model answer, refuse any citation that was not retrieved (K-19)."""
from __future__ import annotations

import invariants as inv

from .context import current_client
from .models import AnswerReply


class CheckedAnswer(inv.Checked):
    """An AnswerReply that passed parse, abstain and K-19."""


@inv.model_call("K-19", schema=AnswerReply, returns=CheckedAnswer)
def compose_answer(question: str, retrieved_ids: list[str], context: list[dict[str, str]],
                   inv_feedback: str | None = None) -> str:
    """Ask the model to answer from the retrieved chunks only."""
    return current_client().complete({"task": "answer", "question": question, "chunks": context},
                                     inv_feedback)


@inv.edge("K-19")
def render_answer(answer: CheckedAnswer, retrieved_ids: list[str]) -> dict[str, object]:
    """The consumer of an answer: re-checks K-19, then renders text with its citations."""
    return {"text": answer.text, "citations": list(answer.citations)}
