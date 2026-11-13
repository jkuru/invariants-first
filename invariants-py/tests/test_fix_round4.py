"""Regression tests for fix round 4: each test failed before its fix."""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

import invariants as inv
import round4 as r4

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def iso():
    with inv.isolated() as r:
        yield r


def load(*rules: dict[str, Any]) -> inv.Registry:
    return inv.load(r4.doc(*rules))


# --- major: unknown field on a closed type is refused at load ----------------------------------------
@pytest.mark.parametrize("bad", [
    "rec.sizee > 100",              # typo on a dataclass field
    "args.n.foo > 1",               # a field on an int
    "rec.size.x > 1",               # a field on a field that is an int
    'rec.name.first == "a"',        # a field on a string
    "maybe_rec.sizee > 1",          # T | None keeps T's fields
])
def test_unknown_field_on_closed_type_is_a_load_error(iso, bad):
    with pytest.raises(inv.LoadError, match="ill-typed"):
        load(r4.rule("G-1", "door", bad))


@pytest.mark.parametrize("bad", [
    "rec.size > 100",
    "rec.double > 100",             # a property is a public attribute of the class
    "maybe_rec != null && maybe_rec.size > 1",
    "bag.anything > 1",             # a plain dict stays open
    "args.anything.whatever > 1",   # Any is dyn
])
def test_known_or_open_fields_still_load(iso, bad):
    load(r4.rule("G-1", "door", bad))


def test_known_field_rule_still_enforces(iso):
    load(r4.rule("G-1", "door", "rec.size > 100"))
    assert r4.door(5) == 5
    with pytest.raises(inv.Rejected):
        r4.door(500)


# --- major: a sync enforcer that reads an async provider is refused at load ---------------------------
@pytest.mark.parametrize("on_error", ["fail_closed", "fail_open_and_page"])
def test_sync_guard_reading_async_provider_is_a_load_error(iso, on_error):
    with pytest.raises(inv.LoadError, match="async def.*sync"):
        load(r4.rule("G-1", "door", "aprov > 10", on_error=on_error))


def test_async_guard_reading_async_provider_loads_and_enforces(iso):
    load(r4.rule("G-2", "adoor", "aprov > 10"))
    assert asyncio.run(r4.adoor(5)) == 5
    with pytest.raises(inv.Rejected):
        asyncio.run(r4.adoor(50))


# --- minor: equality across type families -------------------------------------------------------------
@pytest.mark.parametrize("bad", ['args.n == "a"', 'rec.size != "a"', "rec.name == 1", "args.n == true",
                                 "rec == [1]"])
def test_equality_across_type_families_is_a_load_error(iso, bad):
    with pytest.raises(inv.LoadError, match="no overload for (==|!=)"):
        load(r4.rule("G-1", "door", bad))


@pytest.mark.parametrize("bad", ["args.n == 1.5", "maybe_rec == null", "args.anything == 1",
                                 'rec.name == "r"', "bag.size == 1"])
def test_equality_within_a_family_or_with_null_or_dyn_loads(iso, bad):
    load(r4.rule("G-1", "door", bad))


# --- minor: ablation outside pytest is loud even with no logging configured ---------------------------
def _ablated_child(extra_env: dict[str, str], code: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "INV_"))}
    env.update({"INV_ABLATE": "G-1", "INV_ABLATE_TESTING": "1",
                "PYTHONPATH": os.pathsep.join([str(FIXTURES), str(pathlib.Path(inv.__file__).parent.parent)]),
                **extra_env})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)


CHILD = ("import invariants as inv, round4 as r4\n"
         "inv.load(r4.doc(r4.rule('G-1', 'door', 'args.n < 0')))\n"
         "print('passed', r4.door(-1))\n")


def test_ablation_outside_pytest_without_logging_writes_stderr():
    r = _ablated_child({}, CHILD)
    assert r.returncode == 0, r.stderr
    assert "passed -1" in r.stdout
    assert "OFF in this process" in r.stderr and "G-1" in r.stderr


def test_pytest_current_test_alone_does_not_quiet_ablation():
    r = _ablated_child({"PYTEST_CURRENT_TEST": "x"}, CHILD)
    assert "OFF in this process" in r.stderr


def test_ablation_with_logging_configured_logs_once(monkeypatch):
    r = _ablated_child({}, "import logging; logging.basicConfig(level=logging.WARNING)\n" + CHILD)
    assert r.stderr.count("OFF in this process") == 1
    assert "ERROR:invariants" in r.stderr


def test_under_pytest_ignores_pytest_current_test(monkeypatch):
    from invariants import registry
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setattr(registry._SESSION, "pytest_sessions", 0)     # no configured session (round 5: not sys.modules)
    assert registry.under_pytest() is False
    assert registry.ablation_level() == logging.ERROR


# --- major (cert round 1): --inv-ablate counts 'caught' only when the ablation caused the failure ------
pytest_plugins = ["pytester"]

_APP = '''
import invariants as inv

@inv.guard("R-1")
def halve(n: int) -> int:
    return n // 2
'''

