"""Regression tests for fix round 1: each test failed before its fix."""
from __future__ import annotations

import asyncio
import gc
import logging
import tracemalloc
from typing import Any

import pytest

import fixround as fx
import invariants as inv
from invariants import cel
from invariants.registry import observe
from invariants.testing import make_checked, make_unbuilt
from support import raises

BACKENDS = cel.available_backends()


def load(*rules: dict[str, Any], backend: str | None = None) -> inv.Registry:
    return inv.load(fx.doc(*rules), backend=backend)


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


@pytest.fixture(autouse=True)
def _reset():
    fx.CALLS.clear()
    fx.CONSUMED.clear()
    fx.REPLIES.clear()
    yield


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- blocker: the sink is bounded ------------------------------------------------------------------
def test_sink_is_a_bounded_ring_and_counters_stay_exact(iso):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf()))
    inv.configure(iso, sink_size=10)
    for i in range(50):
        fx.probe2(None, i % 5)
    assert len(iso.sink) == 10 and iso.counts("D-2") == {"runs": 50, "ok": 50}
    inv.configure(iso, sink_size=0)
    fx.probe2(None, 1)
    assert len(iso.sink) == 0 and iso.counts("D-2")["runs"] == 51


def test_memory_stays_flat_over_1e5_calls(iso):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf()), backend="subset")
    for _ in range(2000):                       # warm up: fill the ring and caches
        fx.probe2(None, 1)
    fx.CALLS.clear()
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    for _ in range(100_000):
        fx.probe2(None, 1)
    fx.CALLS.clear()
    gc.collect()
    grown = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    assert grown < 1_000_000, f"grew {grown} bytes"


def test_capture_collects_events_without_the_sink(iso):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf()))
    inv.configure(iso, sink_size=0)
    with inv.capture(iso) as events:
        fx.probe2(None, 1)
    fx.probe2(None, 1)
    assert [e["rule"] for e in events] == ["D-2"]


# --- major: post-effect rules read providers after the effect -------------------------------------
def test_post_effect_provider_reads_state_after_the_effect(iso):
    load(fx.T1)
    s = fx.Svc()
    with inv.capture(iso) as events:
        s.door(2)                              # rows == 2 after: ok
        s.door(2)                              # rows == 4 after the effect: the rule fires (detect)
    assert [e["outcome"] for e in events] == ["ok", "violation"] and s.count() == 4


def test_pre_effect_provider_is_read_before_the_effect(iso):
    load(fx.rule("T-2", "Svc.door2", "rows_before != 0", fx.enf()))
    s = fx.Svc()
    assert s.door2(3) == 3                      # rows_before is 0 before the first insert
    e = raises(inv.Rejected, s.door2, 1)
    assert e is not None and s.count() == 3


# --- major: no exception escapes; detect never blocks ---------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS)
def test_detect_proceeds_when_a_provider_arg_property_raises(iso, backend):
    load(fx.rule("D-1", "probe", "echo > 0", fx.enf(mode="detect"), status="detect"), backend=backend)
    with inv.capture(iso) as events:
        fx.probe(fx.Boom())
    assert len(fx.CALLS) == 1 and events[0]["outcome"] == "rule_error"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("value", [2**63, 10**400, -2**63 - 1])
def test_detect_proceeds_on_out_of_range_ints(iso, backend, value):
    load(fx.rule("D-2", "probe2", "args.x.size > 0", fx.enf(mode="detect"), status="detect"), backend=backend)
    with inv.capture(iso) as events:
        fx.probe2(None, {"size": value})
    assert len(fx.CALLS) == 1 and events[0]["outcome"] == "rule_error"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("value", [2**63, 10**400, -2**63 - 1])
def test_out_of_range_ints_are_rule_error_on_every_backend(backend, value):
    r = inv.load({"rules": [{"id": "C", "status": "candidate", "bad": "args.x.size > 0"}]}, install=False,
                 backend=backend).rules["C"]
    assert inv.evaluate(r, {"args": {"x": {"size": value}}}).kind == "rule_error"


