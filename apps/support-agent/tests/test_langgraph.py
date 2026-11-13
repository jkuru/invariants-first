"""The agent as a LangGraph StateGraph (the `langgraph` extra; skipped when it is not installed).

The planner node runs the @inv.model_call, the act node hands the CheckedPlan in graph state to the
@inv.edge, and the AgentContext is LangGraph's runtime context (graph.invoke(..., context=ctx)).
"""
from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

import invariants as inv  # noqa: E402
from invariants.testing import make_checked  # noqa: E402
from support_agent import fakestripe, langgraph_agent as lg, llm, planner  # noqa: E402
from support_agent.context import AgentContext  # noqa: E402
from support_agent.fake_model import FakeAgentModel  # noqa: E402
from support_agent.models import Plan  # noqa: E402

TRACES = pathlib.Path(__file__).resolve().parent / "fixtures" / "traces"


def ticket(text: str, customer: str) -> dict[str, Any]:
    return {"ticket": text, "customer_id": customer}


def test_a_refund_through_the_state_graph(ctx: AgentContext) -> None:
    out = lg.build().invoke(ticket("please refund 1200 on O-1", "C-1"), context=ctx)
    assert out["trace"] == ["triage", "planner", "act", "done"] and out["result"] == "re_1"
    assert isinstance(out["plan"], planner.CheckedPlan) and fakestripe.REFUNDS[0]["amount"] == 1200


def test_the_state_graph_on_a_replayed_tool_call(ctx: AgentContext) -> None:
    """The same hand-written tool_use trace the plain flow replays: the requests are the same."""
    m = llm.select("replay", TRACES / "tool_refund.json", reply="tool")
    planner.set_model(m)
    out = lg.build().invoke(ticket("please refund 1200 on O-1", "C-1"), context=ctx)
    assert out["result"] == "re_1"
    assert isinstance(m, llm.LLMPlanner)
    m.llm.assert_done()                                          # type: ignore[attr-defined]


def test_an_over_refund_is_handed_off_in_the_graph(ctx: AgentContext, model: FakeAgentModel) -> None:
    model.inject("overrefund", "overrefund")
    out = lg.build().invoke(ticket("refund 100 on O-2", "C-1"), context=ctx)
    assert out["trace"] == ["triage", "planner", "handoff"] and out["review_id"] is not None
    assert fakestripe.REFUNDS == []


def test_a_node_that_swaps_the_plan_in_state_is_stopped_at_the_edge(ctx: AgentContext) -> None:
    """A node between the planner and act replaces the checked plan with one for another customer's
    order (O-3 is C-2's; the door alone would allow it). The edge re-checks PLAN-1 and refuses."""
    def swap(state: lg.AgentState) -> dict[str, Any]:
        forged = make_checked(planner.CheckedPlan, Plan(action="REFUND", customer_id="C-1", order_id="O-3",
                                                        amount_cents=100, reason="r"))
        return {"plan": forged, "trace": ["swap"]}

    g: Any = StateGraph(lg.AgentState, context_schema=AgentContext)
    for name, fn in (("triage", lg.triage), ("planner", lg.planner), ("swap", swap), ("act", lg.act),
                     ("handoff", lg.handoff), ("done", lg.done)):
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "planner")
    g.add_edge("planner", "swap")
    g.add_edge("swap", "act")
    g.add_conditional_edges("act", lg.after_act)
    g.add_edge("handoff", END)
    g.add_edge("done", END)
    out = g.compile().invoke(ticket("refund 100 on O-2", "C-1"), context=ctx)
    assert out["trace"] == ["triage", "planner", "swap", "act", "handoff"]
    assert "another customer's order" in out["handoff_reason"] and fakestripe.REFUNDS == []


def test_a_plan_restored_from_a_checkpoint_is_not_run(ctx: AgentContext) -> None:
    """With a checkpointer and an interrupt before act, the plan comes back as a dict: not a
    CheckedPlan, so it goes to a person, and nothing is refunded."""
    app = lg.builder().compile(checkpointer=InMemorySaver(), interrupt_before=["act"])
    cfg: Any = {"configurable": {"thread_id": "t-1"}}
    app.invoke(ticket("refund 100 on O-2", "C-1"), cfg, context=ctx)
    assert isinstance(app.get_state(cfg).values["plan"], dict)
    out = app.invoke(None, cfg, context=ctx)
    assert out["trace"][-2:] == ["act", "handoff"] and "not a CheckedPlan" in out["handoff_reason"]
    assert fakestripe.REFUNDS == []


def test_ainvoke_runs_the_sync_nodes_in_worker_threads(ctx: AgentContext, model: FakeAgentModel) -> None:
    """ainvoke runs a sync node in a worker thread with a copy of the caller's context, so a model
    call's retries (one conversation per invocation) and its provider reads stay in that thread."""
    model.inject("malformed")
    out = asyncio.run(lg.build().ainvoke(ticket("credit 300 please", "C-1"), context=ctx))
    assert out["result"] == "credited 300 to C-1" and out["plan"].attempts == 2
    assert model.calls[1]["feedback"]


def test_a_dependency_bound_around_ainvoke_reaches_the_nodes(ctx: AgentContext) -> None:
    """inv.Dependency is context-local: bound around ainvoke, the sync nodes (worker threads) and
    async nodes (tasks) see it; bound inside one node, the next node does not."""
    tenant: inv.Dependency[str] = inv.Dependency("tenant")
    seen: list[str] = []

    def sync_node(state: lg.AgentState) -> dict[str, Any]:
        seen.append(tenant.get())
        tenant.set("set-in-a-node")
        return {"trace": ["sync"]}

    async def async_node(state: lg.AgentState) -> dict[str, Any]:
        seen.append(tenant.get())
        return {"trace": ["async"]}

    g: Any = StateGraph(lg.AgentState)
    g.add_node("sync_node", sync_node)
    g.add_node("async_node", async_node)
    g.add_edge(START, "sync_node")
    g.add_edge("sync_node", "async_node")
    g.add_edge("async_node", END)

    async def run() -> None:
        with tenant.bound("acme"):
            await g.compile().ainvoke({"ticket": "", "customer_id": ""})
    asyncio.run(run())
    assert seen == ["acme", "acme"]
