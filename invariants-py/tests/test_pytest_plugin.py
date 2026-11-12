"""The pytest plugin, tested with pytester in subprocesses (so the outer default registry is untouched)."""
from __future__ import annotations

import json
from typing import Any

import pytest

import invariants as inv

pytest_plugins = ["pytester"]

APP = '''
import invariants as inv

BROKEN = False

@inv.guard("R-1")
def halve(n: int) -> int:
    """Never negative (R-1)."""
    return -1 if BROKEN else n // 2

def unguarded(n: int) -> int:
    return n
'''


def rules(*extra: dict[str, Any], falsified_by: list[str] | None = None) -> dict[str, Any]:
    r1 = {"id": "R-1", "rev": 1, "status": "enforce", "class": "law", "owner": "t",
          "statement": "halve never returns a negative number.",
          "subject": {"target": "py:plug_app.halve"}, "scope": "call",
          "bad": [{"when": "result < 0", "message": "negative"}],
          "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce",
                         "on_violation": "reject", "on_error": "fail_closed"}],
          "falsified_by": falsified_by if falsified_by is not None else ["test_app.py::test_r1_falsifier"]}
    targets = {"py:plug_app.halve": {"effects": "none"}}
    for e in extra:
        targets[e["subject"]["target"]] = {"effects": "none"}
    return {"spec": "invariant/v0.1", "targets": targets, "rules": [r1, *extra]}


R2_UNGUARDED = {"id": "R-2", "rev": 1, "status": "enforce", "class": "law", "owner": "t",
                "statement": "unguarded never returns a negative number.",
                "subject": {"target": "py:plug_app.unguarded"}, "scope": "call",
                "bad": [{"when": "result < 0", "message": "negative"}],
                "enforcers": [{"point": "decorator", "timing": "post_effect", "mode": "enforce",
                               "on_violation": "reject", "on_error": "fail_closed"}],
                "falsified_by": ["test_app.py::test_ok"]}

FALSIFIER = '''
import pytest
import invariants as inv
import plug_app

def test_r1_falsifier(monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)

def test_ok():
    assert plug_app.halve(4) == 2
'''

WEAK_FALSIFIER = '''
import plug_app

def test_r1_falsifier():
    assert plug_app.halve(4) == 2      # never exercises the guard

def test_ok():
    assert plug_app.halve(6) == 3
'''


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    """A tiny project: an app module, its rule file and a test module (falsifier that needs the guard)."""
    pytester.makeini("[pytest]\npythonpath = .\n")
    pytester.makepyfile(plug_app=APP, test_app=FALSIFIER)
    write_rules(pytester, rules())
    return pytester


def write_rules(pytester: pytest.Pytester, doc: dict[str, Any], name: str = "rules.json") -> None:
    (pytester.path / name).write_text(json.dumps(doc))


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)


# --- inert by default ----------------------------------------------------------------------------
PLAIN = "def test_plain():\n    assert 1 + 1 == 2\n"


def test_no_options_is_inert(pytester):
    pytester.makepyfile(test_plain=PLAIN)
    r = run(pytester)
    r.assert_outcomes(passed=1)
    assert r.ret == 0
    out = r.stdout.str()
    assert not any(s in out for s in ("invariants verify", "invariants ablation", "invariants report"))


def test_plugin_is_registered(pytester):
    pytester.makepyfile(test_x="def test_x(pytestconfig):\n"
                               "    assert pytestconfig.pluginmanager.has_plugin('invariants-session')\n")
    run(pytester).assert_outcomes(passed=1)


def test_help_lists_options(pytester):
    out = run(pytester, "--help").stdout.str()
    for opt in ("--inv-rules", "--inv-verify", "--inv-ablate", "--inv-report", "--inv-report-json"):
        assert opt in out


# --- --inv-rules ---------------------------------------------------------------------------------
def test_inv_rules_loads_into_default_registry(project):
    project.makepyfile(test_loaded='''
import invariants as inv
def test_loaded():
    assert set(inv.default_registry().rules) == {"R-1"}
''')
    run(project, "--inv-rules", "rules.json").assert_outcomes(passed=3)


