"""R-20: leases never exceed pool size, checked on lease_slots' arguments. The schema's own triggers
also refuse an over-lease (concurrent writers); they are below the API, tested here as app code."""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

import invariants as inv
from pool_service import db, service, store
from conftest import make_pool


def pool(conn: sqlite3.Connection, size: int, pool_id: str = "p1") -> str:
    service.create_pool(conn, pool_id, pool_id, size)
    return pool_id


def test_lease_within_size(conn: sqlite3.Connection) -> None:
    pid = pool(conn, 5)
    service.lease_slots(conn, pid, 2, "a")
    service.lease_slots(conn, pid, 3, "b")
    assert store.leased(conn, pid) == 5


def test_r20_door_rejects_over_lease(conn: sqlite3.Connection) -> None:
    """Falsifier: the door refuses before the write. Ablated, the store's trigger refuses instead."""
    pid = pool(conn, 5)
    service.lease_slots(conn, pid, 3, "a")
    with pytest.raises(inv.Rejected) as e:
        service.lease_slots(conn, pid, 3, "b")
    assert e.value.rule == "R-20" and e.value.outcome.kind == "violation"
    assert store.leased(conn, pid) == 3


def test_trigger_holds_when_door_is_ablated(conn: sqlite3.Connection) -> None:
    pid = pool(conn, 5)
    service.lease_slots(conn, pid, 3, "a")
    with inv.ablated("R-20"):
        with pytest.raises(sqlite3.IntegrityError, match="R-20"):
            service.lease_slots(conn, pid, 3, "b")
    assert store.leased(conn, pid) == 3


def test_unknown_pool_is_rule_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(inv.Rejected) as e:
        service.lease_slots(conn, "missing", 1, "a")
    assert e.value.outcome.kind == "rule_error"


def test_trigger_refuses_shrink_below_leased(conn: sqlite3.Connection) -> None:
    pid = pool(conn, 5)
    service.lease_slots(conn, pid, 4, "a")
    with pytest.raises(sqlite3.IntegrityError, match="R-20"):
        store.set_size(conn, pid, 3)


def _door(size: int, held: list[int], n: int) -> bool:
    """True if the door lets the lease through (checked on its own database)."""
    c = db.memory()
    pid = pool(c, size)
    for h in held:
        store.insert_lease(c, pid, "x", h)
    try:
        service.lease_slots(c, pid, n, "y")
        return True
    except inv.Rejected:
        return False


def _trigger(size: int, held: list[int], n: int) -> bool:
    """True if the store's own trigger lets the lease through (door ablated)."""
    c = db.memory()
    pid = pool(c, size)
    for h in held:
        store.insert_lease(c, pid, "x", h)
    try:
        with inv.ablated("R-20"):
            service.lease_slots(c, pid, n, "y")
        return True
    except sqlite3.IntegrityError:
        return False


@settings(max_examples=150, deadline=None)
@given(size=st.integers(1, 40), data=st.data())
def test_r20_door_and_trigger_agree(size: int, data: st.DataObject) -> None:
    """The API check and the store's trigger give the same verdict (the app's own differential test)."""
    held: list[int] = []
    for h in data.draw(st.lists(st.integers(1, 10), max_size=6)):
        if sum(held) + h <= size:
            held.append(h)
    n = data.draw(st.integers(1, 20))
    assert _door(size, held, n) == _trigger(size, held, n) == (sum(held) + n <= size)


@settings(max_examples=60, deadline=None)
@given(ops=st.lists(st.tuples(st.sampled_from(["lease", "release"]), st.integers(1, 6)), max_size=30))
def test_r20_property_any_history(ops: list[tuple[str, int]]) -> None:
    """Whatever sequence of leases and releases runs, leased never exceeds size."""
    c = db.memory()
    pid = pool(c, 12)
    ids: list[int] = []
    for op, n in ops:
        if op == "lease":
            try:
                ids.append(service.lease_slots(c, pid, n, "h"))
            except inv.Rejected:
                pass
        elif ids:
            service.release(c, ids.pop(n % len(ids)))
        assert store.leased(c, pid) <= store.size(c, pid)


def test_concurrent_leases_never_exceed_size(client: TestClient) -> None:
    """32 threads hammer the lease endpoint; exactly floor(50 / 3) leases fit."""
    make_pool(client, "hammer", 50)

    def one(i: int) -> int:
        return client.post("/pools/hammer/leases", json={"holder": f"w{i}", "slots": 3}).status_code

    with ThreadPoolExecutor(max_workers=32) as ex:
        codes = list(ex.map(one, range(64)))
    assert codes.count(201) == 16
    assert set(codes) <= {201, 409, 422}
    assert client.get("/pools/hammer").json()["leased"] == 48


def test_race_past_the_door_is_stopped_by_the_trigger(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the 2.7 interleaving: every request reads 'leased' before any writes. The API check passes all
    of them (it is not a lock), so the store's own trigger refuses the over-lease: a plain 409."""
    make_pool(client, "racy", 4)
    n = 8
    barrier = threading.Barrier(n, timeout=10)
    real = store.leased

    def leased_then_wait(conn: sqlite3.Connection, pool_id: str) -> int:
        v = real(conn, pool_id)
        barrier.wait()
        return v

    monkeypatch.setattr(store, "leased", leased_then_wait)

    def one(i: int) -> tuple[int, dict]:
        r = client.post("/pools/racy/leases", json={"holder": f"w{i}", "slots": 1})
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(one, range(n)))
    monkeypatch.setattr(store, "leased", real)
    codes = [c for c, _ in results]
    assert codes.count(201) == 4 and codes.count(409) == 4, codes
    conflicts = [b for c, b in results if c == 409]
    assert all("rule" not in b and "exceed the pool size" in b["title"] for b in conflicts)
    assert client.get("/pools/racy").json()["leased"] == 4


def test_lease_http_answers(client: TestClient) -> None:
    make_pool(client, "web", 4)
    r = client.post("/pools/web/leases", json={"holder": "a", "slots": 3})
    assert r.status_code == 201 and r.json()["slots"] == 3
    r = client.post("/pools/web/leases", json={"holder": "b", "slots": 3})
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json() == {"type": "urn:invariants:rejected", "title": "The request violates an invariant",
                        "status": 422, "rule": "R-20", "outcome": "violation"}
    assert client.post("/pools/nope/leases", json={"holder": "b", "slots": 1}).status_code == 404
    lease_id = client.post("/pools/web/leases", json={"holder": "c", "slots": 1}).json()["lease_id"]
    assert client.delete(f"/leases/{lease_id}").status_code == 204
    assert client.delete(f"/leases/{lease_id}").status_code == 404
