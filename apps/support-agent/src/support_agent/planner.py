"""The planner: one model call that turns a support ticket into one action (PLAN-1).

inv.model_call parses the reply into the closed Plan schema, retries format errors with the parser
error fed back, resamples once on a violation, hands an abstention (ESCALATE_TO_HUMAN) to a person,
and checks the plan against held data: a refund never above what is left of the order, never on
another customer's order, and a credit never above the customer's tier allowance.
"""
from __future__ import annotations

import invariants as inv

from .context import AgentContext
from .fake_model import FakeAgentModel
from .models import Plan


class CheckedPlan(inv.Checked):
    """A Plan that went through PLAN-1."""

    __slots__ = ()


_model = FakeAgentModel()


def model() -> FakeAgentModel:
    return _model


def set_model(m: FakeAgentModel) -> None:
    """Swap the model (tests, demos)."""
    global _model
    _model = m


@inv.model_call("PLAN-1", schema=Plan)
def plan_action(ctx: AgentContext, ticket: str, customer_id: str, inv_feedback: str | None = None) -> CheckedPlan:
    """One model call; returns the raw reply (the decorator parses and checks it)."""
    return _model.complete(ticket, customer_id, feedback=inv_feedback)  # type: ignore[return-value]
