"""The planner's real-model path: a Claude model, a replayed trace, or a recording (APP_MODEL).

APP_MODEL picks the model the planner talks to:

- `fake` (the app's default): FakeAgentModel, the deterministic regex stand-in the defect tests use;
- `replay`: answers from the trace file APP_TRACE, and fails with a diff on a request it does not hold;
- `anthropic`: Claude through the Messages API (needs the `anthropic` package and ANTHROPIC_API_KEY);
- `record`: Claude, with every call written to APP_TRACE.

APP_REPLY picks how the model answers: `tool` (the default) offers the submit_plan tool, whose
input is the Plan, and the plan is the input of the model's call to it, as an agent's tool call is;
`json` asks for the Plan as one JSON object in the reply text. Either way inv.model_call parses the
reply into the closed Plan schema and checks PLAN-1. After a format failure the parser error goes
back as a tool_result with is_error true (tool) or as a user turn (json).

The tests choose their own default (replay) and trace files; see tests/conftest.py.
"""
from __future__ import annotations

import os
from typing import Protocol

from invariants.integrations.anthropic import DEFAULT_MODEL, ChatModel, model_for, reply_tool

from .fake_model import FakeAgentModel
from .models import Plan

REPLIES = ("tool", "json")

FIELDS = """- "action": one of "REFUND", "CREDIT", "CANCEL", "ESCALATE_TO_HUMAN"
- "customer_id": the customer id you were given
- "order_id": the order to refund (like "O-1") for REFUND, otherwise ""
- "amount_cents": an integer; amounts in tickets are already in cents; 0 for CANCEL and ESCALATE_TO_HUMAN
- "reason": one short sentence"""

SYSTEM = f"""You are the planning step of a billing support agent. Read one support ticket and choose \
exactly one action.

Reply with one JSON object and nothing else (no prose, no code fence), with exactly these fields:
{FIELDS}

Choose ESCALATE_TO_HUMAN when the ticket is unclear or asks for anything else."""

TOOL_SYSTEM = f"""You are the planning step of a billing support agent. Read one support ticket and choose \
exactly one action.

Call the submit_plan tool exactly once with the action. Its input has exactly these fields:
{FIELDS}

Choose ESCALATE_TO_HUMAN when the ticket is unclear or asks for anything else."""

PLAN_TOOL = reply_tool("submit_plan", Plan, description=(
    "Submit the one action the agent takes for this support ticket: a refund, a credit, a cancellation, "
    "or a handoff to a person (ESCALATE_TO_HUMAN)."))


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


def select(mode: str, trace: str | os.PathLike[str] | None = None, *, reply: str = "tool") -> AgentModel:
    """The planner model for a mode name (replay, record, anthropic, fake), answering by tool call
    (reply="tool") or as JSON text (reply="json")."""
    if reply not in REPLIES:
        raise ValueError(f"unknown reply style {reply!r}; pick from {', '.join(REPLIES)}")
    model = os.environ.get("APP_MODEL_ID", DEFAULT_MODEL)
    if reply == "tool":
        llm = model_for(mode, trace, system=TOOL_SYSTEM, model=model, tool=PLAN_TOOL)
    else:
        llm = model_for(mode, trace, system=SYSTEM, model=model)
    return FakeAgentModel() if llm is None else LLMPlanner(llm)


def from_env() -> AgentModel:
    """The planner model APP_MODEL names (default fake), with the trace file APP_TRACE and the reply
    style APP_REPLY (default tool)."""
    return select(os.environ.get("APP_MODEL", "fake"), os.environ.get("APP_TRACE"),
                  reply=os.environ.get("APP_REPLY", "tool"))
