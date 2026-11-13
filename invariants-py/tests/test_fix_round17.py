"""Fix round 17 (lock and hook review findings): each test failed before its fix.

1. `inv check --base` with invariants.lock replaced by a symlink, or moved away, is not a first
   adoption: the lock is looked up at its own repository path, a symlinked lock is refused, and a
   base lock that the change moved or removed fails.
2. additionalProperties false -> {} or a schema is a weakening; a schema -> a looser schema is judged
   inside it.
3. An OpenAPI operation renamed with the same method and path is compared field by field; one
   removed with no counterpart is a weakening when a rule names it.
4. The pretool hook: no abbreviated --accept-weakening, --output and git -c / GIT_* count as writes,
   quoted and globbed names match, and commands it cannot read are blocked when they mention the lock.
5. A lock pinned before the scan was pinned is compared with the roots the base would have defaulted to.
6. The integer slack of a bound and its twin applies only to integral bounds.
7. Removing items, allOf, not, contains, propertyNames, dependentRequired or if/then/else is a weakening.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from invariants import cli, lockfile
from cli_support import run_inv, write_project

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

API = 'import invariants as inv\n\n\n@inv.guard("R-1")\ndef refund(amount: int) -> None:\n    pass\n'
RULES = 'rules:\n  - id: R-1\n    guard: "py:gate17.api.refund"\n    bad: "args.amount > 10000"\n'


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


def _commit(root: pathlib.Path, msg: str = "base") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _app(root: pathlib.Path, files: dict[str, str] | None = None) -> pathlib.Path:
    write_project(root, files or {"pyproject.toml": '[project]\nname = "gate17"\nversion = "0"\n',
                                  "src/gate17/__init__.py": "", "src/gate17/api.py": API, "invariants.yaml": RULES})
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    _git(root, "init", "-q", "-b", "main")
    _commit(root)
    _git(root, "checkout", "-qb", "pr")
    return root


# --- 1. --base and a symlinked or moved lock --------------------------------------------------------
@needs_git
def test_a_symlinked_lock_is_not_a_first_adoption(tmp_path):
    root = _app(tmp_path / "p")
    (root / "invariants.yaml").write_text(RULES.replace("10000", "99999999"))
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    _git(root, "mv", "invariants.lock", "weak.lock")
    (root / "invariants.lock").symlink_to("weak.lock")
    _commit(root, "pr")
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 1 and "first adoption of" not in r.stdout, r.stdout
    assert "symlink" in r.stdout, r.stdout
    r = run_inv("check", "--base", "main", "--allow-weakening", cwd=root)
    assert r.returncode == 1, r.stdout                 # a symlinked lock is never accepted


@needs_git
def test_a_lock_moved_away_from_the_base_path_fails(tmp_path):
    root = _app(tmp_path / "p")
    (root / "sub").mkdir()
    for n in ("invariants.yaml", "invariants.lock"):
        _git(root, "mv", n, f"sub/{n}")
    _commit(root, "pr")
    r = run_inv("check", "sub/invariants.yaml", "--base", "main", cwd=root)
    assert r.returncode == 1 and "first adoption of" not in r.stdout, r.stdout
    assert "moved or removed" in r.stdout, r.stdout


@needs_git
def test_a_lock_that_stayed_still_compares(tmp_path):
    root = _app(tmp_path / "p")
    (root / "invariants.yaml").write_text(RULES.replace("10000", "99999999"))
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    _commit(root, "pr")
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 1 and "weaken a rule" in r.stdout, r.stdout


# --- 2. additionalProperties ------------------------------------------------------------------------
def _schema_diff(a, b):
    ch = lockfile._schema_changes([("contract c", "schema", "A", "B")], {"A": {"name": "M", "pydantic": a}},
                                  {"B": {"name": "M", "pydantic": b}})
    return any(c.weakened for c in ch), [str(c) for c in ch]


@pytest.mark.parametrize("old,new,weak", [
    (False, {}, True),
    (False, {"type": "string"}, True),
    (False, None, True),
    (None, True, True),
    ({"type": "string"}, False, False),
    ({"type": "string"}, {}, True),                       # a looser schema, judged inside it
    ({"type": "integer", "maximum": 3}, {"type": "integer", "maximum": 5}, True),
    ({"type": "integer", "maximum": 5}, {"type": "integer", "maximum": 3}, False),
    ({}, {"type": "string"}, False),
])
def test_additional_properties(old, new, weak):
    a = {"type": "object", **({} if old is None else {"additionalProperties": old})}
    b = {"type": "object", **({} if new is None else {"additionalProperties": new})}
    got, cs = _schema_diff(a, b)
    assert got is weak, cs


@pytest.mark.parametrize("old,new,weak", [(False, {}, True), (False, {"type": "string"}, True), (True, False, False)])
def test_additional_properties_without_neighbours(old, new, weak):
    assert (lockfile.constraint_weakening("pydantic.additionalProperties", old, new) is not None) is weak


# --- 3. OpenAPI operation renames -------------------------------------------------------------------
def _op(mx: int, method: str = "POST", path: str = "/refunds") -> dict:
    return {"method": method, "path": path, "requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "properties": {"amount": {"type": "integer", "maximum": mx}}}}}}}


def _api(ops: dict) -> dict:
    return {"type": "openapi", "app": "m:app", "operations": ops}


def _lock(contract: dict, rules: dict | None = None) -> dict:
    return {"lock_version": lockfile.LOCK_VERSION, "rules": rules or {}, "targets": {}, "providers": {},
            "contracts": {"api": contract}, "schemas": {}}


def test_a_renamed_operation_is_compared_field_by_field():
    cs = lockfile.changes(_lock(_api({"create_refund": _op(10000)})), _lock(_api({"make_refund": _op(10**9)})))
    weak = [c for c in cs if c.weakened]
    assert weak and any("maximum" in c.what for c in weak), [str(c) for c in cs]
    assert any("renamed" in c.what for c in cs), [str(c) for c in cs]
    cs = lockfile.changes(_lock(_api({"create_refund": _op(10000)})), _lock(_api({"make_refund": _op(100)})))
    assert not any(c.weakened for c in cs), [str(c) for c in cs]


def test_an_operation_removed_with_no_counterpart():
    rules = {"R": {"semantic": {"subject": {"contract": "api", "operation": "create_refund"}}, "targets": [],
                   "doc": {}}}
    old = _api({"create_refund": _op(10000), "health": _op(1, "GET", "/health")})
    cs = lockfile.changes(_lock(old, rules), _lock(_api({"health": _op(1, "GET", "/health")}), rules))
    assert any(c.weakened and "create_refund" in c.what for c in cs), [str(c) for c in cs]
    cs = lockfile.changes(_lock(old), _lock(_api({"create_refund": _op(10000)})))
    gone = [c for c in cs if "health" in c.what]
    assert gone and not any(c.weakened for c in gone), [str(c) for c in cs]


# --- 4. the pretool hook ----------------------------------------------------------------------------
def _hook(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(ev),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


@pytest.mark.parametrize("command,blocked", [
    ("cp /tmp/x invariants.lock", True),
    ("git diff --output=invariants.lock HEAD~1", True),
    ("git log -1 --output=invariants.lock", True),
    ("git -c core.pager='cp /tmp/x invariants.lock' -p log -1", True),
    ("git -c alias.x='!cp /tmp/x invariants.lock' x", True),
    ("GIT_EXTERNAL_DIFF=./x.sh git diff invariants.lock", True),
    ("env GIT_PAGER=./x.sh git log invariants.lock", True),
    ("sort -o invariants.lock x", True),
    ("cp /tmp/x \"invariants\".lock", True),
    ("cp /tmp/x invariants\\.lock", True),
    ("cp /tmp/x inv*.lock", True),
    ("cp /tmp/x *.lock", True),
    ("f=invariants.lo; cp /tmp/x ${f}ck", True),
    ("rg --pre 'sh -c \"cp /tmp/x invariants.lock\"' .", True),
    ("python -c \"open('invariants'+'.lock','w').write('{}')\"", True),
    ("echo $(cat x) > /dev/null; cat invariants.lock", True),
    ("eval \"cat invariants.lock\"", True),
    ("printf x >> pyproject''.toml", True),
    ("printf x >> pyproject.tom?", True),
    ("cat pyproject.toml | python -c 'import sys'", True),
    ("cat 'invariants.lock", True),                     # does not parse
    ("inv lock --accept-weaken", True),
    ("inv lock --accept-w", True),
    ("inv check --base main --allo", True),
    ("inv lock --accept-weakening=1", True),
    # still allowed
    ("git add invariants.lock", False),
    ("inv lock && git add invariants.lock", False),
    ("diff -u /tmp/a invariants.lock", False),
    ("cat invariants.lock | jq .", False),
    ("grep -o rule invariants.lock", False),
    ("git log --all --oneline", False),
    ("inv check --relock", False),
    ("echo \"$HOME\" > /tmp/out.txt", False),
    ("pytest -q", False),
])
def test_hook_shell_commands(tmp_path, command, blocked):
    (tmp_path / "invariants.lock").write_text("{}")
    (tmp_path / "pyproject.toml").write_text('[tool.invariants.scan]\nexclude = ["tests"]\n')
    r = _hook(command, tmp_path)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


@pytest.mark.parametrize("argv", [["lock", "--accept-w"], ["check", "--base", "main", "--allow"],
                                  ["lock", "--acc"]])
def test_inv_takes_no_abbreviated_flags(argv, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.parser().parse_args(argv)
    assert ei.value.code == cli.USAGE and "unrecognized arguments" in capsys.readouterr().err


# --- 5. the first scan pin compares with the base's default roots ------------------------------------
DOOR = {
    "src/gate17d/__init__.py": "",
    "src/gate17d/repo.py": "def write(x):\n    return x\n",
    "src/gate17d/service.py": "from gate17d.repo import write\n\n\ndef save(x):\n    return write(x)\n",
    "invariants.yaml": 'rules:\n  - id: DOOR\n    door: "py:gate17d.service.save"\n    sink: "py:gate17d.repo.write"\n',
}
PYPROJECT = '[project]\nname = "gate17d"\nversion = "0"\n'


@needs_git
@pytest.mark.parametrize("scan,weak", [('\n[tool.invariants.scan]\nroots = ["src"]\n', True), ("", False)])
def test_first_scan_pin_against_the_base_defaults(tmp_path, scan, weak):
    root = tmp_path / "d"
    write_project(root, {**DOOR, "pyproject.toml": PYPROJECT})
    assert run_inv("lock", cwd=root).returncode == 0
    lock = json.loads((root / "invariants.lock").read_text())
    lock.pop("scan")                                   # pinned before the scan was pinned
    (root / "invariants.lock").write_text(lockfile.dumps(lock))
    _git(root, "init", "-q", "-b", "main")
    _commit(root)
    _git(root, "checkout", "-qb", "pr")
    (root / "pyproject.toml").write_text(PYPROJECT + scan)
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    _commit(root, "pr")
    r = run_inv("check", "--base", "main", cwd=root)
    assert (r.returncode == 1) is weak, r.stdout
    assert ("roots removed" in r.stdout) is weak, r.stdout


def test_scan_changes_with_a_default():
    old = {"lock_version": lockfile.LOCK_VERSION}
    new = {"lock_version": lockfile.LOCK_VERSION, "scan": {"roots": ["src"], "exclude": ["tests"]}}
    cs = lockfile.changes(old, new, scan_default={"roots": [".", "src"], "exclude": ["tests"]})
    assert any(c.weakened and c.what == "roots removed" for c in cs), [str(c) for c in cs]
    cs = lockfile.changes(old, new, scan_default={"roots": ["src"], "exclude": ["tests"]})
    assert not any(c.weakened for c in cs), [str(c) for c in cs]


# --- 6. integer slack only for integral bounds -------------------------------------------------------
def _int(**kw):
    return {"type": "object", "properties": {"x": {"type": "integer", **kw}}}


@pytest.mark.parametrize("a,b,weak", [
    (_int(minimum=1.5), _int(exclusiveMinimum=0.6), True),
    (_int(maximum=2.5), _int(exclusiveMaximum=3.4), True),
    (_int(minimum=1), _int(exclusiveMinimum=0), False),
    (_int(maximum=10), _int(exclusiveMaximum=11), False),
    (_int(minimum=1.0), _int(exclusiveMinimum=0.0), False),
    (_int(minimum=1), _int(exclusiveMinimum=0.5), True),
])
def test_twin_bounds_on_integers(a, b, weak):
    got, cs = _schema_diff(a, b)
    assert got is weak, cs


# --- 7. structural keywords --------------------------------------------------------------------------
def _x(**kw):
    return {"type": "object", "properties": {"x": kw}}


@pytest.mark.parametrize("a,b,weak", [
    (_x(**{"not": {"const": 0}}), _x(), True),
    (_x(allOf=[{"maximum": 3}]), _x(), True),
    (_x(type="array", items={"type": "integer", "maximum": 3}), _x(type="array"), True),
    (_x(type="array", items={"type": "integer", "maximum": 3}), _x(type="array", items={"type": "integer"}), True),
    (_x(type="array", prefixItems=[{"type": "integer"}]), _x(type="array"), True),
    (_x(type="array", contains={"const": 1}), _x(type="array"), True),
    (_x(type="array", contains={"const": 1}, minContains=2), _x(type="array", contains={"const": 1}), True),
    (_x(type="object", propertyNames={"pattern": "^a"}), _x(type="object"), True),
    (_x(type="object", dependentRequired={"a": ["b"]}), _x(type="object"), True),
    (_x(type="object", dependentRequired={"a": ["b", "c"]}), _x(type="object", dependentRequired={"a": ["b"]}), True),
    (_x(**{"if": {"const": 1}, "then": {"maximum": 3}}), _x(**{"if": {"const": 1}}), True),
    (_x(**{"if": {"const": 1}, "then": {"maximum": 3}, "else": {"maximum": 5}}),
     _x(**{"if": {"const": 1}, "then": {"maximum": 3}}), True),
    (_x(oneOf=[{"type": "integer"}, {"type": "string"}]), _x(), True),
    (_x(**{"not": {"maximum": 5}}), _x(**{"not": {"maximum": 3}}), True),   # inside not, polarity flips
    # adding them only tightens
    (_x(), _x(**{"not": {"const": 0}}), False),
    (_x(), _x(allOf=[{"maximum": 3}]), False),
    (_x(type="array"), _x(type="array", items={"type": "integer"}), False),
    (_x(type="object"), _x(type="object", dependentRequired={"a": ["b"]}), False),
    (_x(type="array"), _x(type="array", contains={"const": 1}), False),
])
def test_structural_keywords(a, b, weak):
    got, cs = _schema_diff(a, b)
    assert got is weak, cs