def test_failing_observer_never_reaches_the_call(iso, caplog):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf(mode="detect"), status="detect"))

    def bad(*a: Any) -> None:
        raise RuntimeError("observer down")
    stop = observe(bad)
    iso.exporters.append(bad)
    try:
        with caplog.at_level(logging.ERROR, "invariants"):
            fx.probe2(None, 9)
    finally:
        stop()
    assert len(fx.CALLS) == 1 and "observer" in caplog.text


@pytest.mark.parametrize("mode,status", [("detect", "detect"), ("enforce", "enforce")])
def test_edge_given_an_unbuilt_checked_is_an_outcome(iso, mode, status):
    load(fx.rule("M-2", "smodel", "reply.v > 10", fx.enf("model_call", mode=mode, on_violation="escalate"),
                 fx.enf("edge", target="py:fixround.sconsume", mode=mode), model_policy=fx.POLICY,
                 status=status))
    forged = make_unbuilt(fx.CheckedReply)
    if mode == "detect":
        fx.sconsume(forged)
        assert fx.CONSUMED == [forged]
    else:
        e = raises(inv.Rejected, fx.sconsume, forged)
        assert e is not None and e.outcome.kind == "rule_error" and fx.CONSUMED == []


def test_non_cel_backend_exception_is_rule_error(iso):
    class Exploding:
        def evaluate(self, env: Any) -> Any:
            raise ZeroDivisionError("engine bug")

        def references(self) -> frozenset[tuple[str, ...]]:
            return frozenset({("args",)})
    r = inv.load({"rules": [{"id": "C", "status": "candidate", "bad": "args.x > 0"}]}, install=False).rules["C"]
    r.clauses = [(Exploding(), "m")]
    out = inv.evaluate(r, {"args": {"x": 1}})
    assert out.kind == "rule_error" and "ZeroDivisionError" in out.error


# --- major: async model calls, edges and providers ----------------------------------------------
def test_async_model_call_awaits_replies_and_async_providers(iso):
    load(fx.M1)
    fx.REPLIES[:] = ['{"v": 3, "cause": "x"}']
    checked = run(fx.amodel("p"))
    assert checked.verdict == "ok" and checked.v == 3
    run(fx.aconsume(checked))
    assert fx.CONSUMED == [checked]


def test_async_model_call_violation_uses_async_provider(iso):
    load(fx.M1)
    fx.REPLIES[:] = ['{"v": 30, "cause": "x"}']
    with pytest.raises(inv.Rejected):
        run(fx.amodel("p"))


def test_async_edge_rechecks_a_forged_value(iso):
    load(fx.M1)
    forged = make_checked(fx.CheckedReply, fx.Reply(99, "x"), verdict="ok")
    with pytest.raises(inv.Rejected):
        run(fx.aconsume(forged))
    assert fx.CONSUMED == []


def test_async_provider_in_async_guard_reads_after_the_effect(iso):
    load(fx.A1)
    s = fx.ASvc()

    async def main() -> None:
        await s.adoor(2)
        await s.adoor(2)                   # arows == 4 after the effect
    with inv.capture(iso) as events:
        run(main())
    assert [e["outcome"] for e in events] == ["ok", "violation"] and s.count() == 4


def test_async_provider_in_a_sync_call_is_refused_at_load(iso):
    # fix round 4: this used to load and give rule_error on every call; it is now a LoadError
    with pytest.raises(inv.LoadError, match="async def.*sync"):
        load(fx.rule("D-2", "probe2", "alimit > args.x", fx.enf(mode="detect"), status="detect"))


