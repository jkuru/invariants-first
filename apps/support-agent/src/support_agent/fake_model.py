"""A deterministic stand-in for a tool-calling model. It never calls a network.

It reads a ticket like "refund 1200 on O-1" or "credit 300" and answers with a JSON plan, the way a
model in structured-output mode would. `inject(...)` queues bad replies for the next calls, so tests
and demos can drive every failure path.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

FAULTS = ("malformed", "unknown_tool", "extra_field", "overrefund", "overcredit", "abstain", "wrong_order")
_AMOUNT = re.compile(r"(\d+)")
_ORDER = re.compile(r"\b(O-\d+)\b")


def honest_plan(ticket: str, customer_id: str) -> dict[str, Any]:
    """The plan an honest model would give for this ticket."""
    text = ticket.lower()
    order = _ORDER.search(ticket)
    amount = _AMOUNT.search(re.sub(r"O-\d+", "", ticket))
    cents = int(amount.group(1)) if amount else 0
    if "refund" in text and order and cents:
        return {"action": "REFUND", "customer_id": customer_id, "order_id": order.group(1), "amount_cents": cents,
                "reason": "customer asked for a refund"}
    if "credit" in text and cents:
        return {"action": "CREDIT", "customer_id": customer_id, "order_id": "", "amount_cents": cents,
                "reason": "goodwill credit"}
    if "cancel" in text:
        return {"action": "CANCEL", "customer_id": customer_id, "order_id": "", "amount_cents": 0,
                "reason": "customer asked to cancel"}
    return {"action": "ESCALATE_TO_HUMAN", "customer_id": customer_id, "order_id": "", "amount_cents": 0,
            "reason": "not sure what the customer wants"}


class FakeAgentModel:
    """Deterministic fake. Thread-safe; records every call with the feedback it was given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[str] = []
        self._lock = threading.Lock()

    def inject(self, *faults: str) -> None:
        """Make the next len(faults) replies bad, one fault each, in order."""
        for f in faults:
            if f not in FAULTS:
                raise ValueError(f"unknown fault {f!r}; pick from {FAULTS}")
        with self._lock:
            self._queue.extend(faults)

    def complete(self, ticket: str, customer_id: str, feedback: str | None = None) -> str:
        with self._lock:
            fault = self._queue.pop(0) if self._queue else None
            self.calls.append({"ticket": ticket, "feedback": feedback, "fault": fault})
        plan = honest_plan(ticket, customer_id)
        if fault == "malformed":
            return '{"action": "REFUND", "customer_id": '
        if fault == "unknown_tool":
            plan["action"] = "WIRE_TRANSFER"
        elif fault == "extra_field":
            plan["approved_by"] = "the model itself"
        elif fault == "overrefund":
            plan.update(action="REFUND", order_id=plan["order_id"] or "O-2", amount_cents=99_000)
        elif fault == "overcredit":
            plan.update(action="CREDIT", order_id="", amount_cents=9_000)
        elif fault == "abstain":
            plan.update(action="ESCALATE_TO_HUMAN", order_id="", amount_cents=0)
        elif fault == "wrong_order":
            plan.update(order_id="O-3")                     # another customer's order
        return json.dumps(plan)
