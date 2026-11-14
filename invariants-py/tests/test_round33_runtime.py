"""Regression tests for round 33 (runtime): each failed before its fix.

H3. A rule without subject.target that has a decorator and a middleware enforcer is miswired on a
    guard that evaluates only one of the two points: verify() and verify_routes() say so (a target's
    guard was already checked so).
H4. A bare list / dict / tuple / set / frozenset schema field is read like Any: a NaN or Infinity in it
    is a parse_error, and the reply holds a copy (a change to the value handed over after the check
    is not seen).
H5. Below effective enforce a model call's parse failure is one model call: no format retry, no
    feedback; the parse_error is recorded (action proceed) and the call goes on. Sync and async.
H6. Under INV_CONFINE_ROOT a reference ends at an object of the project: not a module that
    sys.modules holds from outside the root under a project name, not `from os import environ`
    (one hop), not a function defined outside the root.
M3. A provider argument that is unhashable is cached by identity while the cache keeps it alive: a
    later argument at the same address never reads the first one's value.
M4. A model call nested in another's body, on the same chat model, keeps its own conversation: the
    outer call's feedback continues the outer conversation. Sync and async.
L1. Exceptions never carry the values a rule read: .bound is the bound names, .outcome has none.
"""
from __future__ import annotations

import asyncio
import gc
import json
import math
import sys
from typing import Any

import pytest
from fastapi import FastAPI

import invariants as inv
import round33rt as m
import round29rt as m29
from invariants.confinement import CONFINE_ENV
from invariants.guard.replies import parse
from invariants.integrations import fastapi as inv_fastapi
from invariants.providers import fetch_providers
from invariants.resolve import resolve, resolve_ref


def _run(x: Any) -> Any:
    return asyncio.run(x) if asyncio.iscoroutine(x) else x


# --- H3 ------------------------------------------------------------------------------------------------
def test_verify_finds_a_targetless_middleware_enforcer_the_guard_does_not_evaluate():
    with inv.isolated() as reg:
        inv.load(m.targetless_doc())
        with pytest.raises(inv.CoverageError, match="the guard on plain does not evaluate point middleware"):
            reg.verify(["round33rt"])
    with inv.isolated() as reg:                       # one point the guard evaluates: as before
        inv.load(m.targetless_doc(("decorator",)))
        with pytest.raises(inv.CoverageError) as ei:  # (the fixture's model calls name rules not loaded here)
            reg.verify(["round33rt"])
        assert "plain:" not in str(ei.value) and "does not evaluate" not in str(ei.value)


def _app(guard: Any) -> FastAPI:
    app = FastAPI()

    @app.post("/t/{x}", operation_id="t")
    @guard("T-1")
    def t(x: int) -> dict[str, int]:
        return {"x": x}
    return app


def test_verify_routes_checks_the_points_of_a_targetless_rule():
    doc = m.targetless_doc(subject={"contract": "api", "operation": "t"})
    contracts = {"contracts": {"api": {"type": "openapi"}}}
    with inv.isolated() as reg:
        inv.load(doc)
        with pytest.raises(inv.CoverageError, match="does not evaluate point middleware"):
            inv_fastapi.verify_routes(_app(inv.guard), contracts, reg)
        assert inv_fastapi.verify_routes(_app(inv_fastapi.guard), contracts, reg) == 1


# --- H4 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("t,v", [(list, [1.0, math.nan]), (dict, {"a": math.inf}), (tuple, (math.nan,)),
                                 (set, {-math.inf}), (frozenset, frozenset({math.nan})),
                                 (list, [[{"deep": math.nan}]])])
def test_a_bare_container_field_refuses_non_finite_numbers(t, v):
    with pytest.raises(inv.ParseError, match="non-finite"):
        parse({"xs": v}, {"xs": t})


def test_a_bare_container_field_is_a_copy():
    inner = [1]
    held = {"k": inner}
    out = parse({"xs": [inner], "d": held}, {"xs": list, "d": dict})
    inner.append(2)
    held["new"] = [3]
    assert out == {"xs": [[1]], "d": {"k": [1]}}
    assert parse('{"xs": [1, {"a": 2}]}', {"xs": list}) == {"xs": [1, {"a": 2}]}     # JSON text: as before


# --- H5 ------------------------------------------------------------------------------------------------
def _model_doc(shape: str, mode: str) -> dict[str, Any]:
    doc = m29.model_doc(shape)
    r = doc["rules"][0]
    r["model_policy"]["format_retries"] = 2
    r["bad"] = "reply.amount > 100"
    r["status"] = "enforce" if mode == "enforce" else "detect"
    r["enforcers"][0].pop("view")
    r["enforcers"][0]["mode"] = mode
    return doc


@pytest.mark.parametrize("shape", ["sync", "async", "sync_of_async"])
def test_below_enforce_a_parse_failure_is_one_model_call(shape):
    fn = getattr(m29, f"m_{shape}")
    with inv.isolated() as reg:
        inv.load(_model_doc(shape, "detect"))
        m29.REPLIES[:] = ["not json", "nope", json.dumps({"action": "REFUND", "amount": 5})]
        c = _run(fn("o"))
        assert c.verdict == "parse_error" and len(m29.REPLIES) == 2
        assert [(e["outcome"], e["action"]) for e in reg.sink] == [("parse_error", "proceed")]


def test_below_enforce_a_demoted_rule_does_not_retry_either():
    with inv.isolated() as reg:
        inv.load(_model_doc("sync", "enforce"))
        with reg._lock:
            reg.demoted["M-sync"] = 1                  # an error spike demoted it: effective detect
        m29.REPLIES[:] = ["not json", json.dumps({"action": "REFUND", "amount": 5})]
        assert m29.m_sync("o").verdict == "parse_error" and len(m29.REPLIES) == 1


