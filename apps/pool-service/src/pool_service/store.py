"""The repository. set_size and insert_pool are the protected sinks of R-23: only the doors in service call them."""
from __future__ import annotations

import sqlite3
from typing import Any


def insert_pool(conn: sqlite3.Connection, pool_id: str, name: str, size: int) -> None:
    conn.execute("INSERT INTO pools (id, name, size) VALUES (?, ?, ?)", (pool_id, name, size))


def set_size(conn: sqlite3.Connection, pool_id: str, new_size: int) -> None:
    """The raw resize. Only ResizeService.resize_door may call it (R-23)."""
    conn.execute("UPDATE pools SET size = ? WHERE id = ?", (new_size, pool_id))


def get_pool(conn: sqlite3.Connection, pool_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, name, size FROM pools WHERE id = ?", (pool_id,)).fetchone()
    return None if row is None else {"pool_id": row[0], "name": row[1], "size": row[2]}


def size(conn: sqlite3.Connection, pool_id: str) -> int | None:
    row = conn.execute("SELECT size FROM pools WHERE id = ?", (pool_id,)).fetchone()
    return None if row is None else int(row[0])


def all_sizes(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in conn.execute("SELECT id, size FROM pools")}


def leased(conn: sqlite3.Connection, pool_id: str) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(slots), 0) FROM leases WHERE pool_id = ?",
                            (pool_id,)).fetchone()[0])


def lease_slots(conn: sqlite3.Connection, pool_id: str) -> list[tuple[int, str, int]]:
    """(lease id, holder, slots) for every lease of the pool, oldest first."""
    return [(r[0], r[1], r[2]) for r in
            conn.execute("SELECT id, holder, slots FROM leases WHERE pool_id = ? ORDER BY id", (pool_id,))]


def insert_lease(conn: sqlite3.Connection, pool_id: str, holder: str, slots: int) -> int:
    cur = conn.execute("INSERT INTO leases (pool_id, holder, slots) VALUES (?, ?, ?)", (pool_id, holder, slots))
    return int(cur.lastrowid or 0)


def delete_lease(conn: sqlite3.Connection, lease_id: int) -> bool:
    return conn.execute("DELETE FROM leases WHERE id = ?", (lease_id,)).rowcount > 0


def insert_proposal(conn: sqlite3.Connection, pool_id: str, new_size: int, proposed_by: str) -> int:
    cur = conn.execute("INSERT INTO resize_proposals (pool_id, new_size, proposed_by) VALUES (?, ?, ?)",
                       (pool_id, new_size, proposed_by))
    return int(cur.lastrowid or 0)


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT id, pool_id, new_size, proposed_by, approved_by, state FROM resize_proposals "
                       "WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        return None
    keys = ("proposal_id", "pool_id", "new_size", "proposed_by", "approved_by", "state")
    return dict(zip(keys, row))


def set_proposal_state(conn: sqlite3.Connection, proposal_id: int, state: str, approved_by: str | None = None) -> None:
    if approved_by is None:
        conn.execute("UPDATE resize_proposals SET state = ? WHERE id = ?", (state, proposal_id))
    else:
        conn.execute("UPDATE resize_proposals SET state = ?, approved_by = ? WHERE id = ?",
                     (state, approved_by, proposal_id))


def insert_job(conn: sqlite3.Connection, job_id: str, pool_id: str, oom_killed: bool, elapsed_s: int,
               timeout_s: int, exit_code: int) -> None:
    conn.execute("INSERT INTO jobs (id, pool_id, oom_killed, elapsed_s, timeout_s, exit_code) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (job_id, pool_id, int(oom_killed), elapsed_s, timeout_s, exit_code))


def get_job(conn: sqlite3.Connection, job_id: str) -> tuple[str, str, bool, int, int, int] | None:
    row = conn.execute("SELECT id, pool_id, oom_killed, elapsed_s, timeout_s, exit_code FROM jobs WHERE id = ?",
                       (job_id,)).fetchone()
    return None if row is None else (row[0], row[1], bool(row[2]), row[3], row[4], row[5])


def insert_remediation(conn: sqlite3.Connection, job_id: str, cause: str, action: str) -> int:
    cur = conn.execute("INSERT INTO remediations (job_id, cause, action) VALUES (?, ?, ?)", (job_id, cause, action))
    return int(cur.lastrowid or 0)


def remediations(conn: sqlite3.Connection, job_id: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT cause, action FROM remediations WHERE job_id = ? ORDER BY id",
                                               (job_id,))]
