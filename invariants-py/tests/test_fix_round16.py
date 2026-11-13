"""Fix round 16 (runtime review findings): each test failed before its fix.

1. A model call skips a retired rule and an enforcer in mode off (off was detect); act() is a
   no-op for an off enforcer.
2. old_self paths come from bind and every enforcer view too, not only from bad.
3. self is a root in model calls and edges on methods, as in guards.
4. Load-time scope checks: result before the call or in a model call or edge, reply in a guard,
   old_self in a model call, edge or monitor, args.self and args.inv_feedback.
5. An ablated model call rule skips its format retries and abstain escalation too.
6. An edge uses the enforcer that targets it, not the rule's first edge enforcer.
7. Model calls and edges pin one registry generation; standing() sees a spike demotion;
   a reload clears the demotion of a rule that changed.
8. Standing: live mediation.allow back doors, a chokepoint naming no function, missing ablation
   coverage and a misplaced py: guard withhold held; verify runs the FastAPI route check.
9. A TypedDict is typed by its fields; a {field: type} reply schema is closed; the JSON Schema
   allows views on middleware and monitor enforcers and declares subject.operation.
10. A snapshot takes bytes, datetimes, durations, UUIDs and Enums; has() on an absent field is false.
11. Provider cache keys keep the type; async providers are gathered; only providers a clause
    reads run; one provider under several names; inv.Dependency for context injection.
12. A pydantic reply stays a model instance; Field(exclude=True); Enums with float and tuple
    values; a naive datetime is a parse_error; an Any field is copied at an edge.
13. model_call(returns=...) types a body that returns raw text (mypy strict).
14. JsonlReviews has an async open, and async guards use it.
15. OpenTelemetry spans have real start and end times and ERROR status on violation and rule_error.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import time
from typing import Any, Iterator

import pytest

import invariants as inv
import round16 as r16
from invariants import status_report
from invariants.evaluate import act
from invariants.model import Outcome
from invariants.testing import make_checked


@pytest.fixture
def iso() -> Iterator[inv.Registry]:
    with inv.isolated() as reg:
        r16.REPLIES.clear()
        r16.HOOK.clear()
        r16.CALLS["n"] = 0
        r16.PROVIDER_CALLS.clear()
        yield reg


def load_error(doc: dict[str, Any]) -> str:
    with pytest.raises(inv.LoadError) as ei:
        inv.load(doc, install=False)
    return str(ei.value)


def events(evs: list[dict[str, Any]], rid: str) -> list[dict[str, Any]]:
    return [e for e in evs if e.get("rule") == rid]


# --- 1. off and retired model call rules ----------------------------------------------------------------
def test_a_retired_model_call_rule_is_skipped(iso):
    inv.load(r16.model_doc(status="retired"))
    r16.REPLIES[:] = [r16.reply(n=10)]
    with inv.capture(iso) as evs:
        c = r16.ask()
    assert c.verdict == "ok" and c.value.n == 10 and events(evs, "M-1") == []


def test_an_off_model_call_enforcer_is_skipped_and_the_edge_still_checks(iso):
    inv.load(r16.model_doc(mode="off", edges={"use_a": "enforce"}))
    r16.REPLIES[:] = [r16.reply(n=10)]
    with inv.capture(iso) as evs:
        c = r16.ask()
    assert c.verdict == "ok" and events(evs, "M-1") == []
    with pytest.raises(inv.Rejected):
        r16.use_a(c)


def test_act_is_a_noop_for_an_off_enforcer(iso):
    inv.load(r16.model_doc(edges={"use_a": "enforce"}))
    rule = iso.rules["M-1"]
    with inv.capture(iso) as evs:
        assert act(iso, rule, {**rule.enforcers[0], "mode": "off"}, Outcome("violation", "M-1", ["x"])) == "proceed"
    assert evs == []


# --- 2. old_self from bind and views ---------------------------------------------------------------------
@pytest.mark.parametrize("how", ["bind", "view"])
def test_old_self_read_through_bind_or_view_is_snapshotted(iso, how):
    kw: dict[str, Any] = {how: {"prev": "old_self.balance"}}
    inv.load(r16.guard_doc("G-OLD", "Account.withdraw", "prev - self.balance > 100", timing="post_effect", **kw))
    a = r16.Account()
    assert a.withdraw(50) == 450
    with pytest.raises(inv.Rejected) as ei:
        a.withdraw(150)
    assert ei.value.outcome.kind == "violation"


# --- 3. self in model calls and edges ------------------------------------------------------------------
def test_model_call_and_edge_methods_read_self(iso):
    inv.load(r16.model_doc("M-SELF", "reply.n > self.limit", edges={"Agent.use": "enforce"}))
    ag = r16.Agent(3)
    r16.REPLIES[:] = [r16.reply(n=2)]
    c = ag.ask()
    assert c.verdict == "ok" and ag.use(c) == "used"
    r16.REPLIES[:] = [r16.reply(n=9)]
    with pytest.raises(inv.Rejected) as ei:
        ag.ask()
    assert ei.value.outcome.kind == "violation"
    with pytest.raises(inv.Rejected) as ei:
        ag.use(make_checked(r16.CS, r16.Reply("A", 9)))
    assert ei.value.outcome.kind == "violation"


# --- 4. load-time scope checks -------------------------------------------------------------------------
MONITOR = {"rules": [{"id": "MON", "status": "detect", "bad": "old_self.n > 1",
                      "enforcers": [{"point": "monitor", "timing": "post_effect", "mode": "detect"}]}]}


@pytest.mark.parametrize("doc,needle", [
    (r16.model_doc(bad="result.n > 1"), "result"),
    (r16.guard_doc("G-P", "pf", "result == null"), "result"),
    (r16.guard_doc("G-P", "pf", "reply.n > 1"), "reply"),
    (r16.model_doc(bad="old_self.n > 1"), "old_self"),
    (r16.model_doc(bad="reply.n > 1", edges={"use_a": "enforce"}, view={"m": "old_self.n"}), "old_self"),
    (MONITOR, "old_self"),
    (r16.guard_doc("G-SELF", "Account.spend", "args.self == null"), "args.self"),
    (r16.model_doc(bad="args.inv_feedback == 'x'"), "args.inv_feedback"),
])
def test_a_root_the_enforcer_never_sets_is_a_load_error(doc, needle):
    err = load_error(doc)
    assert needle in err and "never" in err, err


@pytest.mark.parametrize("doc", [
    r16.guard_doc("G-P", "pf", "result != null", timing="post_effect"),
    r16.guard_doc("G-OLD", "Account.withdraw", "old_self.balance < self.balance"),
    r16.guard_doc("G-OLD", "Account.withdraw", "old_self.balance < self.balance", timing="post_effect"),
    r16.guard_doc("G-SELF", "Account.spend", "args.amount > self.limit"),
])
def test_roots_the_enforcer_sets_still_load(doc):
    inv.load(doc, install=False)


# --- 5. ablation skips format retries and abstain ------------------------------------------------------
def test_an_ablated_model_call_skips_format_retries_and_abstain(iso):
    inv.load(r16.model_doc(fmt=2))
    r16.REPLIES[:] = ["not json", r16.reply()]
    with inv.ablated("M-1"):
        c = r16.ask()
    assert r16.CALLS["n"] == 1 and c.verdict == "parse_error"
    r16.REPLIES[:] = [r16.reply("CANNOT_COMPLY")]
    with inv.ablated("M-1"):
        c = r16.ask()
    assert c.verdict == "ok" and c.value.cause == "CANNOT_COMPLY"


# --- 6. the edge's own enforcer ------------------------------------------------------------------------
@pytest.mark.parametrize("strict,lax", [("use_a", "use_b"), ("use_b", "use_a")])
def test_each_edge_uses_the_enforcer_that_targets_it(iso, strict, lax):
    inv.load(r16.model_doc(edges={strict: "enforce", lax: "detect"}))
    bad = make_checked(r16.C16, r16.Reply("A", 9))
    assert getattr(r16, lax)(bad) == lax[-1]
    with pytest.raises(inv.Rejected):
        getattr(r16, strict)(bad)


# --- 7. one generation per call; demotion ----------------------------------------------------------------
LIM_A = {"lim": {"type": "int", "from": "py:round16.lim_a", "args": {}}}
LIM_B = {"lim": {"type": "int", "from": "py:round16.lim_b", "args": {}}}


@pytest.mark.parametrize("rid", ["M-1", "M-A"])
def test_a_model_call_keeps_the_generation_it_started_under(iso, rid):
    inv.load(r16.model_doc(rid, bad="reply.n > lim", providers=LIM_A))
    r16.HOOK["before"] = lambda: inv.load(r16.model_doc(rid, bad="reply.n > lim", providers=LIM_B))
    r16.REPLIES[:] = [r16.reply(n=10)]
    with pytest.raises(inv.Rejected):
        r16.ask() if rid == "M-1" else asyncio.run(r16.aask())
    assert iso.providers["lim"].fn is r16.lim_b          # the reload happened


def test_standing_sees_a_spike_demotion(iso):
    inv.load(r16.model_doc(edges={"use_a": "enforce"}))
    assert inv.standing("M-1")[0] == "enforce(known-callers)"
    iso.demoted["M-1"] = "spike"
    assert inv.standing("M-1")[0] == "monitored"


def test_a_reload_clears_the_demotion_of_a_changed_rule_only(iso):
    inv.load(r16.model_doc(edges={"use_a": "enforce"}))
    iso.demoted["M-1"] = "spike"
    inv.load(r16.model_doc(edges={"use_a": "enforce"}))
    assert "M-1" in iso.demoted
    inv.load(r16.model_doc(bad="reply.n > 6", edges={"use_a": "enforce"}))
    assert "M-1" not in iso.demoted


# --- 8. standing honesty and verify ---------------------------------------------------------------------
def standing_reasons(mediation: dict[str, Any], **kw: Any) -> list[str]:
    inv.load(r16.guard_doc("G-P", "pf", "args.x > 5", mediation=mediation))
    st, why = inv.standing("G-P", scan=True, **kw)
    assert st != "held"
    return why


def test_live_allow_entries_withhold_held(iso):
    why = standing_reasons({"chokepoint": "pf is the only door", "allow": ["py:round16.backdoor"]})
    assert any("back door" in w and "round16.backdoor" in w for w in why), why
    why = standing_reasons({"chokepoint": "pf is the only door", "allow": ["py:round16.backdoor"],
                            "residual_paths": ["py:round16.backdoor"]})
    assert not any("back door" in w for w in why) and any("residual" in w for w in why), why


@pytest.mark.parametrize("chokepoint,bad", [
    ("pf is the only door", False),
    ("@inv.guard on round16.pf, the only public entry", False),
    ("every write goes through the front", True),
    ("py:round16.nope is the door", True),
])
def test_a_chokepoint_must_name_a_function_of_the_rule(iso, chokepoint, bad):
    why = standing_reasons({"chokepoint": chokepoint})
    assert any("chokepoint" in w for w in why) is bad, why


def test_missing_ablation_coverage_withholds_held(iso):
    med = {"chokepoint": "pf is the only door"}
    for rows, bad in (([{"falsifier": "t", "result": "caught"}], False),
                      ([{"falsifier": "t", "result": "still passes"}], True), (None, True)):
        ablation = {"ablation": {"rules": {"G-P": rows} if rows else {}}}
        why = standing_reasons(med, ablation=ablation)
        assert any("ablation" in w for w in why) is bad, why


def test_verify_reverse_checks_py_guard_keys(iso):
    inv.load(r16.guard_doc("G-P", "pf", "args.x > 5"))
    with pytest.raises(inv.CoverageError) as ei:
        iso.verify(["round16"])
    assert "misplaced: guards py:round16.elsewhere" in str(ei.value)


def test_verify_runs_the_route_check_of_an_installed_fastapi_app(iso):
    from fastapi import FastAPI
    from invariants.integrations import fastapi as inv_fastapi
    doc = r16.guard_doc("G-P", "pf", "args.x > 5")
    doc["contracts"] = {"api": {"type": "openapi", "operation": "doX"}}
    doc["rules"][0]["subject"]["contract"] = "api"
    inv.load(doc)
    app = FastAPI()

    @app.post("/x", operation_id="doX")
    def handler() -> None:
        return None
    inv_fastapi.install(app)
    ok, problems = status_report.verify_result(iso)
    assert not ok and any("doX" in p and "not wrapped" in p for p in problems), problems


# --- 9. typecheck and the JSON Schema --------------------------------------------------------------------
@pytest.mark.parametrize("bad,needle", [("args.t.n > 1", "ill-typed"), ("args.t.nope == 1", "nope")])
def test_a_typeddict_is_typed_by_its_fields(bad, needle):
    assert needle in load_error(r16.guard_doc("G-TD", "td", bad))
    inv.load(r16.guard_doc("G-TD", "td", "args.t.k > 1"), install=False)


def test_a_field_type_map_schema_is_closed():
    err = load_error(r16.model_doc("M-MAP", "reply.x == 1", target="py:round16.ask_map"))
    assert "'x'" in err and "ill-typed" in err, err
    inv.load(r16.model_doc("M-MAP", "reply.n > 1", target="py:round16.ask_map"), install=False)


def test_the_json_schema_allows_views_on_middleware_and_monitor_and_subject_operation():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(inv.schema_path().read_text())
    v = jsonschema.Draft202012Validator(schema)
    doc = {"rules": [{"id": "X", "status": "detect", "bad": "a > 1",
                      "subject": {"target": "py:m.f", "operation": "createOrder"},
                      "enforcers": [{"point": p, "timing": "pre_effect", "mode": "detect", "view": {"a": "args.a"}}
                                    for p in ("middleware", "monitor", "decorator")]}]}
    assert list(v.iter_errors(doc)) == []


# --- 10. snapshots ---------------------------------------------------------------------------------------
def test_a_snapshot_takes_scalar_values(iso):
    clauses = ["old_self.blob != old_self.blob", "old_self.at > timestamp('2100-01-01T00:00:00Z')",
               "old_self.wait > duration('100h')", "old_self.uid == 'x'", "old_self.color == 'b'"]
    inv.load(r16.guard_doc("G-SNAP", "Account.touch", [{"when": c, "message": c} for c in clauses]))
    r16.Account().touch()
    a = r16.Account()
    a.color = r16.Color.BLUE
    with pytest.raises(inv.Rejected) as ei:
        a.touch()
    assert ei.value.outcome.messages == ["old_self.color == 'b'"]


def test_a_snapshot_of_a_date_is_refused_as_cel_refuses_it(iso):
    inv.load(r16.guard_doc("G-SNAP", "Account.touch", "old_self.day == null"))
    with pytest.raises(inv.Rejected) as ei:
        r16.Account().touch()
    assert ei.value.outcome.kind == "rule_error" and "datetime.date" in ei.value.outcome.error


def test_has_on_an_absent_snapshot_field_is_false(iso):
    inv.load(r16.guard_doc("G-SNAP", "Account.touch", "has(old_self.meta.x)"))
    r16.Account().touch()
    with pytest.raises(inv.Rejected) as ei:
        r16.Account(meta={"x": 1}).touch()
    assert ei.value.outcome.kind == "violation"


# --- 11. providers ---------------------------------------------------------------------------------------
def test_provider_cache_keys_keep_the_type(iso):
    prov = {"flag": {"type": "str", "from": "py:round16.flag", "args": {"x": "v"}}}
    doc = r16.guard_doc("P-1", "pf2", "f != 'int'", providers=prov, bind={"v": "args.x", "f": "provider.flag"})
    second = r16.guard_doc("P-2", "pf2", "f != 'bool'", bind={"v": "args.y", "f": "provider.flag"})["rules"][0]
    doc["rules"].append(second)
    inv.load(doc)
    r16.pf2(1, True)
    assert sorted(r16.PROVIDER_CALLS) == [("flag", 1), ("flag", True)]


def test_async_providers_are_awaited_together(iso):
    prov = {n: {"type": "int", "from": f"py:round16.{n}", "args": {"x": "args.x"}} for n in ("slow1", "slow2")}
    inv.load(r16.guard_doc("G-AP", "apf", "slow1 + slow2 > 100", providers=prov))
    asyncio.run(r16.apf(1))
    kinds = [k for k, _ in r16.PROVIDER_CALLS]
    assert max(kinds.index("slow1-start"), kinds.index("slow2-start")) < min(kinds.index("slow1-end"),
                                                                             kinds.index("slow2-end")), kinds


def test_a_provider_no_clause_reads_is_not_called(iso):
    prov = {"costly": {"type": "int", "from": "py:round16.costly", "args": {"x": "args.x"}}}
    inv.load(r16.guard_doc("G-P", "pf", "args.x > 5", providers=prov, bind={"c": "provider.costly"}))
    r16.pf(1)
    assert r16.PROVIDER_CALLS == []


def test_one_provider_function_under_several_names(iso):
    prov = {n: {"type": "bool", "from": "py:round16.known", "args": {"x": f"args.{a}"}}
            for n, a in (("known", "x"), ("also_known", "y"))}
    inv.load(r16.guard_doc("G-P", "pf", "known && also_known", providers=prov))
    r16.pf(1, -1)
    with pytest.raises(inv.Rejected):
        r16.pf(1, 1)


def test_a_dependency_injects_context_into_a_provider(iso):
    prov = {"stored": {"type": "int", "from": "py:round16.stored", "args": {"key": "args.key"}}}
    inv.load(r16.guard_doc("G-P", "pf", "stored > 5", providers=prov))
    with r16.STORE.bound({"a": 7, "b": 1}):
        r16.pf(0, key="b")
        with pytest.raises(inv.Rejected) as ei:
            r16.pf(0, key="a")
        assert ei.value.outcome.kind == "violation"
    with pytest.raises(inv.Rejected) as ei:
        r16.pf(0, key="a")                        # nothing bound: the provider fails, a rule_error
    assert ei.value.outcome.kind == "rule_error"
    with pytest.raises(LookupError, match="round16_store"):
        r16.STORE.get()


# --- 12. pydantic replies --------------------------------------------------------------------------------
def test_a_pydantic_reply_stays_a_model_instance(iso):
    inv.load(r16.pd_doc("M-PD", "ask_pd", "PR", "reply.inner.x > 5", edge="use_pd"))
    r16.REPLIES[:] = [r16.pd_reply()]
    c = r16.ask_pd()
    assert isinstance(c.value, r16.PR) and c.value.level is r16.Fl.LOW and c.value.secret == "x"
    assert c.value.at.tzinfo is not None and isinstance(c.value.inner, r16.Inner)
    got = r16.use_pd(c)
    assert isinstance(got, r16.PR) and got == c.value and got is not c.value and got.inner is not c.value.inner


def test_a_float_enum_reply_is_read_as_a_number(iso):
    inv.load(r16.pd_doc("M-PD", "ask_pd", "PR", "reply.level > 2.0"))
    r16.REPLIES[:] = [r16.pd_reply(level=2.5)]
    with pytest.raises(inv.Rejected) as ei:
        r16.ask_pd()
    assert ei.value.outcome.kind == "violation"


def test_a_tuple_enum_schema_builds_and_parses_without_crashing():
    from invariants.integrations import pydantic as inv_pd
    inv_pd.closed(r16.PT)
    try:
        inv.parse('{"cause": "A", "pair": [1, 2]}', r16.PT)
    except inv.ParseError:
        pass
    got = inv.parse('{"cause": "A", "many": [1, 2]}', r16.PL)      # pydantic validates the Enum
    assert isinstance(got, r16.PL) and got.many is r16.Many.SOME


def test_a_naive_datetime_is_a_parse_error_that_retries(iso):
    inv.load(r16.pd_doc("M-PD", "ask_pd", "PR", "reply.at > timestamp('2100-01-01T00:00:00Z')", fmt=1))
    r16.REPLIES[:] = [r16.pd_reply(at="2024-06-01T12:00:00"), r16.pd_reply()]
    c = r16.ask_pd()
    assert c.verdict == "ok" and c.attempts == 2


def test_an_any_field_is_copied_at_the_edge(iso):
    inv.load(r16.pd_doc("M-ANY", "ask_any", "AnyReply", "size(reply.blob.k) > 3", edge="use_any"))
    r16.REPLIES[:] = [{"cause": "A", "blob": {"k": [1]}}]
    c = r16.ask_any()
    received = c.value.blob
    r16.HOOK["during"] = lambda: received["k"].extend([2, 3, 4, 5])
    r16.use_any(c)
    assert r16.SEEN["before"] == r16.SEEN["after"]


# --- 13. model_call(returns=...) under mypy strict ---------------------------------------------------------
@pytest.mark.skipif(shutil.which("mypy") is None and not (pathlib.Path(sys.executable).parent / "mypy").exists(),
                    reason="mypy not installed")
def test_model_call_returns_types_a_raw_text_body(tmp_path):
    snippet = tmp_path / "typed_returns.py"
    snippet.write_text(textwrap.dedent('''
        import dataclasses
        from typing import Literal, assert_type
        import invariants as inv
        from invariants.integrations import pydantic as invp

        @dataclasses.dataclass(frozen=True)
        class Reply:
            cause: Literal["A", "B"]

        class CheckedReply(inv.Checked):
            __slots__ = ()

        @inv.model_call("R-1", schema=Reply, returns=CheckedReply)
        def propose(prompt: str, inv_feedback: str | None = None) -> str:
            return "{}"

        @inv.model_call("R-2", schema=Reply, returns=CheckedReply)
        async def apropose(prompt: str) -> str:
            return "{}"

        @invp.model_call("R-3", schema=Reply, returns=CheckedReply)
        def ppropose(prompt: str) -> dict[str, str]:
            return {}

        @inv.model_call("R-4", schema=Reply)
        def legacy(prompt: str) -> CheckedReply:
            raise NotImplementedError

        async def main() -> None:
            assert_type(propose("x"), CheckedReply)
            assert_type(await apropose("x"), CheckedReply)
            assert_type(ppropose("x"), CheckedReply)
            assert_type(legacy("x"), CheckedReply)
    '''))
    src = pathlib.Path(inv.__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "MYPYPATH"}
    if src.name not in ("site-packages", "dist-packages"):
        env["MYPYPATH"] = str(src)
    r = subprocess.run([sys.executable, "-m", "mypy", "--no-incremental", "--strict", "--follow-imports=silent",
                        str(snippet)], cwd=tmp_path, capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_model_call_returns_at_run_time(iso):
    inv.load(r16.model_doc(rid="M-R", target="py:round16.ask_raw"))
    r16.REPLIES[:] = [r16.reply(n=1)]
    c = r16.ask_raw()
    assert isinstance(c, r16.C16) and c.verdict == "ok"
    with pytest.raises(TypeError, match="returns"):
        inv.model_call("M-R", schema=r16.Reply, returns=str)       # type: ignore[call-overload]


# --- 14. JsonlReviews off the event loop ---------------------------------------------------------------
def test_jsonl_reviews_has_an_async_open_and_async_guards_use_it(iso, tmp_path, monkeypatch):
    store = inv.JsonlReviews(tmp_path / "r.jsonl")
    rid = asyncio.run(store.aopen(inv.Handoff("X", "route")))
    assert rid.startswith("jsonl-") and json.loads((tmp_path / "r.jsonl").read_text())["id"] == rid
    threads: list[Any] = []
    real = store.open

    def spy(h: inv.Handoff) -> str:
        import threading
        threads.append(threading.current_thread())
        return real(h)
    monkeypatch.setattr(store, "open", spy)
    doc = r16.guard_doc("G-AP", "apf", "args.x > 5")
    doc["rules"][0]["enforcers"][0]["on_violation"] = "route"
    inv.load(doc)
    inv.configure(iso, review_store=store)

    async def main() -> Any:
        loop_thread = __import__("threading").current_thread()
        with pytest.raises(inv.Routed):
            await r16.apf(9)
        return loop_thread
    loop_thread = asyncio.run(main())
    assert threads and threads[0] is not loop_thread


# --- 15. OpenTelemetry spans ----------------------------------------------------------------------------
class _Span:
    def __init__(self, name: str, start_time: int | None) -> None:
        self.name, self.start, self.end_time, self.attrs, self.status = name, start_time, None, {}, None

    def set_attributes(self, a: dict[str, Any]) -> None:
        self.attrs.update(a)

    def set_status(self, s: Any, description: str | None = None) -> None:
        self.status = s

    def end(self, end_time: int | None = None) -> None:
        self.end_time = end_time


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, name: str, start_time: int | None = None) -> _Span:
        self.spans.append(_Span(name, start_time))
        return self.spans[-1]


def test_otel_spans_have_real_times_and_error_status(iso):
    pytest.importorskip("opentelemetry.trace")
    from opentelemetry.trace import StatusCode
    from invariants.integrations import otel
    t = _Tracer()
    prov = {"snooze": {"type": "int", "from": "py:round16.snooze", "args": {"x": "args.x"}}}
    doc = r16.guard_doc("G-SLOW", "slow", [{"when": "snooze > 8", "message": "big"},
                                           {"when": "1 / (snooze - 3) > 5", "message": "err"}],
                        providers=prov)
    doc["rules"][0]["enforcers"][0]["on_error"] = "fail_open_and_page"
    inv.load(doc)
    inv.configure(iso, exporter=otel.exporter(t))
    r16.slow(1)
    with pytest.raises(inv.Rejected):
        r16.slow(9)
    r16.slow(3)                                   # division by zero: a rule_error, failing open
    ok, bad, err = t.spans
    for s in (ok, bad, err):
        assert s.start is not None and s.end_time is not None and s.end_time - s.start >= 20_000_000, vars(s)
    assert ok.status is None or ok.status.status_code != StatusCode.ERROR
    assert bad.status is not None and bad.status.status_code == StatusCode.ERROR
    assert err.status is not None and err.status.status_code == StatusCode.ERROR
