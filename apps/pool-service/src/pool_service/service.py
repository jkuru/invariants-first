"""The doors. Every write that an invariant protects goes through a guarded function here.

R-20 lease_slots: leases never exceed pool size (checked on the API's arguments; the schema's own triggers
also refuse an over-lease, which is the store's business).
R-21 / R-22 ResizeService.resize_door: a resize needs an approval, and never grows a pool more than 4x.
R-24 create_pool: a new pool has a size between 1 and 10000.
"""
from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from typing import Iterator

import invariants as inv

from . import store


class NotFound(LookupError):
    """A pool, lease or proposal does not exist."""


# --- providers (read-only, typed) ---------------------------------------------------------------
@inv.provider("pool_leased")
def pool_leased(conn: sqlite3.Connection, pool_id: str) -> int:
    """Slots currently leased in the pool."""
    return store.leased(conn, pool_id)


@inv.provider("pool_size", "current_size")
def pool_size(conn: sqlite3.Connection, pool_id: str) -> int:
    """The pool's size, one row (R-20 as pool_size, R-22 as current_size); raises NotFound, which the
    guard reports as a rule_error."""
    s = store.size(conn, pool_id)
    if s is None:
        raise NotFound(f"pool {pool_id} does not exist")
    return s


@dataclasses.dataclass(frozen=True)
class Approval:
    """A resize proposal as stored."""

    proposal_id: int
    pool_id: str
    new_size: int
    proposed_by: str
    approved_by: str
    state: str


@inv.provider("approval")
def load_approval(conn: sqlite3.Connection, proposal_id: int) -> Approval | None:
    """The proposal the resize names."""
    row = store.get_proposal(conn, proposal_id)
    return None if row is None else Approval(**row)


# --- pools ---------------------------------------------------------------------------------------
@inv.guard("R-24")
def create_pool(conn: sqlite3.Connection, pool_id: str, name: str, size: int) -> None:
    """Create a pool (the R-23 door for inserts into pools)."""
    store.insert_pool(conn, pool_id, name, size)


# --- leases (R-20) -------------------------------------------------------------------------------
@inv.guard("R-20")
def lease_slots(conn: sqlite3.Connection, pool_id: str, slots: int, holder: str) -> int:
    """Lease slots in a pool; returns the lease id."""
    return store.insert_lease(conn, pool_id, holder, slots)


def release(conn: sqlite3.Connection, lease_id: int) -> None:
    """Release a lease. Releasing cannot break R-20, so it has no guard."""
    if not store.delete_lease(conn, lease_id):
        raise NotFound(f"lease {lease_id} does not exist")


# --- resizes (R-21, R-22) ------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ResizeRequest:
    """What resize_door writes."""

    proposal_id: int
    pool_id: str
    new_size: int


def propose_resize(conn: sqlite3.Connection, pool_id: str, new_size: int, proposed_by: str) -> int:
    """Record a resize proposal (state PENDING); returns its id."""
    if store.size(conn, pool_id) is None:
        raise NotFound(f"pool {pool_id} does not exist")
    return store.insert_proposal(conn, pool_id, new_size, proposed_by)


def approve_resize(conn: sqlite3.Connection, proposal_id: int, approver: str) -> None:
    """Mark a pending proposal APPROVED by approver. R-21 checks the approver is not the proposer."""
    p = store.get_proposal(conn, proposal_id)
    if p is None:
        raise NotFound(f"proposal {proposal_id} does not exist")
    if p["state"] == "PENDING":
        store.set_proposal_state(conn, proposal_id, "APPROVED", approved_by=approver)


class ResizeService:
    """Applies approved resizes inside one SQLite transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextlib.contextmanager
    def txn(self) -> Iterator["ResizeService"]:
        """One SQLite write transaction (the implementation's; no rule depends on it)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @inv.guard("py:pool_service.service.ResizeService.resize_door")
    def resize_door(self, req: ResizeRequest) -> None:
        """The only writer of pools.size (R-23)."""
        store.set_size(self.conn, req.pool_id, req.new_size)
        store.set_proposal_state(self.conn, req.proposal_id, "APPLIED")

    def apply(self, proposal_id: int) -> int:
        """Apply a proposal in one transaction; returns the new size."""
        with self.txn():
            p = store.get_proposal(self.conn, proposal_id)
            if p is None:
                raise NotFound(f"proposal {proposal_id} does not exist")
            self.resize_door(ResizeRequest(proposal_id, p["pool_id"], p["new_size"]))
            return int(p["new_size"])
