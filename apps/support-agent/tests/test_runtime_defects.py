"""Runtime mode: the defects a model (or the graph around it) can introduce while the agent runs.

Each test seeds one defect and shows the SDK stops it before money moves. The numbers match the
defect table in README.md (6-10 are runtime defects; 1-5, 11 and 12 are caught by `inv check`).
"""
from __future__ import annotations

import pydantic
import pytest

import invariants as inv
from invariants.testing import make_checked, make_unbuilt
from support_agent import fakestripe, graph, planner
from support_agent.context import AgentContext
from support_agent.fake_model import FakeAgentModel
from support_agent.models import Plan


def _plan(**kw: object) -> Plan:
    base = {"action": "REFUND", "customer_id": "C-1", "order_id": "O-2", "amount_cents": 100, "reason": "r"}
    return Plan(**{**base, **kw})                                # type: ignore[arg-type]


# --- 6. the model hallucinates an amount the held data does not allow ----------------------------
def test_d6_an_over_refund_is_resampled_then_escalated(ctx: AgentContext, model: FakeAgentModel) -> None:
    """Falsifier for PLAN-1: two over-refunds in a row (the first, and its one resample) go to a person."""
    model.inject("overrefund", "overrefund")
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.trace == ["triage", "planner", "handoff"] and s.review_id is not None
    assert "refunds more than is left of the order" in (s.handoff_reason or "")
    assert fakestripe.REFUNDS == [] and len(model.calls) == 2


def test_d6_one_bad_sample_is_resampled_and_the_honest_one_runs(ctx: AgentContext, model: FakeAgentModel) -> None:
    model.inject("overrefund")
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.result == "re_1" and fakestripe.REFUNDS[0]["amount"] == 100
    assert model.calls[1]["feedback"] is None                     # a resample is fresh: never the rule text


def test_d6_another_customers_order_is_refused(ctx: AgentContext, model: FakeAgentModel) -> None:
    model.inject("wrong_order", "wrong_order")
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.trace[-1] == "handoff" and "another customer's order" in (s.handoff_reason or "")
    assert fakestripe.REFUNDS == []


def test_d6_an_over_credit_is_escalated(ctx: AgentContext, model: FakeAgentModel) -> None:
    model.inject("overcredit", "overcredit")
    s = graph.run_ticket(ctx, "credit 100", "C-2")                 # C-2 is basic: 500 allowed
    assert s.trace[-1] == "handoff" and ctx.ledger.credits("C-2") == 0


def test_d6_the_door_refuses_an_over_refund_whoever_calls(ctx: AgentContext) -> None:
    """Falsifier for BILL-1: the door itself refuses, so a tool, a script or an admin page cannot over-refund."""
    with pytest.raises(inv.Rejected) as e:
        ctx.billing.refund("O-2", 3001, "too much")
    assert e.value.rule == "BILL-1" and fakestripe.REFUNDS == []
    with pytest.raises(inv.Rejected, match="no such order"):
        ctx.billing.refund("O-404", 1, "nope")


def test_d6_the_door_refuses_an_over_credit(ctx: AgentContext) -> None:
    """Falsifier for BILL-2."""
    ctx.billing.credit("C-2", 400, "ok")
    with pytest.raises(inv.Rejected) as e:
        ctx.billing.credit("C-2", 200, "now above the 500 the basic tier allows")
    assert e.value.rule == "BILL-2" and ctx.ledger.credits("C-2") == 400


# --- 7. invalid JSON, an unknown tool, an extra field -------------------------------------------
@pytest.mark.parametrize("fault", ["malformed", "unknown_tool", "extra_field"])
def test_d7_a_bad_reply_is_retried_with_the_parser_error(ctx: AgentContext, model: FakeAgentModel,
                                                        fault: str) -> None:
    model.inject(fault)
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.result == "re_1" and len(model.calls) == 2
    assert model.calls[1]["feedback"]                              # the parser error went back to the model


