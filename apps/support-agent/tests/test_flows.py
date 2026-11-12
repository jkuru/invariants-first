"""The agent doing its job: refunds, credits, cancellations and handoffs on honest model replies."""
from __future__ import annotations

from support_agent import fakestripe, graph
from support_agent.context import AgentContext


def test_a_refund_runs_triage_planner_tool_and_reaches_the_gateway(ctx: AgentContext) -> None:
    s = graph.run_ticket(ctx, "please refund 1200 on O-1", "C-1")
    assert s.trace == ["triage", "planner", "execute", "done"] and s.result == "re_1"
    assert fakestripe.REFUNDS == [{"id": "re_1", "charge": "ch_O-1", "amount": 1200, "metadata": {"order_id": "O-1"}}]
    assert ctx.ledger.order("O-1")["refunded"] == 1200


def test_a_credit_and_a_cancellation(ctx: AgentContext) -> None:
    assert graph.run_ticket(ctx, "credit 300 please", "C-1").result == "credited 300 to C-1"
    assert ctx.ledger.credits("C-1") == 300
    assert graph.run_ticket(ctx, "cancel my plan", "C-2").result == "cancelled C-2"
    assert ctx.ledger.customer("C-2")["subscribed"] == 0


def test_a_ticket_that_is_not_billing_goes_to_a_person(ctx: AgentContext) -> None:
    s = graph.run_ticket(ctx, "my password does not work", "C-1")
    assert s.trace == ["triage", "handoff"] and s.result is None


def test_refunds_add_up_to_at_most_the_order(ctx: AgentContext) -> None:
    for _ in range(3):
        graph.run_ticket(ctx, "refund 1000 on O-2", "C-1")      # O-2 is 3000
    s = graph.run_ticket(ctx, "refund 1000 on O-2", "C-1")      # a fourth would exceed it
    assert s.trace[-1] == "handoff" and ctx.ledger.order("O-2")["refunded"] == 3000
    assert len(fakestripe.REFUNDS) == 3


def test_handle_summarises_a_ticket(ctx: AgentContext) -> None:
    out = graph.handle(ctx, "refund 100 on O-1", "C-1")
    assert out == {"trace": ["triage", "planner", "execute", "done"], "result": "re_1", "review_id": None,
                   "handoff": None}
