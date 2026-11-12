"""What every node and tool receives: the doors and the read side of the ledger."""
from __future__ import annotations

import sqlite3

from .repo import LedgerRepo, StripeGateway, connect
from .service import BillingService


class AgentContext:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.ledger = LedgerRepo(conn)
        self.billing = BillingService(self.ledger, StripeGateway())


def demo_context() -> AgentContext:
    """An in-memory ledger with two customers and three orders."""
    conn = connect()
    conn.executescript("""
        INSERT INTO customers (id, tier, credit_limit_cents) VALUES ('C-1', 'gold', 5000), ('C-2', 'basic', 500);
        INSERT INTO orders (id, customer_id, total_cents) VALUES ('O-1', 'C-1', 12000), ('O-2', 'C-1', 3000),
                                                               ('O-3', 'C-2', 800);
    """)
    return AgentContext(conn)
