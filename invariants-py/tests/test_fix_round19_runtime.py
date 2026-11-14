"""Fix round 19 (runtime): each test failed before its fix.

1. @inv.edge binds parameters only (a `-> Checked` return annotation is not one), resolves a Checked
   class defined where the decorator runs, and an annotation that does not resolve is a clear
   TypeError (LoadError for a provider), never a raw NameError.
2. The value of a Checked over a {field: type} schema is frozen: no mutating method is reachable.
3. Loader: old_self on a pre_effect guard, a model_policy rule without subject.target, a guard rule
   without subject.target whose arguments do not bind (inv verify), old_self aliased into a
   provider argument, and a non-mapping subject are refused.
4. Registry reads of demoted, the ablation counters, the exporters and MemoryReviews take their locks.
5. pydantic: NaN in a dataclass of a lax model, naive datetimes in sets and dataclasses are refused.
6. A rule_error answered by a route/escalate enforcer is HTTP 503, not a routed 202.
7. inv verify: a misplaced @inv.edge and a target wrapped by something other than @inv.guard are
   reported; inv status never reports held without --ablation.
8. @inv.edge on agent-graph state: the Checked value in a field of a TypedDict, dataclass or
   pydantic state (auto-discovered when unambiguous, or at="state.plan").
9. An async hand-off to a review store with only open() runs it in a worker thread.
10. A pydantic reply re-validated from the same JSON skips the NaN/naive walks (never the validation).
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime
import json
import math
import pickle
import threading
from typing import Any, Optional

import pydantic
import pytest

import invariants as inv
import round19 as r19
from invariants import registry, scan, status_report
from invariants.integrations import asgi
from invariants.integrations import pydantic as inv_pd
from invariants.model import Outcome
from invariants.registry import Handoff
from cli_support import run_inv
from support import RULES

MOD = "test_fix_round19_runtime"


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


@pytest.fixture
def loaded(iso):
    inv.load(r19.model_doc())
    r19.SEEN.clear()
    return iso


def checked(**over: Any) -> r19.CPlan:
    r19.REPLIES[:] = [r19.reply(**over)]
    return r19.plan()


# --- 1. edge annotations -------------------------------------------------------------------------
def test_edge_with_untyped_param_and_checked_return_is_refused_at_decoration():
    with pytest.raises(TypeError, match="an edge needs a parameter annotated"):
        @inv.edge("P-1")
        def f(p) -> r19.CPlan:              # type: ignore[no-untyped-def]
            return p


def test_edge_resolves_a_checked_class_defined_where_the_decorator_runs():
    class Local(inv.Checked): ...

    @inv.edge("P-1")
    def g(p: Local) -> None: ...
    assert g.__inv_at__ == "p"            # type: ignore[attr-defined]


def test_an_unresolvable_annotation_is_a_clear_type_error():
    with pytest.raises(TypeError, match="'NoSuchClass' does not resolve"):
        @inv.edge("P-1")
        def h(p: NoSuchClass) -> None: ...  # type: ignore[name-defined]  # noqa: F821


@inv.provider("bogus")
def bogus_provider(v: int) -> NoSuchReturnType:  # type: ignore[name-defined]  # noqa: F821
    return v


def test_a_provider_whose_return_annotation_does_not_resolve_is_a_load_error(iso):
    doc = r19.guard_doc("G-1", "spend", "b > 1", bind={"b": "provider.bogus"},
                        providers={"bogus": {"type": "int", "from": f"py:{MOD}.bogus_provider",
                                             "args": {"v": "args.amount"}}})
    with pytest.raises(inv.LoadError, match="NoSuchReturnType' does not resolve"):
        inv.load(doc)


# --- 2. a frozen {field: type} value -------------------------------------------------------------
def test_a_dict_checked_value_has_no_reachable_mutating_method(loaded):
    c = checked()
    for mutate in (lambda: c.update({"n": 9}), lambda: c.pop("n"), lambda: c.clear(),
                   lambda: c.setdefault("x", 1), lambda: c.popitem(), lambda: c.value.__setitem__("n", 9),
                   lambda: c.value.__delitem__("n"), lambda: c.value["tags"].append("b"),
                   lambda: c.value["tags"].__setitem__(0, "z"), lambda: c.value["tags"].extend(["q"]),
                   lambda: c.value.__init__({"n": 9}), lambda: c.value["tags"].__init__(["q"])):
        with pytest.raises(TypeError, match="immutable"):
            mutate()
    assert c.value == {"action": "go", "n": 1, "tags": ["a"]} and c.get("n") == 1
    assert json.loads(json.dumps(c.value)) == {"action": "go", "n": 1, "tags": ["a"]}
    for other in (copy.deepcopy(c), pickle.loads(pickle.dumps(c))):
        assert other == c
        with pytest.raises(TypeError):
            other.value["n"] = 9
    assert r19.act(c) == "acted" and r19.SEEN["p"] == c


# --- 3. loader ------------------------------------------------------------------------------------
def test_old_self_on_a_pre_effect_guard_is_a_load_error(iso):
    with pytest.raises(inv.LoadError, match="old_self.*pre_effect"):
        inv.load(r19.guard_doc("G-OLD", "Box.grow", "old_self.size > self.size"))
    inv.load(r19.guard_doc("G-OLD", "Box.grow", "old_self.size > self.size", timing="post_effect"))


def test_a_model_policy_rule_needs_subject_target(iso):
    doc = r19.model_doc()
    del doc["rules"][0]["subject"]
    with pytest.raises(inv.LoadError, match="model_policy needs subject.target"):
        inv.load(doc)


def test_a_guard_rule_without_target_is_bound_against_the_guarded_function_by_verify(iso):
    inv.load(r19.guard_doc("G-1", "spend", "args.qty > 5", target=False))
    with pytest.raises(inv.CoverageError, match=r"args.qty.* does not resolve against .*spend"):
        inv.verify(modules=["round19"])


def test_old_self_aliased_into_a_provider_argument_is_snapshotted_and_scope_checked(iso):
    prov = {"count": {"type": "int", "from": "py:round19.count", "args": {"v": "prev"}}}
    bind = {"prev": "old_self.size", "c": "provider.count"}
    reg = inv.load(r19.guard_doc("G-OLD", "Box.grow", "c > 100", timing="post_effect", bind=bind,
                                 providers=prov), install=False)
    assert reg.rules["G-OLD"].snapshot == [("size",)]
    with pytest.raises(inv.LoadError, match="old_self"):     # before the effect it is self itself
        inv.load(r19.guard_doc("G-OLD", "Box.grow", "c > 100", bind=bind, providers=prov), install=False)


@pytest.mark.parametrize("subject", ["foo", 3, ["py:round19.spend"]])
def test_a_non_mapping_subject_is_a_load_error(iso, subject):
    doc = r19.guard_doc("G-1", "spend", "args.amount > 5")
    doc["rules"][0]["subject"] = subject
    with pytest.raises(inv.LoadError, match="subject must be a mapping"):
        inv.load(doc)


# --- 4. registry locks -----------------------------------------------------------------------------
class SpyLock:
    def __init__(self) -> None:
        self.n, self._lock = 0, threading.Lock()

    def __enter__(self) -> SpyLock:
        self._lock.acquire()
        self.n += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()


def test_registry_reads_take_their_locks(iso, monkeypatch):
    inv.load(r19.guard_doc("G-1", "spend", "args.amount > 5"))
    rule = iso.rules["G-1"]
    spy, gspy, rspy = SpyLock(), SpyLock(), SpyLock()
    iso._lock = spy
    monkeypatch.setattr(registry, "_GLOBAL_LOCK", gspy)
    iso.mode(rule, rule.enforcers[0])
    assert spy.n == 1                                            # demoted
    iso.is_ablated("G-1")
    assert spy.n == 2 and gspy.n == 1                            # _reg_ablated, GLOBAL_ABLATED
    store = iso.review_store
    store._lock = rspy
    assert iso.reviews == [] and rspy.n == 1                     # MemoryReviews.items
    before = spy.n
    with inv.capture(iso):
        pass
    assert spy.n == before + 2                                   # exporters: add and remove


# --- 5. pydantic walks -------------------------------------------------------------------------------
@dataclasses.dataclass
class Inner:
    x: float
    at: Optional[datetime.datetime] = None


class Lax(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(strict=False, extra="forbid")
    inner: Inner


class Strict(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    inner: Inner
    times: frozenset[datetime.datetime] = frozenset()


def test_nan_in_a_dataclass_of_a_lax_model_is_refused():
    with pytest.raises(inv.ParseError, match="inner.x: non-finite"):
        inv_pd.validate(Lax, {"inner": {"x": "NaN"}})


def test_a_naive_datetime_in_a_set_or_a_dataclass_is_refused():
    with pytest.raises(inv.ParseError, match="times.*without a timezone"):
        inv_pd.validate(Strict, {"inner": {"x": 1.0}, "times": ["2024-01-01T00:00:00"]})
    with pytest.raises(inv.ParseError, match="inner.at.*without a timezone"):
        inv_pd.validate(Strict, {"inner": {"x": 1.0, "at": "2024-01-01T00:00:00"}})
    ok = inv_pd.validate(Strict, {"inner": {"x": 1.0, "at": "2024-01-01T00:00:00Z"}, "times": ["2024-01-01T00:00:00Z"]})
    assert ok.inner.at == r19.AWARE


# --- 6. rule_error under route is 503 ------------------------------------------------------------------
def test_a_routed_rule_error_is_503_with_its_review(iso):
    inv.load(r19.guard_doc("G-1", "spend", "1 / (args.amount - args.amount) > 0", on_violation="route"))
    with pytest.raises(inv.Routed) as ei:
        r19.spend(3)
    assert ei.value.outcome.kind == "rule_error"
    status, body = asgi.problem(ei.value)
    assert status == 503 and body["outcome"] == "rule_error" and body["review_id"] == ei.value.review_id
    esc = inv.Escalation("x", Outcome("rule_error", "G-1"), review_id="review-9")
    assert asgi.problem(esc)[0] == 503
    assert asgi.problem(inv.Routed("x", Outcome("violation", "G-1"), review_id="review-9"))[0] == 202


# --- 7. verify and status ------------------------------------------------------------------------------
def test_verify_reports_a_misplaced_edge(loaded):
    with pytest.raises(inv.CoverageError) as ei:          # the placed edges are not reported
        inv.verify(modules=["round19"])
    assert "act" not in str(ei.value).replace("aplan", "")

    @inv.edge("P-1")
    def stray(p: r19.CPlan) -> None: ...
    with pytest.raises(inv.CoverageError, match="stray: @inv.edge\\('P-1'\\), but no edge enforcer of P-1 targets it"):
        inv.verify(modules=[MOD])


@inv.edge("G-K")
def kfn(p: r19.CPlan) -> None: ...


def test_verify_reports_a_guard_rule_whose_target_is_not_an_inv_guard(iso):
    doc = {"spec": r19.SPEC, "targets": {f"py:{MOD}.kfn": {"effects": "none"}},
           "rules": [{"id": "G-K", "status": "enforce", "subject": {"target": f"py:{MOD}.kfn"}, "bad": "false",
                      "falsified_by": ["t"], "enforcers": [{"point": "decorator", "timing": "pre_effect",
                                                            "mode": "enforce"}]}]}
    inv.load(doc)
    with pytest.raises(inv.CoverageError, match="kfn is wrapped by @inv.edge, not by @inv.guard"):
        inv.verify(modules=[])


def test_status_never_reports_held_as_ablation_proven_without_ablation(reg):
    report = scan.scan(RULES, [])
    row = status_report.rule_status(reg, report, "R-1")
    assert row["standing"] == "held" and row["ablation_proven"] is False
    assert status_report.NO_ABLATION in row["reasons"]
    text = status_report.to_text({"rules": [row], "verify": {"passed": True, "problems": []}})
    assert status_report.UNPROVEN in text
    from invariants.registry.coverage import rule_digest
    caught = {"ablation": {"rules": {"R-1": [{"result": "caught"}]}, "digests": {"R-1": rule_digest(reg.rules["R-1"])}}}
    proven = status_report.rule_status(reg, report, "R-1", caught)
    assert proven["standing"] == "held" and proven["ablation_proven"] is True and proven["reasons"] == []
    missed = status_report.rule_status(reg, report, "R-1", {"ablation": {"rules": {}}})
    assert missed["standing"] == "enforce(known-callers)" and missed["ablation_proven"] is False


def test_status_cli_labels_held_unproven_without_ablation():
    from cli_support import CLEAN
    proc = run_inv("status", CLEAN / "invariants.yaml")
    assert proc.returncode == 0 and "L-1" in proc.stdout and status_report.UNPROVEN in proc.stdout


def test_status_ablation_flag_needs_a_report(tmp_path):
    from cli_support import CLEAN
    bad = tmp_path / "x.json"
    bad.write_text("{}")
    proc = run_inv("status", CLEAN / "invariants.yaml", "--ablation", bad)
    assert proc.returncode != 0 and "--ablation" in proc.stderr


# --- 8. edges on agent-graph state ---------------------------------------------------------------------
def test_edge_finds_the_checked_field_of_a_typeddict_state(loaded):
    c = checked(n=2)
    state = {"plan": c, "note": "x"}
    assert r19.act_state(state) == 2                   # type: ignore[arg-type]
    got = r19.SEEN["state"]
    assert got is not state and got["note"] == "x" and state["plan"] is c          # the caller's is unchanged
    assert got["plan"] == c and got["plan"] is not c and got["plan"].value is not c.value
    assert r19.act_state.__inv_at__ == "state.plan"    # type: ignore[attr-defined]


def test_edge_on_state_re_checks_like_a_direct_edge(loaded):
    c = checked(n=2)
    dict.__setitem__(c.value, "n", 9)                  # bypassing the freeze: bad fires again
    with pytest.raises(inv.Rejected):
        r19.act_state({"plan": c, "note": ""})       # type: ignore[typeddict-item]
    c2 = checked()
    dict.__setitem__(c2.value, "extra", 1)             # the closed schema again
    with pytest.raises(inv.Rejected, match="extra fields"):
        asyncio.run(r19.act_dstate(r19.DState(c2)))
    with pytest.raises(TypeError, match="accepts only CPlan"):
        r19.act_state({"plan": "forged", "note": ""})  # type: ignore[typeddict-item]
    assert "state" not in r19.SEEN and "dstate" not in r19.SEEN


def test_edge_on_dataclass_and_pydantic_state(loaded):
    c = checked(n=3)
    s = r19.DState(c, "n")
    assert asyncio.run(r19.act_dstate(s)) == 3 and r19.SEEN["dstate"] is not s and s.plan is c
    p = r19.PState(plan=c)
    assert r19.act_pstate(p) == 3 and r19.SEEN["pstate"] is not p and p.plan is c
    assert r19.SEEN["pstate"].plan == c and r19.SEEN["pstate"].plan is not c


def test_an_ambiguous_or_wrong_state_path_fails_at_decoration():
    with pytest.raises(TypeError, match="more than one Checked field.*state.plan, state.other"):
        @inv.edge("P-1")
        def amb(state: r19.PState) -> None: ...
    with pytest.raises(TypeError, match="no annotated field 'nope'"):
        @inv.edge("P-1", at="state.nope")
        def wrong(state: r19.PState) -> None: ...
    with pytest.raises(TypeError, match="must be '<parameter>.<field>"):
        @inv.edge("P-1", at="other.plan")
        def wrong2(state: r19.PState) -> None: ...


# --- 9. async hand-off to a sync-only store ------------------------------------------------------------
def test_an_async_hand_off_runs_a_sync_store_in_a_worker_thread(iso):
    seen: list[int] = []

    class SyncStore:
        def open(self, handoff: Handoff) -> str:
            seen.append(threading.get_ident())
            return "review-x"

    iso.review_store = SyncStore()

    async def main() -> tuple[str, int]:
        return await iso.ahand_off(Handoff("R", "route")), threading.get_ident()
    rid, loop_thread = asyncio.run(main())
    assert rid == "review-x" and seen and seen[0] != loop_thread


def test_memory_reviews_open_on_the_loop(iso):
    async def main() -> str:
        return await iso.ahand_off(Handoff("R", "route"))
    assert asyncio.run(main()) == "review-1" and iso.reviews[0]["id"] == "review-1"


# --- 10. pydantic re-validation of the same reply ------------------------------------------------------
class Rep10(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    at: datetime.datetime
    xs: list[float]


def test_revalidating_the_same_reply_skips_the_walks_not_the_validation(monkeypatch):
    walks = {"n": 0}
    real = inv_pd._naive

    def counted(*a: Any, **k: Any) -> Any:
        if not a[1]:
            walks["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(inv_pd, "_naive", counted)
    data = {"at": "2024-01-01T00:00:00Z", "xs": [1.0, 2.5]}
    a = inv_pd.validate(Rep10, data)
    b = inv_pd.validate(Rep10, data)
    assert a == b and a is not b and a.xs is not b.xs and walks["n"] == 1
    inv_pd.validate(Rep10, {**data, "xs": [3.0]})        # another reply: walked
    assert walks["n"] == 2
    b.xs.append(math.inf)                                # a handed-out copy changed: the cache is not
    assert inv_pd.validate(Rep10, data).xs == [1.0, 2.5]
    with pytest.raises(inv.ParseError, match="non-finite"):
        inv_pd.model_of(b)
