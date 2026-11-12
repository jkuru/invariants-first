"""The doors: every refund, credit and cancellation goes through BillingService.

BILL-1 and BILL-2 check each call's arguments against held data (the order, the customer's tier)
before anything is written, whoever calls: the agent's tools, a script, an admin page.
"""
from __future__ import annotations

import invariants as inv

from .models import Order, Tier
from .repo import LedgerRepo, StripeGateway


class BillingService:
    def __init__(self, ledger: LedgerRepo, stripe: StripeGateway) -> None:
        self.ledger = ledger
        self.stripe = stripe

    @inv.guard("BILL-1")
    def refund(self, order_id: str, amount_cents: int, reason: str) -> str:
        """Refund part of an order; returns the payment provider's refund id."""
        refund_id = self.stripe.refund(order_id, amount_cents)
        self.ledger.record_refund(refund_id, order_id, amount_cents)
        return refund_id

    @inv.guard("BILL-2")
    def credit(self, customer_id: str, amount_cents: int, reason: str) -> None:
        """Give a customer account credit."""
        self.ledger.insert_credit(customer_id, amount_cents, reason)

    def cancel(self, customer_id: str, reason: str) -> None:
        """Cancel a customer's subscription."""
        self.ledger.cancel_subscription(customer_id)


# --- providers: held data the rules compare against ---------------------------------------------
def load_order(ledger: LedgerRepo, order_id: str) -> Order | None:
    row = ledger.order(order_id)
    if row is None:
        return None
    return Order(row["id"], row["customer_id"], row["total_cents"], row["refunded"],
                 row["total_cents"] - row["refunded"])


def load_tier(ledger: LedgerRepo, customer_id: str) -> Tier | None:
    row = ledger.customer(customer_id)
    if row is None:
        return None
    return Tier(row["id"], row["tier"], row["credit_limit_cents"] - ledger.credits(customer_id))


@inv.provider("order")
def order(ledger: LedgerRepo, order_id: str) -> Order | None:
    """The order a refund names (BILL-1)."""
    return load_order(ledger, order_id)


@inv.provider("tier")
def tier(ledger: LedgerRepo, customer_id: str) -> Tier | None:
    """The customer's tier, with the credit still allowed (BILL-2)."""
    return load_tier(ledger, customer_id)


@inv.provider("planned_order")
def planned_order(ledger: LedgerRepo, order_id: str) -> Order | None:
    """The order the planner's reply names (PLAN-1)."""
    return load_order(ledger, order_id)


@inv.provider("planned_tier")
def planned_tier(ledger: LedgerRepo, customer_id: str) -> Tier | None:
    """The tier of the customer the planner's reply names (PLAN-1)."""
    return load_tier(ledger, customer_id)
