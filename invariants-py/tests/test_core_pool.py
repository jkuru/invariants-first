"""R-21 and P-1: providers, transaction boundary, old_self, route (prototype checks 28-38)."""
from __future__ import annotations

import pool

import invariants as inv
from support import load_error, load_variant, raises, rule


def r21_without_approval_caught(svc) -> bool:
    """Falsifier r21_without_approval: a resize with no approval row is rejected, nothing written."""
    e = raises(inv.Rejected, svc.resize, "p1", 150)
    return e is not None and e.outcome.kind == "violation" and pool.size(svc.conn, "p1") == 100


def p1_shrink_caught(svc) -> bool:
    """Falsifier p1_shrink: a shrink through the door is routed and rolled back."""
    pool.approve(svc.conn, "p1", 150)
    svc.resize("p1", 150)
    pool.approve(svc.conn, "p1", 80)
    e = raises(inv.Routed, svc.resize, "p1", 80)
    return e is not None and pool.size(svc.conn, "p1") == 150


def test_r21_rejects_without_approval(reg, pool_svc):
    assert r21_without_approval_caught(pool_svc)


def test_r21_rejects_pending_and_wrong_size(reg, pool_svc):
    pool.approve(pool_svc.conn, "p1", 150, "PENDING")
    e1 = raises(inv.Rejected, pool_svc.resize, "p1", 150)
    pool.approve(pool_svc.conn, "p1", 999)
    e2 = raises(inv.Rejected, pool_svc.resize, "p1", 150)
    assert e1 is not None and e2 is not None and pool.size(pool_svc.conn, "p1") == 100


