"""--inv-ablate inside the normal test protocol: one fixture lifetime, pytest-xdist, scan falsifiers.

The control and ablated re-runs of a falsifier happen right after its own run, torn down only to the
item, so session, package, module and class fixtures are set up once, exactly as without the plugin.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

pytest_plugins = ["pytester"]

APP = '''
import invariants as inv

BROKEN = False

@inv.guard("R-1")
def halve(n: int) -> int:
    return -1 if BROKEN else n // 2

@inv.guard("R-2")
def twice(n: int) -> int:
    return -1 if BROKEN else n * 2
'''

CONFTEST = '''
import pathlib
import pytest

LOG = pathlib.Path(__file__).with_name("lifetimes.log")

def note(what):
    with LOG.open("a") as f:
        f.write(what + "\\n")

@pytest.fixture(scope="session")
def db():
    note("session-setup")
    yield {"open": True}
    note("session-teardown")

@pytest.fixture(scope="module")
def conn(db):
    note("module-setup")
    yield db
    note("module-teardown")

@pytest.fixture
def per_test(conn):
    note("function-setup")
    yield conn
    note("function-teardown")
'''

TESTS_A = '''
import pytest
import invariants as inv
import plug_app

def test_before(per_test):
    assert per_test["open"]

def test_r1_falsifier(per_test, monkeypatch):
    assert per_test["open"]
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)
    with pytest.raises(inv.Rejected):
        plug_app.twice(4)

def test_after(per_test):
    assert per_test["open"]
'''

TESTS_B = '''
import pytest
import invariants as inv
import plug_app

def test_r2_falsifier(per_test, monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.twice(8)

def test_last(per_test):
    assert per_test["open"]
'''


def _rule(rid: str, falsified_by: list[str]) -> dict[str, Any]:
    target = {"R-1": "py:plug_app.halve", "R-2": "py:plug_app.twice"}[rid]
    return {"id": rid, "status": "enforce", "subject": {"target": target}, "bad": "result < 0",
            "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce"}],
            "falsified_by": falsified_by}


def _doc(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"spec": "invariant/v0.1", "rules": list(rules),
            "targets": {"py:plug_app.halve": {"effects": "none"}, "py:plug_app.twice": {"effects": "none"}}}


@pytest.fixture
def lifetimes(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(plug_app=APP, test_a=TESTS_A, test_b=TESTS_B)
    doc = _doc(_rule("R-1", ["test_a.py::test_r1_falsifier"]),
               _rule("R-2", ["test_b.py::test_r2_falsifier", "test_a.py::test_r1_falsifier"]))
    (pytester.path / "rules.json").write_text(json.dumps(doc))
    return pytester


def _log(p: pytest.Pytester) -> list[str]:
    return (p.path / "lifetimes.log").read_text().split()


def _run(p: pytest.Pytester, *args: str) -> tuple[pytest.RunResult, dict[str, Any]]:
    r = p.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:randomly", "--inv-rules", "rules.json",
                               "--inv-ablate", "--inv-report-json", "rep.json", *args)
    rep = p.path / "rep.json"
    return r, (json.loads(rep.read_text()) if rep.exists() else {})


def test_session_and_module_fixtures_are_set_up_once(lifetimes):
    r, rep = _run(lifetimes)
    assert r.ret == 0, r.stdout.str()
    r.assert_outcomes(passed=5)
    log = _log(lifetimes)
    assert log.count("session-setup") == 1 and log.count("session-teardown") == 1, log
    assert log.count("module-setup") == 2 and log.count("module-teardown") == 2, log     # one per module
    assert log[0] == "session-setup" and log[-1] == "session-teardown"
    # test_r1_falsifier: its own run, the control re-run, and per rule (R-1, R-2) one ablated re-run
    # and a second control after it (round 17); test_r2_falsifier: its run, the control, R-2's
    # ablated run and the second control; 3 other tests run once
    assert log.count("function-setup") == 6 + 4 + 3 == log.count("function-teardown")
    assert rep["ablation"]["rules"] == {
        "R-1": [{"falsifier": "test_a.py::test_r1_falsifier", "result": "caught"}],
        "R-2": [{"falsifier": "test_b.py::test_r2_falsifier", "result": "caught"},
                {"falsifier": "test_a.py::test_r1_falsifier", "result": "caught"}]}


def test_module_fixture_teardown_waits_for_the_next_module(lifetimes):
    """The falsifier's last re-run does not tear the module down early: test_after still shares it."""
    _run(lifetimes)
    log = _log(lifetimes)
    first_teardown = log.index("module-teardown")
    a_tests = 1 + 6 + 1                       # test_before, the falsifier's 6 runs, test_after
    assert log[:first_teardown].count("function-setup") == a_tests, log


def test_falsifier_reruns_are_not_logged_as_tests(lifetimes):
    r, _ = _run(lifetimes, "-v")
    out = r.stdout.str()
    assert out.count("test_a.py::test_r1_falsifier PASSED") == 1, out


def test_fixture_teardown_error_after_reruns_is_reported(lifetimes):
    lifetimes.makeconftest(CONFTEST.replace('    note("module-teardown")',
                                            '    note("module-teardown")\n    raise RuntimeError("teardown broke")'))
    r, _ = _run(lifetimes, "test_a.py")
    r.assert_outcomes(passed=3, errors=1)
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_after*"])


