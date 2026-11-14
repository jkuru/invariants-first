"""Fix round 15 (certification round 12 findings): an edit to `bad` that can raise where the old
condition could not is not "matches more cases" (R12-1); a scanned module's or class's namespace
looked up through `__dict__`, `__getattribute__` or `inspect.getmembers` is a getattr or reflection
(R12-2); the [fastapi] extra brings httpx (R12-3); `inv scan` finds a src layout by itself (R12-5d)."""
from __future__ import annotations

import invariants_schema_diff.cel_sites
import invariants_schema_diff.cel_canon
import pathlib
import textwrap
import tomllib

import pytest

import invariants_scan as scan
from invariants import cli, lock
from invariants_cel import typecheck as tc
from invariants_cel import subset
from cli_support import run_inv, write_project

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _cmp(old: str, new: str, typer=None) -> str:
    return lock.compare_bad(old, new, subset.parse, typer)


# --- R12-1: error-introducing edits --------------------------------------------------------------------------
@pytest.mark.parametrize("old,new", [
    ('args.region in ["xx", "yy"]', 'args.region in ["xx", "yy", args.cfg.extra]'),   # the repro: a new read
    ("size(args.s) / 2 > 1", "size(args.s) / 0 > 1"),                                  # a divisor lowered to 0
    ("size(args.s) % 3 > 1", "size(args.s) % args.k > 1"),                             # a computed modulus
    ("args.n + 1 > 5", "args.n + 2 > 5"),                                              # may overflow int64
    ("args.n + 1 > 5", "args.n + 9223372036854775807 > 5"),
    ("args.n > 5", "args.n > 4 + args.k"),                                             # new arithmetic, new read
    ("args.n > 5", "args.n > args.xs[0]"),                                             # a new index
    ("args.s.exists(x, x.a > 5)", "args.s.exists(x, x.a + x.b > 5)"),                  # inside a comprehension
    ('args.region in ["xx"]', 'args.region in ["xx", string(args.n)]'),                # a new call
])
def test_an_edit_that_can_newly_raise_is_not_stronger(old, new):
    assert _cmp(old, new) == "unknown"


@pytest.mark.parametrize("old,new", [
    ("args.n > 5", "args.n > 4"),                                    # a lower threshold
    ('args.region in ["xx", "yy"]', 'args.region in ["xx", "yy", "zz"]'),   # a literal list member
    ("args.n > 5", "args.n > 5 || args.n < 0"),                      # a disjunct over a field already read
    ("args.n > 5", "args.n > 5 || args.m.k < 0"),                    # a disjunct: || absorbs its error
    ("args.n > 5 && args.m > 1", "args.n > 5"),                      # a conjunct removed
    ("size(args.s) / 2 > 1", "size(args.s) / 2 > 0"),                # the same division, a lower bound
    ("args.n + 1 > 5", "args.n + 1 > 4"),                            # the same arithmetic
    ("args.s.exists(x, x.a > 5)", "args.s.exists(x, x.a > 4)"),
    ("args.a > 10 * size(args.b)", "args.a > 4 * size(args.b)"),     # bounded: cannot overflow
])
def test_legitimate_tightenings_stay_stronger(old, new):
    assert _cmp(old, new) == "stronger"


def test_new_error_sites_are_described():
    ta = lock.canon_tree(subset.parse('args.region in ["xx"]'))
    tb = lock.canon_tree(subset.parse('args.region in ["xx", args.cfg.extra]'))
    assert invariants_schema_diff.cel_sites.new_error_sites(ta, tb) == ["a read of args.cfg.extra"]
    assert invariants_schema_diff.cel_sites.new_error_sites(ta, ta) == []


def test_a_uint_factor_keeps_its_polarity_but_may_overflow():
    typer = lock.make_typer([{"args": tc.T({"map"}, fields={"u": tc.UINT})}])
    assert _cmp("args.u * 2 > 5", "args.u * 2 > 4", typer) == "stronger"
    assert _cmp("args.u * 2 > 5", "args.u * 3 > 5", typer) == "unknown"


MOD = """
from typing import Any
import invariants as inv

@inv.guard("C-1")
def f(region: str = "", cfg: dict[str, Any] = {}, n: int = 0) -> None:
    pass
"""
RULES = """spec: invariant/v0.1
targets:
  "py:mod15.f": {effects: none}
rules:
  - id: C-1
    rev: 1
    status: enforce
    subject: {target: "py:mod15.f"}
    bad: 'BAD'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_error: fail_open_and_page}
    falsified_by: ["t::t"]
"""