def test_r21_allows_matching_approval(reg, pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    assert pool.size(pool_svc.conn, "p1") == 150


def test_resize_outside_transaction_is_rule_error(reg, pool_svc):
    pool.approve(pool_svc.conn, "p2", 120)
    e = raises(inv.InvariantError, pool_svc.resize_door, pool.ResizePoolRequest("p2", 120))
    assert isinstance(e, inv.Rejected) and e.outcome.kind == "rule_error"
    assert "same_txn" in e.outcome.error and pool.size(pool_svc.conn, "p2") == 100


def test_provider_raises_or_wrong_type_is_rule_error(reg, pool_svc):
    pool.approve(pool_svc.conn, "p2", 120)
    real_fn = reg.providers["approval"].fn

    def down(conn, pool_id):
        raise RuntimeError("approvals store down")
    reg.providers["approval"].fn = down
    e1 = raises(inv.Rejected, pool_svc.resize, "p2", 120)
    reg.providers["approval"].fn = lambda conn, pool_id: {"state": "APPROVED"}
    e2 = raises(inv.Rejected, pool_svc.resize, "p2", 120)
    reg.providers["approval"].fn = real_fn
    assert e1 and e2 and e1.outcome.kind == e2.outcome.kind == "rule_error"
    assert "approvals store down" not in e1.outcome.error and "raised RuntimeError" in e1.outcome.error
    assert "returned dict" in e2.outcome.error
    assert pool.size(pool_svc.conn, "p2") == 100


def test_provider_exception_text_only_with_debug(reg, pool_svc):
    """The message may hold secrets: it reaches the outcome (and logs) only with debug on."""
    pool.approve(pool_svc.conn, "p2", 120)
    real_fn = reg.providers["approval"].fn

    def down(conn, pool_id):
        raise RuntimeError("db down: password=hunter2")
    reg.providers["approval"].fn = down
    try:
        e1 = raises(inv.Rejected, pool_svc.resize, "p2", 120)
        inv.configure(reg, debug=True)
        e2 = raises(inv.Rejected, pool_svc.resize, "p2", 120)
    finally:
        reg.providers["approval"].fn = real_fn
    assert "hunter2" not in e1.outcome.error and "hunter2" in e2.outcome.error


ECHO_CALLS: list[int] = []


@inv.provider("echo")
def echo(x: int) -> int:
    ECHO_CALLS.append(x)
    return x


@inv.guard(f"py:{__name__}.two_rules")
def two_rules(a: int, b: int) -> None:
    pass


def test_two_rules_bind_provider_args_differently():
    ECHO_CALLS.clear()
    tgt = f"py:{__name__}.two_rules"
    two = inv.load({"targets": {tgt: {"effects": "none"}},
                    "providers": {"echo": {"type": "int", "from": f"py:{__name__}.echo", "args": {"x": "v"},
                                           "consistency": "stale_ok"}},
                    "rules": [{"id": rid, "status": "enforce", "subject": {"target": tgt}, "bind": {"v": path},
                               "bad": "echo != v", "falsified_by": ["t::none"],
                               "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}
                              for rid, path in (("C-a", "args.a"), ("C-b", "args.b"))]}, install=False)
    with inv.using(two):
        two_rules(1, 2)
        calls_12 = list(ECHO_CALLS)
        two_rules(3, 3)
    assert calls_12 == [1, 2] and ECHO_CALLS == [1, 2, 3]


def test_p1_snapshot_inferred(reg, pool_svc):
    assert reg.rules["P-1"].snapshot == [("sizes",)]
    assert inv.check_snapshot("P-1", pool_svc) == ["P-1:old_self.sizes"]


def test_snapshot_negatives(reg, pool_svc):
    p1_conn = load_variant(lambda d: rule(d, "P-1").update(bad="old_self.conn != null"))
    e1 = raises(inv.LoadError, inv.check_snapshot, "P-1", pool_svc, p1_conn)
    e2 = load_error(lambda d: rule(d, "P-1").update(bad="old_self == null"))
    assert e1 is not None and "not plain data" in str(e1)
    assert e2 is not None and "old_self must be followed" in str(e2)


def test_snapshot_keeps_only_outermost_paths():
    r = load_variant(lambda d: rule(d, "P-1").update(
        bad="old_self.sizes.p1 > 0 && old_self.sizes[args.req.pool_id] > self.sizes[args.req.pool_id]"))
    assert r.rules["P-1"].snapshot == [("sizes",)]


def test_non_plain_snapshot_at_runtime_is_rule_error(reg, pool_svc):
    r = load_variant(lambda d: rule(d, "P-1").update(bad="old_self.conn != null"))
    pool.approve(pool_svc.conn, "p1", 150)
    with inv.using(r):
        e = raises(inv.Routed, pool_svc.resize, "p1", 150)
    assert e is not None and e.outcome.kind == "rule_error" and "not plain data" in e.outcome.error
    assert pool.size(pool_svc.conn, "p1") == 100


def test_p1_shrink_routes_and_rolls_back(reg, pool_svc):
    rev0 = len(reg.reviews)
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    pool.approve(pool_svc.conn, "p1", 80)
    e = raises(inv.Routed, pool_svc.resize, "p1", 80)
    assert e is not None and e.review_id == f"review-{rev0 + 1}" and reg.reviews[-1]["id"] == e.review_id
    assert pool.size(pool_svc.conn, "p1") == 150


def test_swallowed_guard_failure_still_rolls_back(reg, pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    pool.approve(pool_svc.conn, "p1", 80)

    def swallow():
        with pool_svc.txn():
            try:
                pool_svc.resize_door(pool.ResizePoolRequest("p1", 80))
            except inv.Routed:
                pass                                       # caller ignores the guard
    e = raises(inv.RolledBack, swallow)
    assert e is not None and pool.size(pool_svc.conn, "p1") == 150 and e.rule == "P-1"


def test_p1_detect_reports_and_commits(reg, pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    pool.approve(pool_svc.conn, "p1", 80)
    with inv.using(load_variant(lambda d: rule(d, "P-1").update(status="detect"))) as det:
        pool_svc.resize("p1", 80)
        ev = next((x for x in reversed(det.sink) if x["rule"] == "P-1"), {})
    assert ev.get("outcome") == "violation" and ev.get("mode") == "detect" and ev.get("action") == "proceed"
    assert pool.size(pool_svc.conn, "p1") == 80


def test_post_effect_outside_boundary_is_rule_error(reg, pool_svc):
    """P-1 alone (R-21 ablated) outside the transaction: the post-effect guard cannot roll back.

    A missing boundary is a wiring bug: WiringError (a Rejected), not P-1's on_violation (route).
    """
    pool.approve(pool_svc.conn, "p1", 150)
    with inv.ablated("R-21"):
        e = raises(inv.WiringError, pool_svc.resize_door, pool.ResizePoolRequest("p1", 150))
    assert e is not None and e.outcome.kind == "rule_error" and "cannot confirm" in e.outcome.error
    assert isinstance(e, inv.Rejected) and not isinstance(e, inv.Routed) and e.review_id is None
    assert reg.reviews == []
