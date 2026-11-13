"""Ablation (`pytest --inv-ablate`): disabling a rule's guard must make its falsifiers fail.

Each rule's falsifier catches with the guard on,
and stops catching with it off.
"""
from __future__ import annotations

from typing import Any, Callable

import pool
import pytest

import invariants as inv
import test_core_model as m
import test_core_outcomes as o
import test_core_pool as p
import test_core_r1 as r1
import test_core_r20 as r20


def _fresh_pool_svc() -> Any:
    conn = pool.connect()
    conn.execute("INSERT INTO pools VALUES ('p1', 100), ('p2', 100)")
    svc = pool.PoolService(conn)

    def resize(pid: str, new: int) -> None:
        svc.resize_door(pool.ResizePoolRequest(pid, new))
    svc.resize = resize
    return svc


def _r1(mp: pytest.MonkeyPatch) -> bool:
    return r1.r1_mutant_caught(mp)


FALSIFIERS: dict[str, list[tuple[str, Callable[[pytest.MonkeyPatch], bool]]]] = {
    "R-1": [("r1_mutant", _r1)],
    "R-7": [("r7_held_data_mismatch", lambda mp: m.r7_held_data_mismatch_caught()),
            ("r7_forged_checked", lambda mp: m.r7_forged_checked_caught())],
    "R-20": [("door_refusal", lambda mp: r20.door_refuses_over_lease())],
    "R-21": [("r21_without_approval", lambda mp: p.r21_without_approval_caught(_fresh_pool_svc()))],
    "P-1": [("p1_shrink", lambda mp: p.p1_shrink_caught(_fresh_pool_svc()))],
    "P-2": [("p2_empty_id", lambda mp: o.p2_empty_id_caught())],
}
CASES = [(rid, name, fn) for rid, fs in FALSIFIERS.items() for name, fn in fs]


@pytest.mark.parametrize("rid,name,fn", CASES, ids=[f"{r}-{n}" for r, n, _ in CASES])
def test_falsifier_catches_with_guard(reg, monkeypatch, rid, name, fn):
    assert fn(monkeypatch), f"{name} should catch with {rid} on"


@pytest.mark.parametrize("rid,name,fn", CASES, ids=[f"{r}-{n}" for r, n, _ in CASES])
def test_falsifier_fails_when_ablated(reg, monkeypatch, rid, name, fn):
    with inv.ablated(rid):
        assert not fn(monkeypatch), f"{name} still catches with {rid} ablated: the guard is not load-bearing"
    assert rid not in reg.ablated


def test_every_enforced_rule_has_a_falsifier(reg):
    assert set(FALSIFIERS) == {rid for rid, r in reg.rules.items() if r.status == "enforce"}


def test_inv_ablate_env_var(monkeypatch):
    monkeypatch.setenv("INV_ABLATE", "R-1,P-2")
    monkeypatch.setenv("INV_ABLATE_TESTING", "1")
    with inv.isolated() as r:
        assert r.ablated == {"R-1", "P-2"}


def test_inv_ablate_alone_is_ignored(monkeypatch, caplog):
    """INV_ABLATE without the test-only switch does nothing, and says so."""
    import invariants.registry as registry
    monkeypatch.setattr(registry._SESSION, "warned_unarmed", False)
    monkeypatch.setenv("INV_ABLATE", "R-1,P-2")
    monkeypatch.delenv("INV_ABLATE_TESTING", raising=False)
    with caplog.at_level("WARNING", logger="invariants"), inv.isolated() as r:
        assert r.ablated == set()
    assert "INV_ABLATE_TESTING" in caplog.text


@pytest.mark.parametrize("scope", ["context", "registry"])
def test_ablated_is_reentrant(reg, scope):
    with inv.ablated("R-1", scope=scope):
        with inv.ablated("R-1", scope=scope):
            pass
        assert reg.is_ablated("R-1")
    assert not reg.is_ablated("R-1")
