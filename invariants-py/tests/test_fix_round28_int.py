"""Regression tests for fix round 28 (guard, registry, pytest plugin, FastAPI): each failed before its fix.

1. @inv.model_call over a sync functools.wraps wrapper of an async def: the body's coroutine is
   awaited (the run is driven asynchronously, as for an async def), never parsed as the model's
   reply (a parse_error retried and then rejected on every call). Another awaitable is a TypeError.
2. `inv status --ablation` / inv.standing trust a --inv-report-json only for the version of the rule
   it ablated: the report carries each rule's digest; a different digest is "another version of the
   rule", a missing one is "no digest", neither proves the rule.
3. FastAPI: a Response a handler returns is opaque to post_effect rules (a rule_error per on_error),
   never read as the response model (result.status_code read the Response's own status_code).
4. Under pytest-xdist, --inv-ablate with no rule file exits 4 (a usage error), not 3 (INTERNALERROR).
5. --inv-ablate: an enforcing edge's refusal of a received non-ok verdict (rule_error, unbuilt) is
   a decision of the rule, so its falsifier is caught, never "inconclusive (rule_error, not violation)";
   the refusal's event carries refused: True.
6. A reply class's pydantic @computed_field or property named verdict, attempts, rule or value is
   refused at decoration, like a field of that name.
7. retry, resample and monitor events carry start_ns and end_ns (OTel spans have real times).
"""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import typing
from typing import Any

import pytest

import invariants as inv
import round28int as r28
from invariants.registry import is_decision
from invariants.registry.coverage import _ablation_problems
from invariants.testing import make_checked

pytest_plugins = ["pytester"]


def rule_digest(rule: Any) -> str:
    from invariants.registry.coverage import rule_digest as digest
    return digest(rule)

OK = json.dumps({"action": "REFUND", "amount": 5})
BIG = json.dumps({"action": "REFUND", "amount": 500})


# --- 1: a sync wrapper over an async def -------------------------------------------------------------
def test_model_call_awaits_the_coroutine_a_sync_wrapper_returns():
    with inv.isolated() as reg:
        inv.load(r28.model_doc())
        r28.REPLIES[:] = ["not json", OK]
        r28.CALLS.clear()
        got = r28.wplan("o1")
        assert asyncio.iscoroutine(got)
        checked = asyncio.run(got)
        assert checked.verdict == "ok" and checked.amount == 5 and checked.attempts == 2
        assert r28.CALLS[0] is None and r28.CALLS[1] is not None     # the parser error was fed back
        assert [(e["outcome"], e["action"]) for e in reg.sink] == [("parse_error", "retry"), ("ok", "proceed")]
        assert not any("coroutine" in str(e.get("error")) for e in reg.sink)


def test_model_call_over_a_sync_wrapper_still_judges_the_reply():
    with inv.isolated():
        inv.load(r28.model_doc())
        r28.REPLIES[:] = [BIG, BIG]
        with pytest.raises(inv.Rejected) as ei:
            asyncio.run(r28.wplan("o1"))
        assert ei.value.outcome.kind == "violation"


def test_model_call_over_a_sync_wrapper_when_passive():
    with inv.isolated():
        inv.load(r28.model_doc(model_mode="off"))
        r28.REPLIES[:] = [OK]
        checked = asyncio.run(r28.wplan("o1"))
        assert checked.verdict == "ok" and checked.amount == 5


def test_model_call_body_returning_a_future_is_a_type_error():
    async def go() -> None:
        fut = asyncio.get_running_loop().create_future()
        r28.REPLIES[:] = [fut]
        with pytest.raises(TypeError, match="not a coroutine"):
            r28.plan("o1")
    with inv.isolated() as reg:
        inv.load(r28.model_doc("plan"))
        asyncio.run(go())
        assert not reg.sink                                       # nothing was blamed on the model


# --- 2: the ablation report is for one version of the rule --------------------------------------------
def _caught(rid: str, digest: str | None) -> dict[str, Any]:
    abl: dict[str, Any] = {"rules": {rid: [{"falsifier": "t", "result": "caught"}]}, "not_load_bearing": []}
    if digest is not None:
        abl["digests"] = {rid: digest}
    return {"ablation": abl}


def test_ablation_report_for_another_version_of_the_rule_proves_nothing():
    with inv.isolated() as reg:
        inv.load(r28.model_doc())
        rule = reg.rules["W-1"]
        assert _ablation_problems(rule, _caught("W-1", rule_digest(rule))) == []
        stale = _ablation_problems(rule, _caught("W-1", "sha256:" + "0" * 64))
        assert stale and "another version of the rule" in stale[0]
        assert "no digest" in _ablation_problems(rule, _caught("W-1", None))[0]


def test_rule_digest_changes_with_bad_and_falsified_by_not_prose():
    def digest(**change: Any) -> str:
        doc = r28.model_doc()
        doc["rules"][0].update(change)
        with inv.isolated() as reg:
            inv.load(doc)
            return rule_digest(reg.rules["W-1"])
    base = digest()
    assert digest(statement="refunds stay small", owner="ops") == base
    assert digest(bad="reply.amount > 1000") != base
    assert digest(falsified_by=["tests/other.py::test_y"]) != base


