"""The planner answering by tool call (APP_REPLY=tool): the model calls submit_plan, whose input is
the Plan, and inv.model_call validates that input as it validates a JSON reply.

The traces in tests/fixtures/traces/tool_*.json are hand-written fixtures (see the README there);
`pytest --record` with ANTHROPIC_API_KEY set replaces them with recordings.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from invariants.integrations.anthropic import ReplayMismatch
from support_agent import fakestripe, graph, llm, planner
from support_agent.context import AgentContext
from support_agent.models import Plan

TRACES = pathlib.Path(__file__).resolve().parent / "fixtures" / "traces"


def test_the_plan_tool_is_the_closed_plan_schema() -> None:
    schema = llm.PLAN_TOOL["input_schema"]
    assert llm.PLAN_TOOL["name"] == "submit_plan" and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(Plan.model_fields)
    assert schema["properties"]["action"]["enum"] == ["REFUND", "CREDIT", "CANCEL", "ESCALATE_TO_HUMAN"]


def test_a_refund_by_tool_call(ctx: AgentContext, traced) -> None:
    traced("tool_refund", reply="tool")
    s = graph.run_ticket(ctx, "please refund 1200 on O-1", "C-1")
    assert s.trace == ["triage", "planner", "execute", "done"] and s.result == "re_1"
    assert fakestripe.REFUNDS[0]["amount"] == 1200


def test_a_bad_tool_call_is_answered_with_an_error_tool_result(ctx: AgentContext, traced, model_mode: str) -> None:
    """The first call carries a field the closed Plan does not have; the parser error goes back as a
    tool_result for that tool_use id with is_error true, and the second call is used."""
    if model_mode == "record":
        pytest.skip("a scripted format failure: a recording would replace it with whatever the model does")
    traced("tool_bad_call_fixed", reply="tool")
    s = graph.run_ticket(ctx, "refund 450 on O-2 please, the item arrived broken", "C-1")
    assert s.result == "re_1" and fakestripe.REFUNDS[0]["amount"] == 450
    if model_mode == "replay":
        assert s.plan is not None and s.plan.attempts == 2
        second = json.loads((TRACES / "tool_bad_call_fixed.json").read_text())["calls"][1]["request"]
        [result] = second["messages"][2]["content"]
        assert result == {"type": "tool_result", "tool_use_id": "toolu_hw_fix_1", "is_error": True,
                          "content": result["content"]}
        assert "approved_by" in result["content"]


def test_an_over_refund_call_is_resampled_in_a_fresh_conversation(ctx: AgentContext, traced, model_mode: str) -> None:
    """PLAN-1 fires on the first call's input (above what is left of O-2); the resample is a new
    conversation that never sees the rule text, and its call is used."""
    if model_mode == "record":
        pytest.skip("a scripted violation: a recording would replace it with whatever the model does")
    traced("tool_resample", reply="tool")
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.result == "re_1" and fakestripe.REFUNDS[0]["amount"] == 100
    if model_mode == "replay":
        assert s.plan is not None and s.plan.attempts == 2


def test_an_escalation_by_tool_call_goes_to_a_person(ctx: AgentContext, traced) -> None:
    traced("tool_escalate", reply="tool")
    s = graph.run_ticket(ctx, "I think I was charged twice last month?", "C-2")
    assert s.trace == ["triage", "planner", "handoff"] and s.review_id is not None
    assert fakestripe.REFUNDS == []


def test_a_json_trace_does_not_replay_as_a_tool_flow(ctx: AgentContext) -> None:
    """The replay compares the whole request, tools included: a trace recorded without the tool
    (refund.json) mismatches the tool flow loudly."""
    planner.set_model(llm.select("replay", TRACES / "refund.json", reply="tool"))
    with pytest.raises(ReplayMismatch, match="tools"):
        graph.run_ticket(ctx, "please refund 1200 on O-1", "C-1")


def test_unknown_reply_style() -> None:
    with pytest.raises(ValueError, match="reply style"):
        llm.select("fake", reply="xml")
