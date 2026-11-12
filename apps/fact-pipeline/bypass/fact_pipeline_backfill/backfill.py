"""An "ops backfill" that skips the promotion door: exactly what K-12's mediation forbids.

Never import this from the app. It exists so the scan (and the monitor) have something to catch.
"""
from __future__ import annotations

import sqlite3

from fact_pipeline.store import write_status


def force_believe(conn: sqlite3.Connection, fact_id: str) -> None:
    """Marks a fact BELIEVED without the K-12 check (a bypass path to the sink)."""
    write_status(conn, fact_id, "BELIEVED", "backfill")


def raw_believe(conn: sqlite3.Connection, fact_id: str) -> None:
    """The same, through raw SQL on the protected table."""
    conn.execute("INSERT INTO fact_status (fact_id, status, reason) VALUES (?, 'BELIEVED', 'raw backfill')",
                 (fact_id,))
