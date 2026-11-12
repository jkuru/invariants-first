"""R-20: door view via providers, storage view via rows (prototype checks 58-62)."""
from __future__ import annotations

import random
import sqlite3

import pool

import invariants as inv
from support import raises


def door_refuses_over_lease() -> bool:
    """The door alone refuses a sequential over-lease."""
    db = pool.connect(triggers=False)
    db.execute("INSERT INTO pools VALUES ('p', 100)")
    pool.acquire(db, "p", 60)
    e = raises(inv.Rejected, pool.acquire, db, "p", 60)
    return e is not None and pool.leased(db, "p") == 60


def interleaving(triggers: bool) -> tuple[int, int]:
    """B runs to completion right after A's door read pool_leased: both doors see 0/100."""
    db = pool.connect(triggers=triggers)
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


def differential_r20(reg: inv.Registry, schema: str = pool.SCHEMA, seed: int = 7, ops: int = 600):
    """Fill both views of one bad; they must agree, and storage refuses exactly when bad is true."""
    rnd, db, r20 = random.Random(seed), pool.connect(schema=schema), reg.rules["R-20"]
    db.execute("INSERT INTO pools VALUES ('a', 50), ('b', 80), ('c', 20)")
    agree = views = refused = 0
    for _ in range(ops):
        p = rnd.choice("abc")
        rows = dict(db.execute("SELECT pool_id, SUM(n) FROM leases GROUP BY pool_id").fetchall())
        sizes = dict(db.execute("SELECT id, size FROM pools").fetchall())
        if rnd.random() < 0.8:
            n, new = rnd.randint(1, 15), sizes[p]
        else:
            n, new = 0, rnd.randint(0, 120)
        rows_bad = inv.evaluate_env(r20, {"leased": rows.get(p, 0), "n": n, "size": new}).kind == "violation"
        if n:
            door_bad = inv.evaluate(r20, {"args": {"conn": db, "pool_id": p, "slots": n}}, 0).kind == "violation"
            views += door_bad == rows_bad
        else:
            views += 1                                   # a resize does not pass the door: storage view only
        try:
            if n:
                db.execute("INSERT INTO leases (pool_id, n) VALUES (?, ?)", (p, n))
            else:
                pool.set_size(db, p, new)
            was_refused = False
        except sqlite3.IntegrityError:
            was_refused = True
        agree += was_refused == rows_bad
        refused += was_refused
        if rnd.random() < 0.3:
            db.execute("DELETE FROM leases WHERE id = (SELECT MIN(id) FROM leases WHERE pool_id = ?)", (p,))
    return agree, views, refused, ops


def test_door_reads_providers_and_refuses(reg):
    assert door_refuses_over_lease()


def test_interleaving_without_trigger_over_leases(reg):
    ok0 = reg.evidence["R-20"]["ok"]
    assert interleaving(False) == (120, 0)
    assert reg.evidence["R-20"]["ok"] - ok0 == 2


def test_interleaving_with_trigger_refuses(reg):
    assert interleaving(True) == (60, 1)


def test_interleaving_trigger_holds_even_when_door_ablated(reg):
    with inv.ablated("R-20"):
        assert interleaving(True) == (60, 1)


def test_differential_views_agree(reg):
    a, v, r, n = differential_r20(reg)
    assert a == v == n and 0 < r < n


def test_differential_catches_off_by_one_trigger(reg):
    a, _, _, n = differential_r20(reg, schema=pool.SCHEMA.replace("+ NEW.n > (SELECT", "+ NEW.n >= (SELECT"))
    assert a < n
