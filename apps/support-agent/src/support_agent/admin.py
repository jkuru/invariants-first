"""Legacy admin page code. goodwill_refund predates BillingService and calls the gateway directly.

It is a known back door: `inv init` froze it in the door rule's allow list, so it keeps working and a
new caller of it, or any new code that skips the door, fails `inv check`.
"""
from __future__ import annotations

from .repo import StripeGateway


def goodwill_refund(gateway: StripeGateway, order_id: str, amount_cents: int) -> str:
    return gateway.refund(order_id, amount_cents)


def admin_goodwill(order_id: str, amount_cents: int) -> str:
    """The admin page's handler."""
    return goodwill_refund(StripeGateway(), order_id, amount_cents)
