"""Fix round 18: --inv-ablate counts a falsifier as caught only when its rule decided something on the
control run (a violation; at a model call a violation, an acted-on parse_error or an abstain).

A falsifier that reaches the rule but never makes it reject passes whether or not the rule holds,
so a failure under ablation (a counter, a refilling quota, `assert not inv.is_ablated(...)`) is
not the rule's doing: inconclusive. Asking `inv.is_ablated()` is not reaching the rule.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

import invariants as inv
from invariants import registry
from invariants.pytest_plugin import NEVER_FIRED

pytest_plugins = ["pytester"]

APP = '''
import dataclasses, json
from typing import Literal
import invariants as inv

BROKEN = False

@inv.guard("R-1")
def halve(n: int) -> int:
    return -1 if BROKEN else n // 2

@dataclasses.dataclass(frozen=True)
class Reply:
    cause: Literal["A", "CANNOT_COMPLY"]
    n: int

class CR(inv.Checked): ...

REPLIES = []

@inv.model_call("M-1", schema=Reply)
def ask(inv_feedback: str | None = None) -> CR:
    return REPLIES.pop(0)
'''


def _doc(falsifier: str = "test_app.py::test_f", *, model_mode: str = "enforce") -> dict[str, Any]:
    return {"spec": "invariant/v0.1",
            "targets": {"py:plug_app.halve": {"effects": "none"}, "py:plug_app.ask": {"effects": "none"}},
            "rules": [
                {"id": "R-1", "status": "enforce", "subject": {"target": "py:plug_app.halve"},
                 "bad": "result < 0", "falsified_by": [falsifier],
                 "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce"}]},
                {"id": "M-1", "status": model_mode, "subject": {"target": "py:plug_app.ask"},
                 "bad": "reply.n > 5", "falsified_by": ["test_app.py::test_m"],
                 "enforcers": [{"point": "model_call", "timing": "pre_effect", "mode": model_mode,
                                "on_violation": "reject"}],
                 "model_policy": {"schema": "Reply", "format_retries": 0, "semantic_resamples": 0,
                                  "feedback": {"format": "none"},
                                  "abstain": {"field": "cause", "value": "CANNOT_COMPLY"}}}]}


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(plug_app=APP)
    (pytester.path / "rules.json").write_text(json.dumps(_doc()))
    return pytester


def _ablate(p: pytest.Pytester, tests: str, doc: dict[str, Any] | None = None) -> dict[str, str]:
    if doc is not None:
        (p.path / "rules.json").write_text(json.dumps(doc))
    p.makepyfile(test_app=tests)
    p.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                           "--inv-ablate", "--inv-report-json", "rep.json")
    rules = json.loads((p.path / "rep.json").read_text())["ablation"]["rules"]
    return {f"{rid} {row['falsifier'].split('::')[-1]}": row["result"] for rid, rows in rules.items() for row in rows}


M_OK = '''
def test_m():
    plug_app.REPLIES[:] = ['{"cause": "A", "n": 1}']
    assert plug_app.ask().value.n == 1
'''


# --- the reviewer's three cases ------------------------------------------------------------------
def test_asserting_not_is_ablated_is_inconclusive(project):
    res = _ablate(project, "import invariants as inv, plug_app\n" + M_OK + '''
def test_f():
    plug_app.halve(4)                      # reaches the guard, never a violation
    assert not inv.is_ablated("R-1")
''')
    assert res["R-1 test_f"] == NEVER_FIRED


def test_is_ablated_alone_does_not_reach_the_rule(project):
    res = _ablate(project, "import invariants as inv, plug_app\n" + M_OK + '''
def test_f():
    assert not inv.is_ablated("R-1")
''')
    assert res["R-1 test_f"] == "inconclusive (rule not reached)"


def test_a_counter_failing_on_the_third_run_is_inconclusive(project):
    res = _ablate(project, "import plug_app\nRUNS = [0]\n" + M_OK + '''
def test_f():
    RUNS[0] += 1
    plug_app.halve(4)
    assert RUNS[0] != 3                    # the 3rd run is the ablated one
''')
    assert res["R-1 test_f"] == NEVER_FIRED


def test_a_refilling_token_bucket_is_inconclusive(project):
    res = _ablate(project, "import pytest, plug_app\n" + M_OK + '''
@pytest.fixture(scope="module")
def bucket():
    return {"left": 2}

def test_f(bucket):
    plug_app.halve(4)
    if bucket["left"] == 0:
        bucket["left"] = 2
        pytest.fail("rate limited")
    bucket["left"] -= 1
''')
    assert res["R-1 test_f"] == NEVER_FIRED


# --- real load-bearing falsifiers stay caught ------------------------------------------------------
def test_a_guard_rejection_is_caught(project):
    res = _ablate(project, "import pytest, invariants as inv, plug_app\n" + M_OK + '''
def test_f(monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)
''')
    assert res["R-1 test_f"] == "caught"


MODEL_TESTS = '''
import pytest, invariants as inv, plug_app

def test_f(monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)

def test_m():
    plug_app.REPLIES[:] = [%r]
    with pytest.raises(%s):
        plug_app.ask()
'''


@pytest.mark.parametrize("reply, raised", [('{"cause": "A", "n": 9}', "inv.Rejected"),          # violation
                                           ("not json", "inv.Rejected"),                       # parse_error
                                           ('{"cause": "CANNOT_COMPLY", "n": 1}', "inv.Escalation")])
def test_model_call_decisions_are_caught(project, reply, raised):
    res = _ablate(project, MODEL_TESTS % (reply, raised))
    assert res["M-1 test_m"] == "caught"


def test_a_detect_mode_parse_error_is_not_a_decision(project):
    """In detect mode a parse_error proceeds: the enforcer did not act on it."""
    res = _ablate(project, '''
import invariants as inv, plug_app

def test_f():
    pass

def test_m():
    plug_app.REPLIES[:] = ["not json"]
    got = plug_app.ask()
    assert not inv.is_ablated("M-1") and got.verdict == "parse_error"
''', _doc(model_mode="detect"))
    assert res["M-1 test_m"] == NEVER_FIRED


# --- the trace files ---------------------------------------------------------------------------------
def test_is_ablated_writes_no_trace_and_skips_does(tmp_path, monkeypatch, iso):
    trace = tmp_path / "reached"
    monkeypatch.setenv(registry.ABLATE_TRACE_ENV, str(trace))
    reg = inv.current()
    with registry.ablated_everywhere("Z-1"):
        assert reg.is_ablated("Z-1") and inv.is_ablated("Z-1")
        assert not trace.exists()
        assert reg.skips("Z-1")
    assert trace.read_text().split() == ["Z-1", "enforcer"]


def test_a_violation_is_noted_in_the_decision_trace(tmp_path, monkeypatch, iso):
    trace = tmp_path / "decided"
    monkeypatch.setenv(registry.DECISION_TRACE_ENV, str(trace))
    reg = inv.current()
    reg.record("Z-1", "ok", {"action": "proceed"})
    reg.record("Z-2", "parse_error", {"action": "proceed"})
    reg.record("Z-3", "rule_error", {"action": "reject"})
    assert not trace.exists()
    reg.record("Z-4", "violation", {"action": "proceed"})
    reg.record("Z-5", "parse_error", {"action": "retry"})
    assert trace.read_text().split() == ["Z-4", "Z-5"]
    assert os.environ[registry.DECISION_TRACE_ENV] == str(trace)
