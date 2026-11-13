"""The happy-path flows on a real-model path: a replayed trace by default (APP_MODEL=replay).

The committed traces in tests/fixtures/traces/ are hand-written fixtures in the shape of a recording,
not recordings (see the README there). `pytest --record` with ANTHROPIC_API_KEY set replaces them
with real ones, marked "fixture": "recorded".
"""
from __future__ import annotations

import pytest

from support_agent import fakestripe, graph
from support_agent.context import AgentContext


def test_a_refund_on_the_model(ctx: AgentContext, traced) -> None:
    traced("refund")
    s = graph.run_ticket(ctx, "please refund 1200 on O-1", "C-1")
    assert s.trace == ["triage", "planner", "execute", "done"] and s.result == "re_1"
    assert fakestripe.REFUNDS[0]["amount"] == 1200 and ctx.ledger.order("O-1")["refunded"] == 1200


def test_a_credit_and_a_cancellation_on_the_model(ctx: AgentContext, traced) -> None:
    traced("credit_and_cancel")
    assert graph.run_ticket(ctx, "credit 300 please", "C-1").result == "credited 300 to C-1"
    assert graph.run_ticket(ctx, "cancel my plan", "C-2").result == "cancelled C-2"
    assert ctx.ledger.customer("C-2")["subscribed"] == 0


def test_an_unclear_billing_ticket_is_handed_to_a_person(ctx: AgentContext, traced) -> None:
    traced("escalate")
    s = graph.run_ticket(ctx, "I think I was charged twice last month?", "C-2")
    assert s.trace == ["triage", "planner", "handoff"] and s.review_id is not None
    assert fakestripe.REFUNDS == []


def test_a_fenced_reply_is_fixed_on_the_second_turn(ctx: AgentContext, traced, model_mode: str) -> None:
    """The first reply wraps the JSON in prose and a code fence; the parser error goes back as a
    follow-up turn in the same conversation, and the second reply is the bare object."""
    if model_mode == "record":
        pytest.skip("a scripted format failure: a recording would replace it with whatever the model does")
    traced("refund_format_fix")
    s = graph.run_ticket(ctx, "refund 450 on O-2 please, the item arrived broken", "C-1")
    assert s.result == "re_1" and fakestripe.REFUNDS[0]["amount"] == 450
    if model_mode == "replay":
        assert s.plan is not None and s.plan.attempts == 2