def test_inv_rules_from_ini(project):
    project.makeini("[pytest]\npythonpath = .\ninv_rules = rules.json\n")
    project.makepyfile(test_loaded='''
import invariants as inv
def test_loaded():
    assert "R-1" in inv.default_registry().rules
''')
    run(project).assert_outcomes(passed=3)


def test_inv_rules_missing_file_is_usage_error(project):
    r = run(project, "--inv-rules", "nope.json")
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    assert "does not exist" in r.stderr.str()


def test_inv_rules_load_error_is_usage_error(project):
    doc = rules()
    doc["rules"][0]["status"] = "bogus"
    write_rules(project, doc)
    r = run(project, "--inv-rules", "rules.json")
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    assert "--inv-rules" in r.stderr.str()


def test_yaml_rule_file(project):
    yaml = pytest.importorskip("yaml")
    (project.path / "rules.yaml").write_text(yaml.safe_dump(rules()))
    run(project, "--inv-rules", "rules.yaml", "--inv-verify").assert_outcomes(passed=2)


# --- --inv-verify --------------------------------------------------------------------------------
def test_verify_passes(project):
    r = run(project, "--inv-rules", "rules.json", "--inv-verify")
    r.assert_outcomes(passed=2)
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*invariants verify: 1 targeted rule(s) guarded*"])


def test_verify_fails_when_target_lacks_guard(project):
    write_rules(project, rules(R2_UNGUARDED))
    r = run(project, "--inv-rules", "rules.json", "--inv-verify")
    r.assert_outcomes(passed=2)
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*invariants verify FAILED*", "*R-2: py:plug_app.unguarded is not wrapped by its guard*"])


def test_verify_without_rules_fails(pytester):
    pytester.makepyfile(test_plain=PLAIN)
    r = run(pytester, "--inv-verify")
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*invariants verify FAILED*", "*no rules loaded*"])


def test_verify_does_not_mask_test_failures(project):
    project.makepyfile(test_bad="def test_bad():\n    assert False\n")
    r = run(project, "--inv-rules", "rules.json", "--inv-verify")
    r.assert_outcomes(passed=2, failed=1)
    assert r.ret == pytest.ExitCode.TESTS_FAILED


# --- --inv-ablate --------------------------------------------------------------------------------
def test_ablate_load_bearing_guard_passes(project):
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    r.assert_outcomes(passed=2)               # re-runs are not reported as extra tests
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*invariants ablation*", "*R-1: load-bearing*", "*caught*test_app.py::test_r1_falsifier*"])


def test_ablate_reports_guard_not_load_bearing(project):
    project.makepyfile(test_app=WEAK_FALSIFIER)
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    r.assert_outcomes(passed=2)
    assert r.ret == pytest.ExitCode.TESTS_FAILED
    r.stdout.fnmatch_lines(["*invariants ablation FAILED: guard not load-bearing for R-1*",
                            "*R-1: guard not load-bearing*", "*still passes*test_app.py::test_r1_falsifier*"])


def test_ablate_reaches_isolated_registries(project):
    """A falsifier that loads the rules into its own registry still sees the ablation."""
    project.makepyfile(test_app='''
import pytest
import invariants as inv
import plug_app

def test_r1_falsifier(monkeypatch):
    with inv.isolated():
        inv.load("rules.json")
        monkeypatch.setattr(plug_app, "BROKEN", True)
        with pytest.raises(inv.Rejected):
            plug_app.halve(4)
''')
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*R-1: load-bearing*"])


def test_ablate_reaches_module_level_registries(project):
    """A registry made at import time, before the re-run, is ablated too (process-wide ablation)."""
    project.makepyfile(test_app='''
import pytest
import invariants as inv
import plug_app

REG = inv.load("rules.json", install=False)

def test_r1_falsifier(monkeypatch):
    with inv.using(REG):
        monkeypatch.setattr(plug_app, "BROKEN", True)
        with pytest.raises(inv.Rejected):
            plug_app.halve(4)
''')
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*R-1: load-bearing*"])


