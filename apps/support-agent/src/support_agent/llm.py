"""The planner's real-model path: a Claude model, a replayed trace, or a recording (APP_MODEL).

APP_MODEL picks the model the planner talks to:

- `fake` (the app's default): FakeAgentModel, the deterministic regex stand-in the defect tests use;
- `replay`: answers from the trace file APP_TRACE, and fails with a diff on a request it does not hold;
- `anthropic`: Claude through the Messages API (needs the `anthropic` package and ANTHROPIC_API_KEY);
- `record`: Claude, with every call written to APP_TRACE.

The tests choose their own default (replay) and trace files; see tests/conftest.py.
"""
from __future__ import annotations

import os
from typing import Protocol

from invariants.integrations.anthropic import DEFAULT_MODEL, ChatModel, model_for

from .fake_model import FakeAgentModel

SYSTEM = """You are the planning step of a billing support agent. Read one support ticket and choose \
exactly one action.

Reply with one JSON object and nothing else (no prose, no code fence), with exactly these fields:
- "action": one of "REFUND", "CREDIT", "CANCEL", "ESCALATE_TO_HUMAN"
- "customer_id": the customer id you were given
- "order_id": the order to refund (like "O-1") for REFUND, otherwise ""
- "amount_cents": an integer; amounts in tickets are already in cents; 0 for CANCEL and ESCALATE_TO_HUMAN
- "reason": one short sentence

Choose ESCALATE_TO_HUMAN when the ticket is unclear or asks for anything else."""


class AgentModel(Protocol):
    """What the planner needs: a raw JSON reply for a ticket, given the parser feedback on a retry."""

    def complete(self, ticket: str, customer_id: str, feedback: str | None = None) -> str: ...


def prompt_for(ticket: str, customer_id: str) -> str:
    return f"Customer id: {customer_id}\nTicket: {ticket}"


class LLMPlanner:
    """An AgentModel over an integrations.anthropic model (AnthropicModel, ReplayModel, RecordingModel)."""

    def __init__(self, llm: ChatModel) -> None:
        self.llm = llm

    def complete(self, ticket: str, customer_id: str, feedback: str | None = None) -> str:
        return self.llm.complete(prompt_for(ticket, customer_id), feedback)


def select(mode: str, trace: str | os.PathLike[str] | None = None) -> AgentModel:
    """The planner model for a mode name (replay, record, anthropic, fake)."""
    llm = model_for(mode, trace, system=SYSTEM, model=os.environ.get("APP_MODEL_ID", DEFAULT_MODEL))
    return FakeAgentModel() if llm is None else LLMPlanner(llm)


def from_env() -> AgentModel:
    """The planner model APP_MODEL names (default fake), with the trace file APP_TRACE."""
    return select(os.environ.get("APP_MODEL", "fake"), os.environ.get("APP_TRACE"))
