"""R-21 and P-1: providers, old_self, route."""
from __future__ import annotations

import pool

import invariants as inv
from support import load_error, load_variant, raises, rule


def r21_without_approval_caught(svc) -> bool:
    """Falsifier r21_without_approval: a resize with no approval row is rejected, nothing written."""
    e = raises(inv.Rejected, svc.resize, "p1", 150)
    return e is not None and e.outcome.kind == "violation" and pool.size(svc.conn, "p1") == 100


def p1_shrink_caught(svc) -> bool:
    """Falsifier p1_shrink: a shrink through the door is routed before the write."""
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
                    "providers": {"echo": {"type": "int", "from": f"py:{__name__}.echo", "args": {"x": "v"}}},
                    "rules": [{"id": rid, "status": "enforce", "subject": {"target": tgt}, "bind": {"v": path},
                               "bad": "echo != v", "falsified_by": ["t::none"],
                               "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]}
                              for rid, path in (("C-a", "args.a"), ("C-b", "args.b"))]}, install=False)
    with inv.using(two):
        two_rules(1, 2)
        calls_12 = list(ECHO_CALLS)
        two_rules(3, 3)
    assert calls_12 == [1, 2] and ECHO_CALLS == [1, 2, 3]


# P-1 as a transition rule over old_self (a post_effect check: on an external effect, detect only)
OLD_P1 = "self.sizes[args.req.pool_id] < old_self.sizes[args.req.pool_id]"


def transition(bad: str = OLD_P1) -> inv.Registry:
    def mutate(d):
        r = rule(d, "P-1")
        r.update(bad=bad, scope="transition", status="detect")
        r["enforcers"][0].update(timing="post_effect", mode="detect")
    return load_variant(mutate)


def test_p1_snapshot_inferred(pool_svc):
    r = transition()
    assert r.rules["P-1"].snapshot == [("sizes",)]
    assert inv.check_snapshot("P-1", pool_svc, r) == ["P-1:old_self.sizes"]


def test_snapshot_negatives(pool_svc):
    e1 = raises(inv.LoadError, inv.check_snapshot, "P-1", pool_svc, transition("old_self.conn != null"))
    e2 = load_error(lambda d: rule(d, "P-1").update(bad="old_self == null"))
    assert e1 is not None and "not plain data" in str(e1)
    assert e2 is not None and "old_self must be followed" in str(e2)


def test_snapshot_keeps_only_outermost_paths():
    r = transition("old_self.sizes.p1 > 0 && old_self.sizes[args.req.pool_id] > self.sizes[args.req.pool_id]")
    assert r.rules["P-1"].snapshot == [("sizes",)]


def test_non_plain_snapshot_at_runtime_is_rule_error(pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    with inv.using(transition("old_self.conn != null")) as r:
        pool_svc.resize("p1", 150)
        ev = next(x for x in reversed(r.sink) if x["rule"] == "P-1")
    assert ev["outcome"] == "rule_error" and "not plain data" in ev["error"]


def test_transition_rule_sees_the_old_and_new_state(pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    with inv.using(transition()) as r:
        pool_svc.resize("p1", 150)                       # a growth: ok
        pool.approve(pool_svc.conn, "p1", 80)
        pool_svc.resize("p1", 80)                        # a shrink: old_self.sizes.p1 150 > 80, reported
        outcomes = [x["outcome"] for x in r.sink if x["rule"] == "P-1"]
    assert outcomes == ["ok", "violation"] and pool.size(pool_svc.conn, "p1") == 80


def test_post_effect_enforce_on_an_external_effect_is_refused_at_load():
    e = load_error(lambda d: rule(d, "P-1")["enforcers"][0].update(timing="post_effect"))
    assert e is not None and "timing check fails" in str(e)


def test_p1_shrink_routes_before_the_write(reg, pool_svc):
    rev0 = len(reg.reviews)
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    pool.approve(pool_svc.conn, "p1", 80)
    e = raises(inv.Routed, pool_svc.resize, "p1", 80)
    assert e is not None and e.review_id == f"review-{rev0 + 1}" and reg.reviews[-1]["id"] == e.review_id
    assert pool.size(pool_svc.conn, "p1") == 150


def test_p1_detect_reports_and_writes(reg, pool_svc):
    pool.approve(pool_svc.conn, "p1", 150)
    pool_svc.resize("p1", 150)
    pool.approve(pool_svc.conn, "p1", 80)
    with inv.using(load_variant(lambda d: rule(d, "P-1").update(status="detect"))) as det:
        pool_svc.resize("p1", 80)
        ev = next((x for x in reversed(det.sink) if x["rule"] == "P-1"), {})
    assert ev.get("outcome") == "violation" and ev.get("mode") == "detect" and ev.get("action") == "proceed"
    assert pool.size(pool_svc.conn, "p1") == 80
