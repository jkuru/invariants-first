"""The agent graph: triage -> planner -> router -> tool executor, or human handoff.

A small graph runner in the style of LangGraph: nodes are registered as function values and the
router picks the next node from the state. The executor is an @inv.edge: it takes only a CheckedPlan,
re-validates it against the closed schema and re-checks PLAN-1 on what it receives, so a forged plan
or one changed after the planner checked it never reaches a tool.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

import invariants as inv

from .context import AgentContext
from .planner import CheckedPlan, plan_action
from .tools import TOOLS


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


Node = Callable[[AgentContext, State], str | None]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.start = ""

    def add_node(self, name: str, fn: Node) -> None:
        self.nodes[name] = fn
        self.start = self.start or name

    def run(self, ctx: AgentContext, state: State, limit: int = 10) -> State:
        node: str | None = self.start
        while node is not None and limit:
            state.trace.append(node)
            node = self.nodes[node](ctx, state)
            limit -= 1
        return state


def triage_node(ctx: AgentContext, state: State) -> str:
    t = state.ticket.lower()
    state.topic = "billing" if any(w in t for w in ("refund", "credit", "cancel", "charge")) else "other"
    return "planner" if state.topic == "billing" else "handoff"


def planner_node(ctx: AgentContext, state: State) -> str:
    try:
        state.plan = plan_action(ctx, state.ticket, state.customer_id)
    except (inv.Escalation, inv.Routed, inv.ReviewNotOpened) as e:
        state.review_id, state.handoff_reason = getattr(e, "review_id", None), str(e)
        return "handoff"
    except inv.Rejected as e:
        state.handoff_reason = str(e)
        return "handoff"
    return "execute"


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


def tool_executor_node(ctx: AgentContext, state: State) -> str:
    assert state.plan is not None
    try:
        state.result = execute_plan(ctx, state.plan, state.customer_id)
    except inv.InvariantError as e:
        state.handoff_reason = str(e)
        return "handoff"
    return "done"


def human_handoff_node(ctx: AgentContext, state: State) -> None:
    state.result = None
    return None


def done_node(ctx: AgentContext, state: State) -> None:
    return None


def build() -> Graph:
    g = Graph()
    g.add_node("triage", triage_node)
    g.add_node("planner", planner_node)
    g.add_node("execute", tool_executor_node)
    g.add_node("handoff", human_handoff_node)
    g.add_node("done", done_node)
    return g


def run_ticket(ctx: AgentContext, ticket: str, customer_id: str) -> State:
    return build().run(ctx, State(ticket, customer_id))


def handle(ctx: AgentContext, ticket: str, customer_id: str) -> dict[str, Any]:
    """One ticket, as a JSON-ready summary (for the CLI and demos)."""
    s = run_ticket(ctx, ticket, customer_id)
    return {"trace": s.trace, "result": s.result, "review_id": s.review_id, "handoff": s.handoff_reason}
