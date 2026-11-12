"""R-21 (resize needs an independent approval) and R-22 (no more than 4x growth), on a transactional door."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import invariants as inv
from pool_service import service, store
from conftest import make_pool


def propose(client: TestClient, pool_id: str, new_size: int, who: str = "alice") -> int:
    r = client.post(f"/pools/{pool_id}/resizes", json={"new_size": new_size, "proposed_by": who})
    assert r.status_code == 201, r.text
    return r.json()["proposal_id"]


def approve(client: TestClient, pid: int, who: str = "bob") -> None:
    r = client.post(f"/resizes/{pid}/approve", json={"approver": who})
    assert r.status_code == 200 and r.json()["state"] == "APPROVED"


def test_approved_resize_applies(client: TestClient) -> None:
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 30)
    approve(client, pid)
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 200 and r.json()["size"] == 30
    assert client.get("/pools/gpu-a").json()["size"] == 30


def test_r21_apply_without_approval_is_rejected(client: TestClient) -> None:
    """Falsifier: a pending proposal cannot be applied."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 20)
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 422 and r.json()["rule"] == "R-21"
    assert client.get("/pools/gpu-a").json()["size"] == 10


def test_r21_self_approval_is_rejected(client: TestClient) -> None:
    """Falsifier: the proposer cannot approve their own resize."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 20, who="alice")
    approve(client, pid, who="alice")
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 422 and r.json()["rule"] == "R-21"
    assert client.get("/pools/gpu-a").json()["size"] == 10


def test_r21_replay_is_rejected(client: TestClient) -> None:
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 20)
    approve(client, pid)
    assert client.post(f"/resizes/{pid}/apply").status_code == 200
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 422 and r.json()["rule"] == "R-21"


def test_r21_forged_request_is_rejected(conn: sqlite3.Connection) -> None:
    """A caller that writes a size other than the approved one is refused, and nothing commits."""
    service.create_pool(conn, "p1", "P1", 10)
    pid = service.propose_resize(conn, "p1", 20, "alice")
    service.approve_resize(conn, pid, "bob")
    svc = service.ResizeService(conn)
    with pytest.raises(inv.Rejected) as e:
        with svc.txn():
            svc.resize_door(service.ResizeRequest(pid, "p1", 40))
    assert e.value.rule == "R-21"
    assert store.size(conn, "p1") == 10


def test_resize_door_outside_its_transaction_is_rule_error(conn: sqlite3.Connection) -> None:
    """The same_txn approval cannot be read outside ResizeService.txn: rule_error, fail closed."""
    service.create_pool(conn, "p1", "P1", 10)
    pid = service.propose_resize(conn, "p1", 20, "alice")
    service.approve_resize(conn, pid, "bob")
    with pytest.raises(inv.Rejected) as e:
        service.ResizeService(conn).resize_door(service.ResizeRequest(pid, "p1", 20))
    assert e.value.outcome.kind == "rule_error"
    assert store.size(conn, "p1") == 10


def test_swallowed_violation_still_rolls_back(conn: sqlite3.Connection) -> None:
    service.create_pool(conn, "p1", "P1", 10)
    pid = service.propose_resize(conn, "p1", 20, "alice")
    svc = service.ResizeService(conn)
    with pytest.raises(inv.RolledBack) as e:
        with svc.txn():
            try:
                svc.resize_door(service.ResizeRequest(pid, "p1", 20))
            except inv.Rejected:
                pass                     # a careless caller
    assert e.value.rule == "R-21"
    assert store.size(conn, "p1") == 10


def test_r22_growth_above_4x_is_rolled_back(client: TestClient) -> None:
    """Falsifier: 10 -> 50 is approved but breaks R-22 after the write; the transaction rolls back."""
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 50)
    approve(client, pid)
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 422 and r.json()["rule"] == "R-22"
    assert client.get("/pools/gpu-a").json()["size"] == 10
    assert client.post(f"/resizes/{pid}/approve", json={"approver": "carol"}).json()["state"] == "APPROVED"


def test_r22_allows_exactly_4x(client: TestClient) -> None:
    make_pool(client, "gpu-a", 10)
    pid = propose(client, "gpu-a", 40)
    approve(client, pid)
    assert client.post(f"/resizes/{pid}/apply").json()["size"] == 40


def test_shrink_below_leased_is_a_storage_conflict(client: TestClient) -> None:
    make_pool(client, "gpu-a", 10)
    assert client.post("/pools/gpu-a/leases", json={"holder": "w", "slots": 8}).status_code == 201
    pid = propose(client, "gpu-a", 5)
    approve(client, pid)
    r = client.post(f"/resizes/{pid}/apply")
    assert r.status_code == 409 and r.json()["rule"] == "R-20" and r.json()["point"] == "storage"
    assert client.get("/pools/gpu-a").json()["size"] == 10


def test_unknown_proposal(client: TestClient) -> None:
    assert client.post("/resizes/999/apply").status_code == 404
    assert client.post("/resizes/999/approve", json={"approver": "x"}).status_code == 404
    assert client.post("/pools/nope/resizes", json={"new_size": 1, "proposed_by": "x"}).status_code == 404


def test_old_self_snapshot_is_plain_data(conn: sqlite3.Connection) -> None:
    service.create_pool(conn, "p1", "P1", 10)
    got = inv.check_snapshot("py:pool_service.service.ResizeService.resize_door", service.ResizeService(conn))
    assert got == ["R-22:old_self.sizes"]