def test_report_counts_isolated_blocks_in_test_bodies(project):
    project.makepyfile(test_app='''
import invariants as inv
import plug_app

def test_a():
    with inv.isolated():
        inv.load("rules.json")
        plug_app.halve(2)
        plug_app.halve(4)
''')
    run(project, "--inv-rules", "rules.json", "--inv-report").stdout.fnmatch_lines(["*R-1*enforce*2*2*0*0*0*"])


def test_ablate_matches_parametrised_falsifiers(project):
    project.makepyfile(test_app='''
import pytest
import invariants as inv
import plug_app

@pytest.mark.parametrize("n", [2, 4])
def test_r1_falsifier(monkeypatch, n):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(n)
''')
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*caught*test_r1_falsifier[[]2[]]*", "*caught*test_r1_falsifier[[]4[]]*"])


def test_ablate_falsifier_not_collected_fails_the_session(project):
    """A typo in falsified_by must not pass: an enforce rule needs one caught falsifier."""
    write_rules(project, rules(falsified_by=["test_missing.py::test_gone"]))
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == 1
    r.stdout.fnmatch_lines(["*no falsifier caught the ablation of R-1*", "*R-1: not checked*",
                            "*not collected*test_missing.py::test_gone*"])


def test_ablate_lenient_only_notes_a_missing_falsifier(project):
    write_rules(project, rules(falsified_by=["test_missing.py::test_gone"]))
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate", "--inv-ablate-lenient")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*R-1: not checked*"])


def test_ablate_falsifier_failing_with_guard_on_fails_the_session(project):
    project.makepyfile(test_app="def test_r1_falsifier():\n    assert False\n")
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    r.stdout.fnmatch_lines(["*no falsifier caught the ablation of R-1*"])


def test_ablation_reaches_subprocesses_through_inv_ablate(project):
    """A falsifier that runs the check in a subprocess sees the rule ablated through INV_ABLATE."""
    project.makepyfile(test_app="""
import os, subprocess, sys
def test_r1_falsifier():
    # fix round 4: the child must act on the ablation through the library (here inv.is_ablated),
    # so the reach is noted; a child that only reads INV_ABLATE itself is "rule not reached"
    code = "import sys, invariants as inv; inv.load('rules.json'); sys.exit(1 if inv.is_ablated('R-1') else 0)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
""")
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*R-1: load-bearing*"])


def test_inv_events_fixture_captures_every_event(project):
    project.makepyfile(test_app="""
import plug_app
def test_r1_falsifier(inv_events):
    plug_app.halve(4)
    assert [e["rule"] for e in inv_events] == ["R-1"]
""")
    r = run(project, "--inv-rules", "rules.json")
    r.assert_outcomes(passed=1)


def test_ablate_skips_falsifier_failing_with_guard_on(project):
    project.makepyfile(test_app="def test_r1_falsifier():\n    assert False\n")
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate")
    r.assert_outcomes(failed=1)
    r.stdout.fnmatch_lines(["*not checked (failed with the guard on)*"])


def test_ablate_without_rules_notes_it(pytester):
    pytester.makepyfile(test_plain=PLAIN)
    r = run(pytester, "--inv-ablate")
    assert r.ret == 0
    r.stdout.fnmatch_lines(["*invariants ablation*", "*no rules loaded*"])


# --- --inv-report --------------------------------------------------------------------------------
REPORT_TESTS = '''
import pytest
import invariants as inv
import plug_app

def test_two_ok():
    plug_app.halve(2)
    plug_app.halve(4)

def test_violation(monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)

def test_own_registry(inv_registry):
    plug_app.halve(8)
'''


