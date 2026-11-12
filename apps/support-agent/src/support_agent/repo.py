"""The lowest APIs: the ledger repository (SQLite) and the payment gateway (Stripe).

Their write methods are the sinks: only the BillingService doors may call them. How they store
things (SQL, transactions, the provider's API) is their own business, below every rule.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import fakestripe

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, total_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS refunds (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, tier TEXT NOT NULL, credit_limit_cents INTEGER NOT NULL,
                                      subscribed INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS credits (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL,
                                    amount_cents INTEGER NOT NULL, reason TEXT NOT NULL);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class LedgerRepo:
    """Orders, refunds, customers and credits."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def order(self, order_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT o.id, o.customer_id, o.total_cents, COALESCE(SUM(r.amount_cents), 0) AS refunded "
            "FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.id = ? GROUP BY o.id",
            (order_id,)).fetchone()
        return dict(row) if row else None

    def customer(self, customer_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT id, tier, credit_limit_cents, subscribed FROM customers WHERE id = ?",
                                (customer_id,)).fetchone()
        return dict(row) if row else None

    def credits(self, customer_id: str) -> int:
        return self.conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM credits WHERE customer_id = ?",
                                 (customer_id,)).fetchone()[0]

    def record_refund(self, refund_id: str, order_id: str, amount_cents: int) -> None:
        self.conn.execute("INSERT INTO refunds (id, order_id, amount_cents) VALUES (?, ?, ?)",
                          (refund_id, order_id, amount_cents))

    def insert_credit(self, customer_id: str, amount_cents: int, reason: str) -> None:
        self.conn.execute("INSERT INTO credits (customer_id, amount_cents, reason) VALUES (?, ?, ?)",
                          (customer_id, amount_cents, reason))

    def cancel_subscription(self, customer_id: str) -> None:
        self.conn.execute("UPDATE customers SET subscribed = 0 WHERE id = ?", (customer_id,))


class StripeGateway:
    """The payment provider. refund() moves real money."""

    def refund(self, order_id: str, amount_cents: int) -> str:
        return str(fakestripe.Refund.create(charge=f"ch_{order_id}", amount=amount_cents,
                                            metadata={"order_id": order_id})["id"])
