"""Fix round 20 (cli, mcp): `inv check` runs verify whenever there is something to import, not only
when a rule names subject.target (1); the MCP server refuses a pyproject.toml that is a symlink out of
--root, and a tool argument of the wrong type instead of treating it as absent (2); `inv validate`
labels each check's problems from a table, not from the check's module (3); the CLI reads and writes
its text files as UTF-8 whatever the locale (4)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

from cli_support import run_inv, write_project
from invariants import lock, mcp
from invariants.cli import commands
from invariants.lock import pinning

# a targetless rule, and a guard in the app that names a rule no file has
GUARDED = {"app/__init__.py": "",
           "app/svc.py": "import invariants as inv\n\n@inv.guard('NOPE')\ndef f(x):\n    return x\n"}
TARGETLESS = {"id": "R-1", "status": "candidate", "bad": "false"}


def _project(tmp_path: pathlib.Path, doc: dict, files: dict[str, str] = GUARDED) -> pathlib.Path:
    root = write_project(tmp_path / "proj", files)
    (root / "invariants.json").write_text(json.dumps(doc))
    r = run_inv("lock", root / "invariants.json", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


# --- 1. inv check runs verify ---------------------------------------------------------------------
def test_check_verifies_the_guards_an_import_finds(tmp_path):
    root = _project(tmp_path, {"rules": [TARGETLESS]})
    r = run_inv("check", root / "invariants.json", "--import", "app.svc", cwd=root)
    assert r.returncode == 1, r.stdout
    assert "FAIL  guards" in r.stdout and "guards NOPE, which is not in the rule file" in r.stdout


def test_check_verifies_the_guards_in_a_python_contract(tmp_path):
    doc = {"contracts": {"svc": {"type": "python", "ref": "app.svc"}}, "rules": [TARGETLESS]}
    root = _project(tmp_path, doc)
    r = run_inv("check", root / "invariants.json", cwd=root)
    assert r.returncode == 1 and "guards NOPE" in r.stdout, r.stdout


def test_check_says_why_it_skips_verify(tmp_path):
    root = _project(tmp_path, {"rules": [TARGETLESS]})
    r = run_inv("check", root / "invariants.json", cwd=root)
    assert r.returncode == 0, r.stdout
    assert "skip  guards: nothing to import" in r.stdout and "--import" in r.stdout


def test_importable_names_every_module_verify_reaches():
    doc = {"targets": {"py:a.t": {}}, "contracts": {"c": {"type": "python", "ref": "b.m:X"},
                                                   "o": {"type": "openapi", "app": "c.main:app"}},
           "rules": [{"subject": {"target": "py:d.f"}, "enforcers": [{"point": "edge", "target": "py:e.g"}]},
                     "not a rule"]}
    assert commands._importable(doc, ["f"]) == ["py:a.t", "py:d.f", "py:e.g", "b.m", "c.main", "f"]
    assert commands._importable({"rules": [TARGETLESS]}, []) == []


# --- 2. mcp ---------------------------------------------------------------------------------------
def test_settings_refuse_a_pyproject_symlinked_outside_root(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('[project]\nname = "x"\n')
    root = tmp_path / "srv"
    root.mkdir()
    (root / "invariants.json").write_text("{}")
    (root / "pyproject.toml").symlink_to(outside)
    with pytest.raises(mcp.ToolError, match="pyproject.toml resolves to .*outside the server's --root"):
        mcp.confine._settings_inside(root.resolve(), root / "invariants.json")


@pytest.mark.parametrize("args, what", [
    ({"graphify": 3}, "graphify must be a string"),
    ({"graphify": ["g.json"]}, "graphify must be a string"),
    ({"no_graphify": "yes"}, "no_graphify must be a boolean"),
    ({"root": 7}, "root must be a string"),
])
def test_a_wrongly_typed_argument_is_a_tool_error(tmp_path, args, what):
    root = write_project(tmp_path / "p", {"app/__init__.py": "", "app/x.py": "def f():\n    pass\n"})
    with pytest.raises(mcp.ToolError, match=what):
        mcp.tool_propose_rules(root.resolve(), None, args)


def test_a_wrongly_typed_rules_or_root_is_a_tool_error_in_every_tool(tmp_path):
    root = write_project(tmp_path / "p", {"invariants.json": "{}"}).resolve()
    for tool in (mcp.tool_scan, mcp.tool_check, mcp.tool_status, mcp.tool_explain):
        with pytest.raises(mcp.ToolError, match="rules must be a string"):
            tool(root, root / "invariants.json", {"rules": 1, "id": "R-1"})
        with pytest.raises(mcp.ToolError, match="root must be a string"):
            tool(root, None, {"root": False, "id": "R-1"})


# --- 3. validate labels -----------------------------------------------------------------------------
def test_validate_labels_come_from_the_table():
    assert [label for label, _ in commands.VALIDATE_CHECKS] == ["scan", "lockfile"]
    assert lock.expand_applies_to is pinning.expand_applies_to


def test_validate_label_of_an_applies_to_problem(tmp_path):
    doc = {"rules": [{"id": "R-1", "status": "candidate", "bad": "false", "applies_to": "py:nomatch.*"}]}
    (tmp_path / "invariants.json").write_text(json.dumps(doc))
    r = run_inv("validate", tmp_path / "invariants.json", cwd=tmp_path)
    assert r.returncode == 1 and "  lockfile: R-1: applies_to" in r.stderr, r.stderr


# --- 4. a non-UTF-8 locale ---------------------------------------------------------------------------
LATIN1 = {"PYTHONUTF8": "0", "LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1"}


def _inv_env(env: dict[str, str], *args: object, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env}
    full.pop("INV_ABLATE", None)
    return subprocess.run([sys.executable, "-m", "invariants.cli", *map(str, args)], capture_output=True,
                          cwd=cwd, env=full, timeout=60, encoding="utf-8", errors="replace")


def _locale_is_latin1() -> bool:
    r = subprocess.run([sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
                       capture_output=True, text=True, env={**os.environ, **LATIN1})
    return r.stdout.strip().upper().replace("-", "").replace("_", "") in {"ISO88591", "LATIN1"}


@pytest.mark.skipif(not _locale_is_latin1(), reason="no ISO-8859-1 locale on this machine")
def test_lock_round_trip_under_a_latin1_locale(tmp_path):
    doc = {"rules": [{"id": "R-1", "status": "candidate", "bad": "'über 100 €' == 'x'",
                      "statement": "Erstattungen über 100 € brauchen eine Freigabe – immer."}]}
    runs = {}
    for name, env in (("utf8", {"PYTHONUTF8": "1"}), ("latin1", LATIN1)):
        d = tmp_path / name
        d.mkdir()
        (d / "invariants.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        r = _inv_env(env, "lock", d / "invariants.yaml", cwd=d)
        assert r.returncode == 0, r.stdout + r.stderr
        r = _inv_env(env, "lock", d / "invariants.yaml", "--check", cwd=d)
        assert r.returncode == 0 and "fresh" in r.stdout, r.stdout + r.stderr
        runs[name] = (d / "invariants.lock").read_bytes()
    assert "über 100 €".encode() in runs["utf8"]          # the clause is pinned as raw UTF-8
    assert runs["latin1"] == runs["utf8"]


def test_status_verifies_the_same_modules_as_verify(tmp_path):
    """inv status checks guards in each --import's package, as inv verify does: a guard on a rule the
    file does not define fails both."""
    root = write_project(tmp_path / "proj", {
        "pyproject.toml": '[project]\nname = "p"\nversion = "0"\n',
        "app/__init__.py": "", "app/svc.py": "import invariants as inv\n\n@inv.guard('NOPE')\ndef f(x):\n    return x\n",
        "invariants.yaml": "rules:\n  - id: R-1\n    status: candidate\n    bad: 'false'\n"})
    v = run_inv("verify", "--import", "app.svc", cwd=root)
    s = run_inv("status", "--import", "app.svc", "--json", cwd=root)
    assert v.returncode != 0 and "NOPE" in v.stdout + v.stderr
    data = json.loads(s.stdout)
    assert data["verify"]["passed"] is False and any("NOPE" in p for p in data["verify"]["problems"])