@pytest.mark.parametrize("new,ok", [
    ('args.region in ["xx", "yy", args.cfg.extra]', False),
    ('args.region in ["xx", "yy", "zz"]', True),
    ('args.region in ["xx", "yy"] || args.cfg.extra == "q"', True),
])
def test_lock_refuses_an_error_introducing_edit_on_a_fail_open_rule(tmp_path, new, ok):
    root = write_project(tmp_path / "p", {"mod15.py": MOD,
                                          "invariants.yaml": RULES.replace("BAD", 'args.region in ["xx", "yy"]')})
    assert run_inv("lock", root / "invariants.yaml").returncode == 0
    (root / "invariants.yaml").write_text(RULES.replace("BAD", new))
    proc = run_inv("lock", root / "invariants.yaml")
    out = proc.stdout + proc.stderr
    if ok:
        assert proc.returncode == 0 and "WEAKENED" not in out, out
    else:
        assert proc.returncode == 1 and "WEAKENED" in out and "can raise an error" in out, out
        assert "a read of args.cfg.extra" in out and "a read of args.cfg," not in out, out


# --- R12-2: namespace reflection in the scan ------------------------------------------------------------------
STORE = textwrap.dedent("""
    def sink(c):
        c.send()
    def door(c):
        sink(c)
    class Box:
        def put(self, c):
            door(c)
    """)


def _doc():
    return {"targets": {"py:app.store.door": {"effects": "external"}},
            "rules": [{"id": "T-1", "status": "candidate", "subject": {"target": "py:app.store.door"},
                       "bad": "false", "enforcers": [{"point": "decorator", "timing": "pre_effect", "mode": "detect"}],
                       "mediation": {"sink": "py:app.store.sink", "door": "py:app.store.door"}}]}


def _codes(tmp_path, body):
    files = {"app/__init__.py": "", "app/store.py": STORE,
             "app/ops.py": "import inspect\nfrom app import store\n\ndef b(c, k):\n    " + body + "\n"}
    return {f.code for f in scan.scan(_doc(), [write_project(tmp_path, files)]).findings}


@pytest.mark.parametrize("body,want", [
    ('store.__dict__["sink"](c)', "forbidden/getattr-sink"),
    ('store.__dict__.get("sink")(c)', "forbidden/getattr-sink"),
    ('store.__getattribute__("sink")(c)', "forbidden/getattr-sink"),
    ('object.__getattribute__(store, "sink")(c)', "forbidden/getattr-sink"),
    ('type(store).__getattribute__(store, "sink")(c)', "forbidden/getattr-sink"),
    ("store.__dict__[k](c)", "unresolved/reflection"),
    ("store.__dict__.get(k)(c)", "unresolved/reflection"),
    ("store.__getattribute__(k)(c)", "unresolved/reflection"),
    ("object.__getattribute__(store, k)(c)", "unresolved/reflection"),
    ("[f for n, f in store.__dict__.items() if n == k][0](c)", "unresolved/reflection"),
    ("d = store.__dict__; d[k](c)", "unresolved/reflection"),
    ('dict(inspect.getmembers(store))["sink"](c)', "unresolved/reflection"),
    ('store.__dict__["sink"] = print', "unresolved/reflection"),
    ('store.Box.__dict__[k](store.Box(), c)', "unresolved/reflection"),
])
def test_namespace_reflection_is_not_a_pass(tmp_path, body, want):
    assert want in _codes(tmp_path, body)


def test_a_constant_key_on_another_name_is_an_edge(tmp_path):
    files = {"app/__init__.py": "", "app/store.py": STORE + "def helper(c):\n    sink(c)\n",
             "app/ops.py": 'from app import store\n\ndef b(c):\n    store.__dict__["helper"](c)\n'}
    got = {f.code for f in scan.scan(_doc(), [write_project(tmp_path, files)]).findings}
    assert "bypass" in got, got


def test_a_functions_dict_stays_harmless(tmp_path):
    got = _codes(tmp_path, 'print(store.sink.__dict__, store.door.__dict__.get("x"))')
    assert not got & {"bypass", "unresolved/reflection", "unresolved/sink-escapes", "forbidden/getattr-sink"}, got