_TESTS = '''
import os, subprocess, sys
import pytest
import invariants as inv
import plug_app

RUNS = {"n": 0}

def test_stateful():                 # never reaches the guard; fails on any second run
    RUNS["n"] += 1
    assert RUNS["n"] == 1

def test_marker(tmp_path_factory):   # never reaches the guard; fails on every other run
    marker = os.path.join(os.path.dirname(__file__), "marker")
    if os.path.exists(marker):
        os.remove(marker)
        raise RuntimeError("leftover")
    open(marker, "w").close()

def test_unreached_fails_only_ablated():   # fails under ablation for a reason unrelated to R-1
    assert not os.environ.get("INV_ABLATE")

def test_real():                     # needs the guard
    with pytest.raises(inv.Rejected):
        plug_app.halve(-4)

def test_subprocess():               # reaches the rule in a child process only
    code = "import invariants as inv, plug_app; inv.load('rules.json'); plug_app.halve(-4)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode != 0, "child was not rejected"
'''


def _rules(falsifiers: list[str]) -> dict[str, Any]:
    return {"spec": "invariant/v0.1", "targets": {"py:plug_app.halve": {"effects": "none"}},
            "rules": [{"id": "R-1", "status": "enforce", "subject": {"target": "py:plug_app.halve"},
                       "bad": "args.n < 0",
                       "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}],
                       "falsified_by": falsifiers}]}


def _ablate(pytester: pytest.Pytester, falsifiers: list[str]) -> tuple[pytest.RunResult, dict[str, Any]]:
    import json
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(plug_app=_APP, test_app=_TESTS)
    (pytester.path / "rules.json").write_text(json.dumps(_rules(falsifiers)))
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--inv-rules", "rules.json", "--inv-ablate",
                                      "--inv-report-json", "rep.json")
    return r, json.loads((pytester.path / "rep.json").read_text())["ablation"]


@pytest.mark.parametrize("name", ["test_stateful", "test_marker"])
def test_ablation_failure_that_also_fails_without_ablation_is_inconclusive(pytester, name):
    r, rep = _ablate(pytester, [f"test_app.py::{name}"])
    row = rep["rules"]["R-1"][0]
    assert row["result"].startswith("inconclusive") and "without ablation" in row["result"], row
    assert rep["unverified"] == ["R-1"]
    assert r.ret != 0 and "load-bearing" not in r.stdout.str().replace("not load-bearing", "")


def test_ablation_failure_where_rule_is_not_reached_is_inconclusive(pytester):
    r, rep = _ablate(pytester, ["test_app.py::test_unreached_fails_only_ablated"])
    assert rep["rules"]["R-1"][0]["result"] == "inconclusive (rule not reached)"
    assert r.ret != 0


@pytest.mark.parametrize("name", ["test_real", "test_subprocess"])
def test_ablation_that_reaches_the_rule_is_caught(pytester, name):
    r, rep = _ablate(pytester, [f"test_app.py::{name}"])
    assert rep["rules"]["R-1"][0]["result"] == "caught", rep
    assert r.ret == 0, r.stdout.str()


# --- major (cert round 1): an enforcing edge refuses a non-ok verdict whatever its on_error ----------
def test_edge_fail_open_refuses_a_rule_error_verdict(iso):
    import support
    import triage

    def fail_open(d: dict[str, Any]) -> None:
        for e in support.rule(d, "R-7")["enforcers"]:
            e["on_error"] = "fail_open_and_page"
    inv.load(support.variant(fail_open), base_dir=support.FIXTURES)
    real = triage.JOBS.copy()
    try:
        triage.JOBS.pop("J-slow")                    # the provider fails during the model call only
        triage.client.script('{"cause":"OUT_OF_MEMORY","message":"m"}')
        c = triage.propose_cause("x", "J-slow")
        assert c.verdict == "rule_error"             # model_call failed open (paged) with that verdict
    finally:
        triage.JOBS.update(real)
    with pytest.raises(inv.Rejected, match="verdict rule_error"):
        triage.remediate(c, "J-slow")
    last = list(iso.sink)[-1]
    assert (last["point"], last["outcome"], last["action"]) == ("edge", "rule_error", "reject")


def test_edge_fail_open_still_fails_open_on_its_own_rule_error(iso):
    """on_error still covers the edge's own re-evaluation: an ok verdict whose re-check errors."""
    import support
    import triage

    def fail_open(d: dict[str, Any]) -> None:
        for e in support.rule(d, "R-7")["enforcers"]:
            e["on_error"] = "fail_open_and_page"
    inv.load(support.variant(fail_open), base_dir=support.FIXTURES)
    triage.client.script('{"cause":"OUT_OF_MEMORY","message":"m"}')
    c = triage.propose_cause("x", "J-oom") if "J-oom" in triage.JOBS else None
    if c is None or c.verdict != "ok":
        pytest.skip("fixture has no job with an ok OUT_OF_MEMORY verdict")
    real = triage.JOBS.copy()
    try:
        triage.JOBS.clear()                          # the edge's own provider read now fails
        triage.remediate(c, "J-oom")
    finally:
        triage.JOBS.update(real)
    last = list(iso.sink)[-1]
    assert (last["point"], last["outcome"], last["action"]) == ("edge", "rule_error", "page")
