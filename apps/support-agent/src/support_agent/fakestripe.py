"""A stand-in for the `stripe` client library: the same call shape (stripe.Refund.create), no network.

Every created refund is kept in REFUNDS so tests can see what reached the payment provider.
"""
from __future__ import annotations

import itertools
import threading
from typing import Any

REFUNDS: list[dict[str, Any]] = []
_ids = itertools.count(1)
_lock = threading.Lock()


class Refund:
    @staticmethod
    def create(charge: str, amount: int, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        with _lock:
            r = {"id": f"re_{next(_ids)}", "charge": charge, "amount": amount, "metadata": dict(metadata or {})}
            REFUNDS.append(r)
            return r


def reset() -> None:
    global _ids
    with _lock:
        REFUNDS.clear()
        _ids = itertools.count(1)