# --- minor: generators refused; verify checks the target ------------------------------------------
@pytest.mark.parametrize("deco", [lambda: inv.guard("X"), lambda: inv.model_call("X", fx.Reply)])
def test_generator_targets_are_refused(deco):
    def gen(n: int):
        yield n

    async def agen(n: int):
        yield n
    for fn in (gen, agen):
        with pytest.raises(TypeError, match="generator"):
            deco()(fn)


def test_verify_flags_a_guard_on_another_function(iso):
    load(fx.rule("D-3", "plain", "args.n < 0", fx.enf()))
    with pytest.raises(inv.CoverageError, match="elsewhere: guards D-3, whose target is py:fixround.plain"):
        inv.verify(modules=["fixround"])


# --- major: route, escalate and page hand off; spike demotion -------------------------------------
class Store:
    def __init__(self) -> None:
        self.got: list[inv.Handoff] = []

    def open(self, h: inv.Handoff) -> str:
        self.got.append(h)
        return f"R{len(self.got)}"


def test_route_and_escalate_hand_off_with_bound_values(iso):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf(on_violation="route")))
    store = Store()
    inv.configure(iso, review_store=store)
    e = raises(inv.Routed, fx.probe2, None, 9)
    assert e.review_id == "R1" and store.got[0].action == "route" and store.got[0].bound["args"]["x"] == 9
    assert e.bound["args"]["x"] == 9


def test_escalation_and_abstain_open_reviews(iso):
    load(fx.M2)
    store = Store()
    inv.configure(iso, review_store=store)
    fx.REPLIES[:] = ['{"v": 30, "cause": "x"}', '{"v": 1, "cause": "CANNOT_COMPLY"}']
    e1 = raises(inv.Escalation, fx.smodel, "p")
    e2 = raises(inv.Escalation, fx.smodel, "p")
    assert (e1.review_id, e2.review_id) == ("R1", "R2") and store.got[1].reason == "abstain"


def test_failing_review_store_still_stops_the_call(iso, caplog):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf(on_violation="route")))

    class Down:
        def open(self, h: inv.Handoff) -> str:
            raise OSError("store down")
    inv.configure(iso, review_store=Down())
    e = raises(inv.ReviewNotOpened, fx.probe2, None, 9)     # fix round 6: explicit, carries the work
    assert e is not None and e.review_id is None and fx.CALLS == []
    assert e.handoff.rule == "D-2" and e.action == "route" and isinstance(e.__cause__, OSError)


def test_page_calls_the_pager(iso):
    load(fx.rule("D-2", "probe2", "args.x.y > 5", fx.enf(on_error="fail_open_and_page")))
    paged: list[inv.Handoff] = []
    inv.configure(iso, pager=paged.append)
    fx.probe2(None, 1)
    assert fx.CALLS == [1] and paged[0].action == "page" and paged[0].rule == "D-2"


def test_rule_error_spike_demotes_to_detect_and_pages(iso):
    load(fx.rule("D-2", "probe2", "args.x.y > 5", fx.enf()))
    paged: list[inv.Handoff] = []
    inv.configure(iso, error_spike=inv.ErrorSpike(errors=3, window=60), pager=paged.append)
    for _ in range(3):
        assert raises(inv.Rejected, fx.probe2, None, 1) is not None
    fx.probe2(None, 1)                           # demoted: detect never blocks
    assert fx.CALLS == [1] and "D-2" in iso.demoted and len(paged) == 1 and "spike" in paged[0].reason
    iso.restore("D-2")
    assert raises(inv.Rejected, fx.probe2, None, 1) is not None


def test_violation_log_level_is_configurable(iso, caplog):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf(mode="detect"), status="detect"))
    inv.configure(iso, violation_log_level=logging.DEBUG)
    with caplog.at_level(logging.INFO, "invariants"):
        fx.probe2(None, 9)
    assert "invariant" not in caplog.text


