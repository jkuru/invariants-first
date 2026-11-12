"""R-1: split_work."""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

import invariants as inv
from pool_service import work
from conftest import make_pool


@pytest.mark.parametrize("total, caps, want", [
    (0, [], []),
    (0, [3, 4], [0, 0]),
    (7, [3, 4], [3, 4]),
    (5, [1, 1, 1, 1, 1], [1, 1, 1, 1, 1]),
    (10, [10, 0, 10], [5, 0, 5]),
    (3, [2, 2, 2], [1, 1, 1]),
    (4, [2, 2, 2], [2, 1, 1]),
])
def test_split_examples(total: int, caps: list[int], want: list[int]) -> None:
    assert work.split_work(total, caps) == want


def test_not_enough_capacity() -> None:
    with pytest.raises(work.NotEnoughCapacity):
        work.split_work(10, [3, 3])


@settings(max_examples=300, deadline=None)
@given(caps=st.lists(st.integers(0, 50), max_size=12), data=st.data())
def test_r1_property(caps: list[int], data: st.DataObject) -> None:
    total = data.draw(st.integers(0, sum(caps)))
    shares = work.split_work(total, caps)          # the guard would raise inv.Rejected on any violation
    assert len(shares) == len(caps) and sum(shares) == total
    assert all(0 <= s <= c for s, c in zip(shares, caps))


def test_r1_rejects_a_buggy_allocator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsifier: an allocator that drops the remainder is stopped by the guard."""
    monkeypatch.setattr(work, "_allocate", lambda total, caps: [total * c // max(sum(caps), 1) for c in caps])
    with pytest.raises(inv.Rejected) as e:
        work.split_work(4, [2, 2, 2])
    assert e.value.rule == "R-1"
    assert e.value.outcome is not None and e.value.outcome.messages == ["shares do not sum to total_jobs"]


def test_r1_rejects_share_above_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work, "_allocate", lambda total, caps: [total] + [0] * (len(caps) - 1))
    with pytest.raises(inv.Rejected) as e:
        work.split_work(5, [2, 3])
    assert "share above capacity" in e.value.outcome.messages


def test_split_endpoint(client) -> None:
    make_pool(client, "cpu-pool", 20)
    for holder, slots in (("w1", 2), ("w2", 3), ("w3", 5)):
        assert client.post("/pools/cpu-pool/leases", json={"holder": holder, "slots": slots}).status_code == 201
    r = client.post("/pools/cpu-pool/split", json={"total_jobs": 7})
    assert r.status_code == 200
    assert sum(r.json()["shares"].values()) == 7
    assert client.post("/pools/cpu-pool/split", json={"total_jobs": 11}).status_code == 409
    assert client.post("/pools/nope/split", json={"total_jobs": 1}).status_code == 404
