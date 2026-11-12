"""R-20: the lease API reads providers and refuses; what it does not claim (concurrent callers)."""
from __future__ import annotations

import sqlite3

import pool

import invariants as inv
from support import raises


def door_refuses_over_lease() -> bool:
    """The door alone refuses a sequential over-lease."""
    db = pool.connect()
    db.execute("INSERT INTO pools VALUES ('p', 100)")
    pool.acquire(db, "p", 60)
    e = raises(inv.Rejected, pool.acquire, db, "p", 60)
    return e is not None and pool.leased(db, "p") == 60


def interleaving() -> tuple[int, int]:
    """B runs to completion right after A's door read pool_leased: both doors see 0/100. A check on an
    API's arguments is not a lock; the store decides what concurrent writers may do."""
    db = pool.connect()
    db.execute("INSERT INTO pools VALUES ('p', 100)")
    refused = 0

    def lease() -> None:
        nonlocal refused
        try:
            pool.acquire(db, "p", 60)
        except sqlite3.IntegrityError:
            refused += 1
    pool._AFTER_READ.append(lease)                       # B, run inside A's provider read
    lease()                                              # A
    while pool._AFTER_READ:
        pool._AFTER_READ.pop(0)()                        # door ablated: no read happened, B runs after A
    return pool.leased(db, "p"), refused


def test_door_reads_providers_and_refuses(reg):
    assert door_refuses_over_lease()


def test_interleaving_is_not_closed_by_the_api_check(reg):
    ok0 = reg.evidence["R-20"]["ok"]
    assert interleaving() == (120, 0)
    assert reg.evidence["R-20"]["ok"] - ok0 == 2