# --- SDK ergonomics -------------------------------------------------------------------------------
def test_monitor_evaluates_rows_through_its_view_and_records(iso):
    load(fx.rule("D-2", "probe2", "big", fx.enf(view={"big": "args.obj"}), fx.enf("monitor", timing="post_effect", mode="detect",
                                                           view={"big": "args.x"})))
    with inv.capture(iso) as events:
        out = inv.monitor("D-2", [("a", {"args": {"x": True}}), ("b", {"args": {"x": False}})])
    assert [(k, o.kind) for k, o in out] == [("a", "violation"), ("b", "ok")]
    assert [(e["point"], e["key"], e["action"]) for e in events] == [("monitor", "a", "report"),
                                                                     ("monitor", "b", "report")]


def test_load_resource_reads_a_rule_file_inside_a_package(tmp_path, monkeypatch, iso):
    pkg = tmp_path / "rulespkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "invariants.yaml").write_text("spec: invariant/v0.1\nrules:\n  - {id: Q-1, status: candidate, "
                                         "bad: 'args.x > 1'}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg = inv.load_resource("rulespkg")
    assert "Q-1" in reg.rules
    with pytest.raises(inv.LoadError):
        inv.load_resource("rulespkg", "missing.yaml")


def test_make_checked_builds_a_value_with_a_verdict():
    c = make_checked(fx.CheckedReply, fx.Reply(1, "x"), verdict="violation")
    assert c.verdict == "violation" and c.v == 1
    with pytest.raises(TypeError):
        make_checked(int, 1)  # type: ignore[type-var]


def test_is_ablated_is_public(iso):
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf()))
    with inv.ablated("D-2"):
        assert inv.is_ablated("D-2")
    assert not inv.is_ablated("D-2")


def test_otel_exporter_emits_invariant_spans(iso):
    from invariants.integrations import otel

    class Span:
        def __init__(self, name: str) -> None:
            self.name, self.attrs, self.ended = name, {}, False

        def set_attributes(self, a: dict[str, Any]) -> None:
            self.attrs.update(a)

        def set_status(self, s: Any) -> None:
            self.status = s

        def end(self, end_time: int | None = None) -> None:
            self.ended = True

    class Tracer:
        def __init__(self) -> None:
            self.spans: list[Span] = []

        def start_span(self, name: str, start_time: int | None = None) -> Span:
            self.spans.append(Span(name))
            return self.spans[-1]
    t = Tracer()
    load(fx.rule("D-2", "probe2", "args.x > 5", fx.enf(mode="detect"), status="detect"))
    inv.configure(iso, exporter=otel.exporter(t))
    fx.probe2(None, 9)
    s = t.spans[0]
    assert s.name == "invariant.decorator" and s.ended
    assert s.attrs["invariant.rule_id"] == "D-2" and s.attrs["invariant.outcome"] == "violation"
    assert s.attrs["invariant.mode"] == "detect"


def test_schema_fields_are_cached():
    from invariants.loader import _dataclass_fields, schema_fields
    schema_fields(fx.Reply)
    before = _dataclass_fields.cache_info().hits
    inv.parse('{"v": 1, "cause": "x"}', fx.Reply)
    assert _dataclass_fields.cache_info().hits > before


def test_env_is_projected_once_for_every_clause(iso, monkeypatch):
    if "cel-expr" not in BACKENDS:
        pytest.skip("cel-expr not installed")
    from invariants.cel import _engine
    r = inv.load({"rules": [{"id": "C", "status": "candidate", "bad": [
        {"when": "args.x > 1", "message": "a"}, {"when": "args.x > 2", "message": "b"},
        {"when": "args.x > 3", "message": "c"}]}]}, install=False, backend="cel-expr").rules["C"]
    calls = []
    real = _engine.project

    def counting(env: Any, refs: Any) -> Any:
        calls.append(refs)
        return real(env, refs)
    monkeypatch.setattr(type(r.clauses[0][0]), "project", staticmethod(counting))
    assert inv.evaluate(r, {"args": {"x": 5}}).kind == "violation" and len(calls) == 1
