"""The agent: triage -> planner -> executor, or a human handoff.

The steps are plain functions, so any agent framework can call them: run_ticket() calls them in
order, and support_agent.langgraph_agent wires the same steps into a LangGraph StateGraph (the
`langgraph` extra). The planner is an @inv.model_call (planner.plan_action) and the executor an
@inv.edge (execute_plan): it takes only a CheckedPlan, re-validates it against the closed schema
and re-checks PLAN-1 on what it receives, so a forged plan, or one changed after the planner checked
it, never reaches a tool.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import invariants as inv

from .context import AgentContext
from .planner import CheckedPlan, plan_action
from .tools import TOOLS

BILLING_WORDS = ("refund", "credit", "cancel", "charge")


@dataclasses.dataclass
class State:
    ticket: str
    customer_id: str
    topic: str = ""
    plan: CheckedPlan | None = None
    result: str | None = None
    review_id: str | None = None
    handoff_reason: str | None = None
    trace: list[str] = dataclasses.field(default_factory=list)


def topic_of(ticket: str) -> str:
    """billing or other: only billing tickets reach the planner."""
    t = ticket.lower()
    return "billing" if any(w in t for w in BILLING_WORDS) else "other"


@dataclasses.dataclass(frozen=True)
class Planned:
    """What the planner step produced: a checked plan, or why a person takes the ticket."""

    plan: CheckedPlan | None = None
    review_id: str | None = None
    handoff_reason: str | None = None


def plan_step(ctx: AgentContext, ticket: str, customer_id: str) -> Planned:
    """Run the planner (the model call); an escalation, route or rejection becomes a handoff."""
    try:
        return Planned(plan=plan_action(ctx, ticket, customer_id))
    except (inv.Escalation, inv.Routed, inv.ReviewNotOpened) as e:
        return Planned(review_id=getattr(e, "review_id", None), handoff_reason=str(e))
    except inv.Rejected as e:
        return Planned(handoff_reason=str(e))


@inv.edge("PLAN-1")
def execute_plan(ctx: AgentContext, plan: CheckedPlan, customer_id: str) -> str:
    """Run the one tool the checked plan names, for the ticket's customer (its input model is validated
    first). The edge re-checks PLAN-1 against this customer and the ledger as it is now."""
    model, tool = TOOLS[plan.action]
    if plan.action == "REFUND":
        args = {"order_id": plan.order_id, "amount_cents": plan.amount_cents, "reason": plan.reason}
    elif plan.action == "CREDIT":
        args = {"customer_id": plan.customer_id, "amount_cents": plan.amount_cents, "reason": plan.reason}
    else:
        args = {"customer_id": plan.customer_id, "reason": plan.reason}
    return tool(ctx, model(**args))


def act_step(ctx: AgentContext, plan: CheckedPlan, customer_id: str) -> tuple[str | None, str | None]:
    """(the tool's result, None), or (None, why a person takes the ticket) when the edge or a door refuses."""
    try:
        return execute_plan(ctx, plan, customer_id), None
    except inv.InvariantError as e:
        return None, str(e)


def run_ticket(ctx: AgentContext, ticket: str, customer_id: str) -> State:
    """One ticket through the steps; state.trace names the steps taken."""
    s = State(ticket, customer_id)
    s.trace.append("triage")
    s.topic = topic_of(ticket)
    if s.topic == "billing":
        s.trace.append("planner")
        planned = plan_step(ctx, ticket, customer_id)
        s.plan, s.review_id, s.handoff_reason = planned.plan, planned.review_id, planned.handoff_reason
        if s.plan is not None:
            s.trace.append("execute")
            s.result, s.handoff_reason = act_step(ctx, s.plan, customer_id)
            if s.handoff_reason is None:
                s.trace.append("done")
                return s
    s.trace.append("handoff")
    s.result = None
    return s


def handle(ctx: AgentContext, ticket: str, customer_id: str) -> dict[str, Any]:
    """One ticket, as a JSON-ready summary (for the CLI and demos)."""
    s = run_ticket(ctx, ticket, customer_id)
    return {"trace": s.trace, "result": s.result, "review_id": s.review_id, "handoff": s.handoff_reason}