def test_a_local_objects_getattribute_is_untouched(tmp_path):
    got = _codes(tmp_path, 'object.__getattribute__(c, "value")')
    assert not got, got


# --- R12-3: packaging ------------------------------------------------------------------------------------------
def test_the_fastapi_extra_brings_httpx():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert "httpx" in extras["fastapi"]


def test_the_cookbook_runs_ci_sh_with_bash():
    text = (ROOT / "docs" / "cookbook.md").read_text()
    assert "$ bash ci.sh" in text and "$ ./ci.sh" not in text


# --- R12-5: docs and scan roots ----------------------------------------------------------------------------------
def test_changelog_and_docstring_no_longer_trust_declared_int():
    assert "declared `int` at every enforcer" not in (ROOT / "CHANGELOG.md").read_text()
    assert "declared types make int or uint" not in (invariants_schema_diff.cel_canon._Canon.integral.__doc__ or "")


def test_readme_documents_path():
    assert "--path DIR" in (ROOT / "README.md").read_text()


def test_evaluation_leads_with_the_current_verdict():
    text = (ROOT / "docs" / "evaluation.md").read_text()
    assert "scratchpad" not in text and "fix round" not in text.lower() and "Another agent" not in text
    assert text.index("## Current results") < text.index("## Before: the original run")
    short = text[text.index("## Short answer"):text.index("## Current results")]
    assert "2 of the 24" in short and "1 of the 8 benign" in short and "6 of the 16" in short


PYPROJECT = '[project]\nname = "demo"\nversion = "0"\n'
SRC_RULES = """spec: invariant/v0.1
targets:
  "py:app.store.door": {effects: external}
rules:
  - id: T-1
    status: candidate
    subject: {target: "py:app.store.door"}
    bad: 'false'
    enforcers:
      - {point: decorator, timing: pre_effect, mode: detect}
    mediation:
      sink: "py:app.store.sink"
      door: "py:app.store.door"
"""


def _src_project(tmp_path, pyproject=PYPROJECT, bad=True):
    files = {"pyproject.toml": pyproject, "invariants.yaml": SRC_RULES, "src/app/__init__.py": "",
             "src/app/store.py": "def sink(c):\n    c.send()\ndef door(c):\n    sink(c)\n"}
    if bad:
        files["scripts/bad.py"] = "from app import store\ndef b(c):\n    store.sink(c)\n"
    return write_project(tmp_path / "proj", files)


def test_scan_finds_a_src_layout_by_itself(tmp_path):
    root = _src_project(tmp_path)
    proc = run_inv("scan", root / "invariants.yaml")
    assert proc.returncode == 1 and "missing" not in proc.stdout, proc.stdout + proc.stderr
    assert "scripts.bad.b -> app.store.sink" in proc.stdout       # files outside src/ are scanned too
    clean = _src_project(tmp_path / "c", bad=False)
    proc = run_inv("scan", clean / "invariants.yaml")
    assert proc.returncode == 0 and "pass" in proc.stdout, proc.stdout + proc.stderr


def test_scan_uses_setuptools_where(tmp_path):
    py = PYPROJECT + '[tool.setuptools.packages.find]\nwhere = ["lib"]\n'
    root = write_project(tmp_path / "proj", {"pyproject.toml": py, "invariants.yaml": SRC_RULES,
                                             "lib/app/__init__.py": "",
                                             "lib/app/store.py": "def sink(c):\n    c.send()\ndef door(c):\n    sink(c)\n"})
    assert cli.source_dirs(root / "invariants.yaml") == [(root / "lib").resolve()]
    proc = run_inv("scan", root / "invariants.yaml")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_nested_rule_file_does_not_scan_the_outer_projects_src(tmp_path):
    root = _src_project(tmp_path)
    nested = write_project(root / "fixtures" / "x", {"invariants.yaml": SRC_RULES})
    assert cli.source_dirs(nested / "invariants.yaml") == []


def test_overlapping_roots_name_each_file_once(tmp_path):
    root = _src_project(tmp_path)
    names = [n for _, _, n in scan.iter_sources([root, root / "src"])]
    assert "app.store" in names and "src.app.store" not in names and len(names) == len(set(names))