def test_report_prints_counts(project):
    project.makepyfile(test_app=REPORT_TESTS)
    r = run(project, "--inv-rules", "rules.json", "--inv-report")
    r.assert_outcomes(passed=3)
    r.stdout.fnmatch_lines(["*invariants report*", "*rule*status*reach*ok*violation*rule_error*",
                            "*R-1*enforce*4*3*1*0*0*"])


def test_report_json(project):
    project.makepyfile(test_app=REPORT_TESTS)
    r = run(project, "--inv-rules", "rules.json", "--inv-verify", "--inv-report-json", "out/report.json")
    assert r.ret == 0
    data = json.loads((project.path / "out" / "report.json").read_text())
    assert data["rules"]["R-1"] == {"status": "enforce", "reach": 4, "ok": 3, "violation": 1,
                                    "rule_error": 0, "parse_error": 0}
    assert data["verify"] == {"ok": True, "problems": []}
    assert "ablation" not in data


def test_report_counts_registries_made_in_fixtures(project):
    project.makepyfile(test_app='''
import pytest
import invariants as inv
import plug_app

@pytest.fixture
def reg():
    with inv.isolated() as r:
        inv.load("rules.json")
        yield r

def test_a(reg):
    plug_app.halve(2)
''')
    run(project, "--inv-rules", "rules.json", "--inv-report").stdout.fnmatch_lines(["*R-1*enforce*1*1*0*0*0*"])


def test_report_excludes_ablation_reruns(project):
    r = run(project, "--inv-rules", "rules.json", "--inv-ablate", "--inv-report-json", "r.json")
    data = json.loads((project.path / "r.json").read_text())
    assert data["rules"]["R-1"]["reach"] == 2         # test_ok + the falsifier's main run
    assert data["ablation"]["not_load_bearing"] == []
    assert data["ablation"]["rules"]["R-1"][0]["result"] == "caught"
    assert r.ret == 0


def test_report_without_rules(pytester):
    pytester.makepyfile(test_x="def test_x():\n    pass\n")
    run(pytester, "--inv-report").stdout.fnmatch_lines(["*no rules loaded and no rule evaluations recorded*"])


# --- fixtures ------------------------------------------------------------------------------------
def test_inv_registry_fixture_is_isolated(project):
    project.makepyfile(test_fx='''
import invariants as inv

def test_isolated(inv_registry):
    assert inv.current() is inv_registry
    assert inv_registry is not inv.default_registry()
    assert set(inv_registry.rules) == {"R-1"}
    assert inv_registry.sink == []
''')
    run(project, "--inv-rules", "rules.json", "test_fx.py").assert_outcomes(passed=1)


def test_inv_registry_fixture_empty_without_rules(project):
    project.makepyfile(test_fx='''
import invariants as inv

def test_empty(inv_registry):
    assert inv.current() is inv_registry and inv_registry.rules == {}

def test_restored():
    assert inv.current() is inv.default_registry()
''')
    run(project, "test_fx.py", "-p", "no:randomly").assert_outcomes(passed=2)


def test_inv_ablated_fixture(project):
    project.makepyfile(test_fx='''
import pytest
import invariants as inv
import plug_app

def test_ablated(inv_registry, inv_ablated, monkeypatch):
    monkeypatch.setattr(plug_app, "BROKEN", True)
    with inv_ablated("R-1"):
        assert inv_registry.is_ablated("R-1")
        assert plug_app.halve(4) == -1          # guard off
    assert not inv_registry.is_ablated("R-1")
    with pytest.raises(inv.Rejected):
        plug_app.halve(4)

def test_unknown_rule(inv_registry, inv_ablated):
    with pytest.raises(inv.LoadError, match="R-99"):
        inv_ablated("R-99")
''')
    run(project, "--inv-rules", "rules.json", "test_fx.py").assert_outcomes(passed=2)


def test_fixtures_available_in_outer_suite(inv_registry, inv_ablated):
    """The plugin is active in this suite too (pytest11 entry point)."""
    assert inv.current() is inv_registry
    assert inv_registry.rules == {}
    with pytest.raises(inv.LoadError):
        inv_ablated("R-1")
