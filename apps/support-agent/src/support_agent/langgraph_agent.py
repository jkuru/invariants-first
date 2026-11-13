"""The agent as a LangGraph StateGraph (needs the `langgraph` extra: pip install -e '.[langgraph]').

    triage -> planner -> act -> done
                 \\         \\
                  +---------+--> handoff

The planner node runs the @inv.model_call (planner.plan_action) and puts the CheckedPlan in the
graph state; the act node hands state["plan"] to the @inv.edge (graph.execute_plan), which
re-validates it and re-checks PLAN-1, so a plan that another node or a state update changed after
the planner checked it never reaches a tool. A checkpointer restores the plan as a plain dict (it
does not serialise the Checked), which is not a CheckedPlan: the act node hands that ticket to a
person rather than run an unchecked value. The AgentContext (the ledger
and the doors) is LangGraph's runtime context: graph.invoke(state, context=ctx); the rules' providers
read it as args.ctx, an explicit argument of the model call and the edge.

    app = build()
    out = app.invoke({"ticket": "refund 1200 on O-1", "customer_id": "C-1"}, context=ctx)
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .context import AgentContext
from .graph import act_step, plan_step, topic_of
from .planner import CheckedPlan


class AgentState(TypedDict, total=False):
    ticket: str
    customer_id: str
    topic: str
    plan: CheckedPlan | None
    result: str | None
    review_id: str | None
    handoff_reason: str | None
    trace: Annotated[list[str], operator.add]


def triage(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    return {"topic": topic_of(state["ticket"]), "trace": ["triage"]}


def planner(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """The model call: a CheckedPlan in state, or the reason a person takes the ticket."""
    p = plan_step(runtime.context, state["ticket"], state["customer_id"])
    return {"plan": p.plan, "review_id": p.review_id, "handoff_reason": p.handoff_reason, "trace": ["planner"]}


def act(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """The edge consumes the plan in state: execute_plan(ctx, plan, customer_id) re-checks it first."""
    plan = state.get("plan")
    if not isinstance(plan, CheckedPlan):
        # A checkpointer stores the plan as plain data and restores a dict, which the edge's type
        # gate refuses (TypeError): the restored value was never checked. A person takes it.
        return {"handoff_reason": f"the plan in state is not a CheckedPlan ({type(plan).__name__}); plan again",
                "trace": ["act"]}
    result, why = act_step(runtime.context, plan, state["customer_id"])
    return {"result": result, "handoff_reason": why, "trace": ["act"]}


def handoff(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    return {"result": None, "trace": ["handoff"]}


def done(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    return {"trace": ["done"]}


def after_triage(state: AgentState) -> Literal["planner", "handoff"]:
    return "planner" if state["topic"] == "billing" else "handoff"


def after_planner(state: AgentState) -> Literal["act", "handoff"]:
    return "act" if state.get("plan") is not None else "handoff"


def after_act(state: AgentState) -> Literal["done", "handoff"]:
    return "done" if state.get("handoff_reason") is None else "handoff"


def builder() -> StateGraph[AgentState, AgentContext, AgentState, AgentState]:
    """The uncompiled graph, to compile with your own checkpointer or interrupts."""
    g: StateGraph[AgentState, AgentContext, AgentState, AgentState] = StateGraph(
        AgentState, context_schema=AgentContext)
    for name, fn in (("triage", triage), ("planner", planner), ("act", act), ("handoff", handoff),
                     ("done", done)):
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", after_triage)
    g.add_conditional_edges("planner", after_planner)
    g.add_conditional_edges("act", after_act)
    g.add_edge("handoff", END)
    g.add_edge("done", END)
    return g


def build() -> Any:
    """The compiled graph: .invoke(state, context=ctx) or await .ainvoke(state, context=ctx)."""
    return builder().compile()