APP = '''
import invariants as inv

@inv.guard("R-1")
def share(n: int) -> int:
    return 100 // n
'''
RULES = {"spec": "invariant/v0.1", "targets": {"py:plug28.share": {"effects": "none"}},
         "rules": [{"id": "R-1", "status": "enforce", "subject": {"target": "py:plug28.share"},
                    "bad": "args.n <= 0", "enforcers": [{"point": "decorator", "timing": "pre_effect",
                                                         "mode": "enforce"}],
                    "falsified_by": ["test_app.py::test_zero"]}]}
TEST = '''
import pytest, invariants as inv, plug28

def test_zero():
    with pytest.raises(inv.Rejected):
        plug28.share(0)
'''


@pytest.fixture
def plug(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(plug28=APP, test_app=TEST)
    (pytester.path / "rules.json").write_text(json.dumps(RULES))
    return pytester


def test_report_json_carries_the_digest_status_checks(plug):
    r = plug.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                                  "--inv-ablate", "--inv-report-json", "rep.json")
    assert r.ret == 0, r.stdout.str()
    rep = json.loads((plug.path / "rep.json").read_text())
    with inv.isolated() as reg:
        import sys
        sys.path.insert(0, str(plug.path))
        try:
            inv.load(plug.path / "rules.json")
        finally:
            sys.path.remove(str(plug.path))
            sys.modules.pop("plug28", None)
        rule = reg.rules["R-1"]
        assert rep["ablation"]["digests"] == {"R-1": rule_digest(rule)}
        assert _ablation_problems(rule, rep) == []
    # the rule changes after the run: the report no longer proves it
    changed = json.loads(json.dumps(RULES))
    changed["rules"][0]["bad"] = "args.n < -1000"
    with inv.isolated() as reg:
        import sys
        sys.path.insert(0, str(plug.path))
        try:
            inv.load(changed)
        finally:
            sys.path.remove(str(plug.path))
            sys.modules.pop("plug28", None)
        assert "another version" in _ablation_problems(reg.rules["R-1"], rep)[0]


# --- 3: a returned Response is opaque to post_effect rules ---------------------------------------------
def _shop() -> Any:
    from fastapi.testclient import TestClient
    from invariants.integrations import fastapi as inv_fastapi
    inv_fastapi.install(r28.app)
    return TestClient(r28.app, raise_server_exceptions=False)


@pytest.mark.parametrize("base", ["/r", "/ar"])
def test_response_attribute_named_like_a_model_field_is_not_read(base):
    c = _shop()
    with inv.isolated() as reg:
        inv.load(r28.shop_doc("result.status_code != 200"))
        assert c.get(f"{base}/5").status_code == 200
        assert c.get(f"{base}/7").status_code == 422               # the response model is judged
        r = c.get(f"{base}/-3")                                    # a JSONResponse(status_code=200)
        assert r.status_code == 503 and r.json()["outcome"] == "rule_error"
        last = list(reg.sink)[-1]
        assert last["outcome"] == "rule_error" and "returned a Response" in last["error"]


@pytest.mark.parametrize("base", ["/r", "/ar"])
def test_response_rule_error_follows_on_error(base):
    c = _shop()
    with inv.isolated():
        inv.load(r28.shop_doc("result.total < 0", on_error="fail_open_and_page"))
        r = c.get(f"{base}/-3")
        assert r.status_code == 200 and r.json()["total"] == -3


# --- 4: --inv-ablate with no rules under pytest-xdist --------------------------------------------------
def test_xdist_ablate_without_rules_is_a_usage_error(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(test_app="def test_a():\n    assert True\n")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-ablate", "-n", "2")
    assert r.ret == pytest.ExitCode.USAGE_ERROR, r.stdout.str() + r.stderr.str()
    assert "no rules loaded" in r.stderr.str()
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-ablate", "-n", "2",
                                      "--inv-rules", "nope.yaml")
    assert r.ret == pytest.ExitCode.USAGE_ERROR and "does not exist" in r.stderr.str()


def test_xdist_ablate_with_rules_still_runs(plug):
    pytest.importorskip("xdist")
    r = plug.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                                  "--inv-ablate", "-n", "2")
    assert r.ret == 0, r.stdout.str()
    r.stdout.fnmatch_lines(["*caught*test_app.py::test_zero*"])


# --- 5: an edge's refusal is a decision ----------------------------------------------------------------
def test_edge_refusal_event_is_a_decision():
    with inv.isolated() as reg:
        inv.load(r28.model_doc())
        for verdict in ("rule_error", "parse_error"):
            with pytest.raises(inv.Rejected):
                r28.execute(make_checked(r28.CheckedReply, r28.Reply("REFUND", 1), verdict=verdict, rule="W-1"))
        evs = list(reg.sink)
        assert [(e["outcome"], e["action"], e.get("refused")) for e in evs] == [
            ("rule_error", "reject", True), ("parse_error", "reject", True)]
        assert all(is_decision(e["outcome"], e) for e in evs)
    # a rule_error of the rule's own evaluation (fail_closed) is no refusal and no decision
    assert not is_decision("rule_error", {"action": "reject"})
    assert not is_decision("rule_error", {"action": "proceed", "refused": True})   # detect