@pytest.mark.parametrize("shape", ["sync", "async"])
def test_at_enforce_format_retries_run_as_before(shape):
    fn = getattr(m29, f"m_{shape}")
    with inv.isolated() as reg:
        inv.load(_model_doc(shape, "enforce"))
        m29.REPLIES[:] = ["not json", "nope", json.dumps({"action": "REFUND", "amount": 5})]
        assert _run(fn("o")).verdict == "ok" and not m29.REPLIES
        assert [e["action"] for e in reg.sink] == ["retry", "retry", "proceed"]


# --- H6 ------------------------------------------------------------------------------------------------
@pytest.fixture
def confined_app(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "app33").mkdir(parents=True)
    (root / "app33" / "__init__.py").write_text(
        "import json, os, sys\nfrom os import environ, getcwd\nsys.modules['app33.leak'] = os\nenc = json.JSONEncoder().encode\n"
        "SCHEMA = {'a': 1}\nLIMIT = 100\n\n\ndef refund(x: int) -> int:\n    return x\n\n\n"
        "class Svc:\n    def go(self) -> None:\n        pass\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv(CONFINE_ENV, str(root))
    yield root
    for name in [n for n in sys.modules if n.split(".")[0] == "app33"]:
        del sys.modules[name]


@pytest.mark.parametrize("target", ["py:app33.environ", "py:app33.getcwd", "py:app33.enc", "py:app33.leak",
                                    "py:app33.leak.environ", "py:app33.os", "py:os.environ"])
def test_confined_resolve_refuses_an_outside_object(confined_app, target):
    with pytest.raises(inv.LoadError, match="outside|not defined inside|not a file inside"):
        resolve(target)


@pytest.mark.parametrize("ref", ["app33:environ", "app33.leak:environ", "app33:getcwd"])
def test_confined_resolve_ref_refuses_an_outside_object(confined_app, ref):
    with pytest.raises(inv.LoadError, match="outside|not defined inside|not a file inside"):
        resolve_ref(ref, "contract c")


def test_confined_resolve_still_reaches_the_project_s_own_objects(confined_app):
    assert resolve("py:app33.refund")(3) == 3 and resolve("py:app33.Svc.go") is not None
    assert resolve("py:app33.SCHEMA") == {"a": 1} and resolve("py:app33.LIMIT") == 100
    assert resolve_ref("app33:SCHEMA", "contract c") == {"a": 1}


def test_unconfined_resolve_is_unchanged(confined_app, monkeypatch):
    monkeypatch.delenv(CONFINE_ENV)
    import os
    assert resolve("py:app33.environ") is os.environ and resolve("py:app33.leak") is os


# --- M3 ------------------------------------------------------------------------------------------------
def test_an_unhashable_provider_argument_never_reads_another_one_s_value():
    with inv.isolated() as reg:
        inv.load(m.PROVIDER_DOC)
        rule: Any = type("R", (), {"names": None})()
        names = {"src": "args.xs", "t": "provider.total"}
        cache: dict[Any, Any] = {}
        seen = []
        for n in range(1, 40):                       # each list is freed before the next is made
            seen.append(fetch_providers(reg, rule, names, {"args": {"xs": [n]}}, cache)["total"])
            gc.collect()
        assert seen == list(range(1, 40))
        same = [7]
        assert fetch_providers(reg, rule, names, {"args": {"xs": same}}, cache)["total"] == 7
        same.append(1)                               # the very same object: shared, as before
        assert fetch_providers(reg, rule, names, {"args": {"xs": same}}, cache)["total"] == 7


# --- M4 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_nested_model_call_keeps_the_outer_conversation(asynchronous):
    fn = m.aanswer if asynchronous else m.answer
    with inv.isolated():
        inv.load(m.nested_doc(asynchronous))
        m.LLM.requests.clear()
        # outer attempt 1: inner ok, outer not JSON; attempt 2 (feedback): inner ok again, outer ok
        m.LLM.script[:] = ['{"text": "s1"}', "oops", '{"text": "s2"}', '{"text": "fine"}']
        c = _run(fn("q"))
        assert c.verdict == "ok" and c.value == m.Answer("fine")
        last = m.LLM.requests[-1]
        assert [t["content"] if isinstance(t["content"], str) else "?" for t in last][:2] == ["answer: q", "oops"]
        assert last[0]["content"] == "answer: q" and len(last) == 3      # the outer conversation, continued
        assert m.LLM.requests[2] == [{"role": "user", "content": "summarize: q"}]   # the inner: a new one


def test_outside_a_model_call_the_conversation_is_context_local_as_before():
    llm = m.Scripted()
    llm.script[:] = ["a", "b"]
    assert llm.complete("p") == "a" and llm.complete("p", "again") == "b"
    assert len(llm.requests[-1]) == 3


# --- L1 ------------------------------------------------------------------------------------------------
def test_exceptions_carry_the_bound_names_not_the_values():
    doc = m29.guard_doc("sync", "pre_effect")
    r = doc["rules"][0]
    r["bad"] = "args.n > 5"
    r["enforcers"][0].pop("view")
    doc.pop("providers")
    with inv.isolated() as reg:
        inv.load(doc)
        with pytest.raises(inv.Rejected) as ei:
            m29.g_sync(987654321)
        e = ei.value
        assert e.bound == ("args",) and e.outcome is not None and e.outcome.bound is None
        assert e.outcome.kind == "violation"
        assert "987654321" not in repr(vars(e)) and "987654321" not in repr(vars(e.outcome))
        assert reg.sink[-1]["outcome"] == "violation"
