"""The agent's tools. Each one validates its input model and calls a BillingService door.

TOOLS is the registry the executor dispatches through, as tool-calling frameworks do: the tools are
passed around as values, and a tool that skipped the door would be found by `inv check`.
"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from .context import AgentContext
from .models import CancelToolInput, CreditToolInput, RefundToolInput


def issue_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:
    return ctx.billing.refund(inp.order_id, inp.amount_cents, inp.reason)


def issue_credit_tool(ctx: AgentContext, inp: CreditToolInput) -> str:
    ctx.billing.credit(inp.customer_id, inp.amount_cents, inp.reason)
    return f"credited {inp.amount_cents} to {inp.customer_id}"


def cancel_subscription_tool(ctx: AgentContext, inp: CancelToolInput) -> str:
    ctx.billing.cancel(inp.customer_id, inp.reason)
    return f"cancelled {inp.customer_id}"


Tool = Callable[[AgentContext, Any], str]
TOOLS: dict[str, tuple[type[BaseModel], Tool]] = {
    "REFUND": (RefundToolInput, issue_refund_tool),
    "CREDIT": (CreditToolInput, issue_credit_tool),
    "CANCEL": (CancelToolInput, cancel_subscription_tool),
}