FALSIFY = '''
import pytest, invariants as inv, round28int as r28
from invariants.testing import make_checked

def test_forged():
    c = make_checked(r28.CheckedReply, r28.Reply("REFUND", 1), verdict="rule_error", rule="W-1")
    with pytest.raises(inv.Rejected):
        r28.execute(c)
'''


def test_ablation_catches_a_falsifier_of_an_edge_refusal(pytester):
    import round28int
    import pathlib
    pytester.makeini(f"[pytest]\npythonpath = . {pathlib.Path(round28int.__file__).parent}\n")
    pytester.makepyfile(test_app=FALSIFY)
    doc = r28.model_doc()
    doc["rules"][0]["falsified_by"] = ["test_app.py::test_forged"]
    (pytester.path / "rules.json").write_text(json.dumps(doc))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                                      "--inv-ablate", "--inv-report-json", "rep.json")
    rep = json.loads((pytester.path / "rep.json").read_text())
    assert rep["ablation"]["rules"]["W-1"][0]["result"] == "caught", r.stdout.str()
    assert r.ret == 0


# --- 6: computed fields and properties are shadowed too ------------------------------------------------
def test_computed_field_named_like_checked_metadata_is_refused():
    from pydantic import BaseModel, ConfigDict, computed_field

    class J(BaseModel):
        model_config = ConfigDict(extra="forbid")
        decision: typing.Literal["allow", "deny"]

        @computed_field  # type: ignore[prop-decorator]
        @property
        def verdict(self) -> str:
            return self.decision

    with pytest.raises(TypeError, match="'verdict' is shadowed"):
        @inv.model_call("J-1", schema=J, returns=r28.CheckedReply)
        def judge(x: str) -> str:
            return "{}"


@pytest.mark.parametrize("deco", [property, functools.cached_property])
def test_property_named_like_checked_metadata_is_refused(deco):
    def attempts(self: Any) -> int:
        return 99

    D = dataclasses.make_dataclass("D", [("n", int)], namespace={"attempts": deco(attempts)})
    if deco is functools.cached_property:
        D.attempts.__set_name__(D, "attempts")
    with pytest.raises(TypeError, match="'attempts' is shadowed"):
        @inv.model_call("J-1", schema=D, returns=r28.CheckedReply)
        def judge(x: str) -> str:
            return "{}"


def test_other_properties_are_fine():
    @dataclasses.dataclass
    class D:
        n: int

        @property
        def doubled(self) -> int:
            return self.n * 2

    @inv.model_call("J-1", schema=D, returns=r28.CheckedReply)
    def judge(x: str) -> str:
        return "{}"


# --- 7: retry, resample and monitor events carry their times -------------------------------------------
def test_retry_and_resample_events_carry_start_and_end():
    with inv.isolated() as reg:
        inv.load(r28.model_doc("plan"))
        r28.REPLIES[:] = ["nope", BIG, OK]
        r28.plan("o1")
        evs = list(reg.sink)
        assert [e["action"] for e in evs] == ["retry", "resample", "proceed"]
        for e in evs:
            assert 0 < e["start_ns"] <= e["end_ns"], e
        assert evs[0]["end_ns"] <= evs[1]["start_ns"]        # each attempt is its own span


def test_monitor_events_carry_start_and_end():
    with inv.isolated() as reg:
        inv.load(r28.monitor_doc())
        inv.monitor("MON-1", [("a", {"args": {"n": -1}}), ("b", {"args": {"n": 1}})])
        evs = list(reg.sink)
        assert [e["key"] for e in evs] == ["a", "b"]
        for e in evs:
            assert 0 < e["start_ns"] <= e["end_ns"], e


def test_status_says_to_rerun_a_stale_ablation_report(tmp_path):
    """`inv status --ablation` with a report for another version of the rule says to re-run it, not to
    write a new falsifier."""
    import json
    import textwrap
    from cli_support import run_inv, write_project
    root = write_project(tmp_path, {
        "pay.py": textwrap.dedent("""
            import invariants as inv

            @inv.guard("PAY-1")
            def refund(amount: int) -> None:
                pass
        """),
        "invariants.yaml": textwrap.dedent("""
            rules:
              - id: PAY-1
                guard: py:pay.refund
                bad: args.amount <= 0
                tests: [tests/test_pay.py::test_refuses]
        """)})
    for digest in ("sha256:" + "0" * 64, None):
        abl: dict[str, Any] = {"rules": {"PAY-1": [{"falsifier": "tests/test_pay.py::test_refuses",
                                                   "result": "caught"}]}, "not_load_bearing": []}
        if digest:
            abl["digests"] = {"PAY-1": digest}
        (root / "r.json").write_text(json.dumps({"rules": {}, "ablation": abl}))
        r = run_inv("status", "--import", "pay", "--ablation", "r.json", cwd=root)
        assert "the ablation report is stale; re-run it" in r.stdout, r.stdout + r.stderr
