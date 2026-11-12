"""The API boundaries of the agent, as pydantic models: the tools' inputs and the planner's reply.

Each Field constraint is a value law on an API. `inv init` pins them, so a later change that lets a
tool accept more (a raised refund cap, a removed bound) fails `inv check` as a weakening.
"""
from __future__ import annotations

import dataclasses
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefundToolInput(Closed):
    """issue_refund_tool's input: one refund against one order."""

    order_id: str = Field(min_length=1, max_length=40)
    amount_cents: int = Field(gt=0, le=50_000)
    reason: str = Field(min_length=1, max_length=200)


class CreditToolInput(Closed):
    """issue_credit_tool's input: account credit for a customer."""

    customer_id: str = Field(min_length=1, max_length=40)
    amount_cents: int = Field(gt=0, le=10_000)
    reason: str = Field(min_length=1, max_length=200)


class CancelToolInput(Closed):
    """cancel_subscription_tool's input."""

    customer_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=200)


Action = Literal["REFUND", "CREDIT", "CANCEL", "ESCALATE_TO_HUMAN"]


class Plan(Closed):
    """The planner's reply: one action. Every field is required; an unused one is "" or 0."""

    action: Action
    customer_id: str = Field(min_length=1, max_length=40)
    order_id: str = Field(max_length=40)
    amount_cents: int = Field(ge=0, le=500_000)
    reason: str = Field(min_length=1, max_length=200)


@dataclasses.dataclass(frozen=True)
class Order:
    """An order as the ledger holds it (held data: never from the model)."""

    order_id: str
    customer_id: str
    total_cents: int
    refunded_cents: int
    remaining_cents: int


@dataclasses.dataclass(frozen=True)
class Tier:
    """A customer's support tier and the credit it allows."""

    customer_id: str
    tier: str
    credit_limit_cents: int