def test_d7_retries_are_bounded_then_a_person_decides(ctx: AgentContext, model: FakeAgentModel) -> None:
    model.inject("unknown_tool", "malformed", "unknown_tool")      # format_retries: 2, so 3 attempts in all
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.trace[-1] == "handoff" and len(model.calls) == 3 and fakestripe.REFUNDS == []


# --- 8. the model abstains --------------------------------------------------------------------
def test_d8_an_abstention_opens_a_review(ctx: AgentContext, model: FakeAgentModel,
                                         rules_loaded: inv.Registry) -> None:
    model.inject("abstain")
    s = graph.run_ticket(ctx, "refund 100 on O-2", "C-1")
    assert s.trace[-1] == "handoff" and s.review_id is not None
    review = next(r for r in rules_loaded.reviews if r["id"] == s.review_id)
    assert review["rule"] == "PLAN-1" and fakestripe.REFUNDS == []


# --- 9. a forged plan that never went through the planner --------------------------------------
def _parsed(**kw: object) -> object:
    """A Plan built the way inv.parse builds it (the closed schema), as a careful forger would."""
    return inv.parse(_plan(**kw).model_dump(), Plan)


def test_d9_a_forged_plan_is_refused_at_the_edge(ctx: AgentContext) -> None:
    forged = make_checked(planner.CheckedPlan, _plan(amount_cents=99_000), verdict="ok", rule="PLAN-1")
    with pytest.raises(inv.Rejected):                              # a forged verdict counts for nothing:
        graph.execute_plan(ctx, forged, "C-1")                     # the edge re-validates and re-checks it
    fair = make_checked(planner.CheckedPlan, _parsed(amount_cents=3_000), verdict="ok", rule="PLAN-1")
    graph.execute_plan(ctx, fair, "C-1")                           # closed and within the order: it runs
    big = make_checked(planner.CheckedPlan, _parsed(amount_cents=99_000), verdict="ok", rule="PLAN-1")
    with pytest.raises(inv.Rejected):                              # beyond it: re-checked against the ledger
        graph.execute_plan(ctx, big, "C-1")
    with pytest.raises(inv.Rejected):                              # no verdict at all
        graph.execute_plan(ctx, make_unbuilt(planner.CheckedPlan), "C-1")
    with pytest.raises(TypeError):                                 # not a CheckedPlan
        graph.execute_plan(ctx, _plan(), "C-1")                    # type: ignore[arg-type]
    assert [r["amount"] for r in fakestripe.REFUNDS] == [3_000]


def test_d9_a_plan_checked_for_one_customer_cannot_run_for_another(ctx: AgentContext) -> None:
    plan = planner.plan_action(ctx, "refund 100 on O-2", "C-1")
    with pytest.raises(inv.Rejected, match="another customer"):
        graph.execute_plan(ctx, plan, "C-2")


# --- 10. graph state changes the plan between the planner and the executor ----------------------
def test_d10_a_plan_changed_after_the_check_is_refused(ctx: AgentContext) -> None:
    """Falsifier for PLAN-1's edge: the executor re-checks the plan it receives, not the one the planner saw."""
    plan = planner.plan_action(ctx, "refund 100 on O-2", "C-1")
    with pytest.raises(pydantic.ValidationError, match="frozen"):
        plan.value.amount_cents = 99_000                           # type: ignore[misc]   the plan is frozen
    object.__setattr__(plan.value, "amount_cents", 99_000)         # a graph reducer that forces it anyway
    with pytest.raises(inv.Rejected) as e:
        graph.execute_plan(ctx, plan, "C-1")
    assert e.value.rule == "PLAN-1" and fakestripe.REFUNDS == []


def test_d10_the_ledger_moved_since_the_plan_was_checked(ctx: AgentContext) -> None:
    plan = planner.plan_action(ctx, "refund 2500 on O-2", "C-1")   # fine when planned (3000 left)
    ctx.billing.refund("O-2", 1000, "another agent got there first")
    with pytest.raises(inv.Rejected):                              # 2000 left now: the edge reads it again
        graph.execute_plan(ctx, plan, "C-1")