def test_teardown_error_of_the_last_falsifier_is_reported(lifetimes):
    """When the falsifier is the module's last test, the module teardown error is still reported."""
    lifetimes.makeconftest(CONFTEST.replace('    note("module-teardown")',
                                            '    note("module-teardown")\n    raise RuntimeError("teardown broke")'))
    lifetimes.makepyfile(test_a=TESTS_A.replace("def test_after(per_test):\n    assert per_test[\"open\"]\n", ""))
    r, _ = _run(lifetimes, "test_a.py")
    r.assert_outcomes(passed=2, errors=1)
    r.stdout.fnmatch_lines(["*ERROR at teardown of test_r1_falsifier*", "*teardown broke*"])


def test_failing_falsifier_is_not_rerun(lifetimes):
    lifetimes.makepyfile(test_a=TESTS_A.replace("assert per_test[\"open\"]\n    monkeypatch",
                                                "assert not per_test[\"open\"]\n    monkeypatch"))
    r, rep = _run(lifetimes, "test_a.py")
    r.assert_outcomes(passed=2, failed=1)
    assert rep["ablation"]["rules"]["R-1"] == [{"falsifier": "test_a.py::test_r1_falsifier",
                                                "result": "not checked (failed with the guard on)"}]
    log = _log(lifetimes)
    assert log.count("function-setup") == 3 and log.count("session-setup") == 1, log


def test_falsifier_not_run_is_reported(lifetimes):
    """-x stops the session before a falsifier runs: it is reported as not run, not as caught."""
    lifetimes.makepyfile(test_0="def test_first():\n    assert False\n")
    r, rep = _run(lifetimes, "-x")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    assert rep["ablation"]["rules"]["R-1"] == [{"falsifier": "test_a.py::test_r1_falsifier",
                                                "result": "not run"}]
    assert rep["ablation"]["unverified"] == ["R-1", "R-2"]


def test_class_scoped_fixture_is_shared(lifetimes):
    lifetimes.makepyfile(test_a='''
import pytest
import invariants as inv
import plug_app
from conftest import note

class TestGroup:
    @pytest.fixture(scope="class")
    def group(self):
        note("class-setup")
        yield
        note("class-teardown")

    def test_r1_falsifier(self, group, monkeypatch):
        monkeypatch.setattr(plug_app, "BROKEN", True)
        with pytest.raises(inv.Rejected):
            plug_app.halve(4)

    def test_other(self, group):
        pass
''')
    doc = _doc(_rule("R-1", ["test_a.py::TestGroup::test_r1_falsifier"]))
    (lifetimes.path / "rules.json").write_text(json.dumps(doc))
    r, rep = _run(lifetimes, "test_a.py")
    assert r.ret == 0, r.stdout.str()
    assert _log(lifetimes).count("class-setup") == 1
    assert rep["ablation"]["rules"]["R-1"][0]["result"] == "caught"


# --- pytest-xdist ----------------------------------------------------------------------------------
xdist = pytest.mark.skipif(not __import__("importlib.util").util.find_spec("xdist"),
                           reason="pytest-xdist is not installed")


@xdist
def test_xdist_each_worker_ablates_its_falsifiers(lifetimes):
    r, rep = _run(lifetimes, "-n", "2")
    assert r.ret == 0, r.stdout.str()
    r.assert_outcomes(passed=5)
    r.stdout.fnmatch_lines(["*invariants ablation*", "*R-1: load-bearing*", "*R-2: load-bearing*"])
    assert rep["ablation"]["rules"] == {
        "R-1": [{"falsifier": "test_a.py::test_r1_falsifier", "result": "caught"}],
        "R-2": [{"falsifier": "test_b.py::test_r2_falsifier", "result": "caught"},
                {"falsifier": "test_a.py::test_r1_falsifier", "result": "caught"}]}
    assert rep["ablation"]["unverified"] == [] and rep["ablation"]["not_load_bearing"] == []
    assert rep["rules"]["R-2"]["reach"] == 2 and rep["rules"]["R-2"]["violation"] == 2   # summed over workers
    log = _log(lifetimes)
    assert log.count("session-setup") <= 2         # at most once per worker


@xdist
def test_xdist_not_load_bearing_fails_the_controller(lifetimes):
    weak = TESTS_B.replace('    monkeypatch.setattr(plug_app, "BROKEN", True)\n    with pytest.raises(inv.Rejected):\n'
                           '        plug_app.twice(8)', "    assert plug_app.twice(8) == 16")      # never needs the guard
    assert weak != TESTS_B
    lifetimes.makepyfile(test_b=weak)
    r, rep = _run(lifetimes, "-n", "2")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*invariants ablation FAILED: guard not load-bearing for R-2*"])
    assert rep["ablation"]["not_load_bearing"] == ["R-2"]


@xdist
def test_xdist_uncollected_falsifier_and_verify(lifetimes):
    doc = _doc(_rule("R-1", ["test_a.py::test_missing"]), _rule("R-2", ["test_b.py::test_r2_falsifier"]))
    (lifetimes.path / "rules.json").write_text(json.dumps(doc))
    r, rep = _run(lifetimes, "-n", "2", "--inv-verify")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    assert rep["ablation"]["rules"]["R-1"] == [{"falsifier": "test_a.py::test_missing", "result": "not collected"}]
    assert rep["ablation"]["unverified"] == ["R-1"]
    assert rep["verify"] == {"ok": True, "problems": []}
    r.stdout.fnmatch_lines(["*invariants verify: 2 targeted rule(s) guarded*"])


@xdist
def test_xdist_verify_failure_reaches_the_controller(lifetimes):
    lifetimes.makepyfile(plug_app=APP.replace('@inv.guard("R-2")\n', ''))
    r, _ = _run(lifetimes, "-n", "2", "--inv-verify")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*invariants verify FAILED*"])
