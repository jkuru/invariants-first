"""The CI gate's fail-open holes after round 22: an added scan root judged harmless though it renames
files and widens the excludes (H1, H2); the always-skipped directories not pinned (M1); the agent hook
letting an edit of [tool.setuptools.packages.find] where change the scan roots; `inv mcp` check and
status reading (and echoing) a contract or artifact outside the server's --root (L1)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from invariants import lock
from cli_support import run_inv, write_project

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


def _commit(root: pathlib.Path, msg: str = "base") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


# --- H1: a new scan root renames the files under it -----------------------------------------------
PAY_RULES = '''\
spec: invariant/v0.1
targets:
  "py:app.payments.refund": {effects: external}
rules:
  - id: PAY-1
    status: enforce
    owner: payments
    statement: refund positive
    subject: {target: "py:app.payments.refund"}
    bad:
      - {when: 'args.amount <= 0', message: refund must be positive}
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject}
    mediation:
      chokepoint: money leaves only through refund
      sink: "py:app.payments.send_to_gateway"
    falsified_by: [tests/test_p.py::test_x]
'''
PAYMENTS = '''\
import invariants as inv


def send_to_gateway(charge_id: str, amount: int) -> None:
    print(f"refunded {amount} on {charge_id}")


@inv.guard("PAY-1")
def refund(charge_id: str, amount: int) -> None:
    send_to_gateway(charge_id, amount)
'''
R01 = {"pyproject.toml": '[project]\nname="x"\nversion="0"\n', "invariants.yaml": PAY_RULES,
       "src/app/__init__.py": "", "src/app/payments.py": PAYMENTS, "src/app/sub/__init__.py": "",
       # a back door, hidden once src/app/sub is a root of its own (ops.py is then module `ops`)
       "src/app/sub/ops.py": "from ..payments import send_to_gateway\ndef g():\n    send_to_gateway('c', 5)\n"}


def _r01(tmp_path: pathlib.Path) -> pathlib.Path:
    root = write_project(tmp_path / "r01", R01)
    r = run_inv("lock", "--accept-weakening", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def test_the_bypass_is_found_with_the_default_roots(tmp_path):
    r = run_inv("check", cwd=_r01(tmp_path))
    assert r.returncode == 1 and "bypass" in r.stdout, r.stdout


@pytest.mark.parametrize("setting", [
    '[tool.setuptools.packages.find]\nwhere = ["src", "src/app/sub"]\n',
    '[tool.invariants.scan]\nroots = [".", "src", "src/app/sub"]\n',
])
def test_adding_a_scan_root_is_a_weakening(tmp_path, setting):
    root = _r01(tmp_path)
    py = root / "pyproject.toml"
    py.write_text(py.read_text() + setting)
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "scan: roots added: (none) -> 'src/app/sub'  WEAKENED" in r.stdout, r.stdout
    assert "refused" in r.stderr
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout, r.stdout


@needs_git
def test_relock_against_the_base_refuses_an_added_root(tmp_path):
    root = _r01(tmp_path)
    (root / "src/app/sub/ops.py").write_text("def g():\n    pass\n")      # the base is clean
    assert run_inv("lock", cwd=root).returncode == 0
    _git(root, "init", "-q", "-b", "main")
    _commit(root)
    # the change: a back door, and a root that hides it
    (root / "src/app/sub/ops.py").write_text(R01["src/app/sub/ops.py"])
    py = root / "pyproject.toml"
    py.write_text(py.read_text() + '[tool.setuptools.packages.find]\nwhere = ["src", "src/app/sub"]\n')
    r = run_inv("check", "--relock", "--base", "HEAD", cwd=root)
    assert r.returncode == 1, r.stdout
    assert "roots added" in r.stdout and "WEAKENED" in r.stdout, r.stdout
    # a reviewer can still accept it on purpose
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0
    r = run_inv("check", "--base", "HEAD", cwd=root)
    assert r.returncode == 1 and "since HEAD" in r.stdout and "roots added" in r.stdout, r.stdout


@pytest.mark.parametrize("old,new,weak", [
    (["src"], [".", "src"], True),                     # added
    ([".", "src"], ["src"], True),                     # removed
    ([".", "src"], ["src", "."], False),               # the same set: a file's name does not depend on order
])
def test_every_change_to_the_root_set_is_a_weakening(old, new, weak):
    a = {"lock_version": lock.LOCK_VERSION, "scan": lock.scan_pin(old, [])}
    b = {"lock_version": lock.LOCK_VERSION, "scan": lock.scan_pin(new, [])}
    cs = lock.changes(a, b)
    assert any(c.weakened for c in cs) is weak, [str(c) for c in cs]
    assert bool(cs) is weak


# --- H2: a new root makes the existing excludes match more ----------------------------------------
R02_RULES = '''\
spec: invariant/v0.1
targets:
  "py:payments.refund": {effects: external}
rules:
  - id: PAY-1
    status: enforce
    owner: payments
    statement: refund positive
    subject: {target: "py:payments.refund"}
    bad:
      - {when: 'args.amount <= 0', message: refund must be positive}
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject}
    mediation:
      chokepoint: money leaves only through payments.refund
      sink: "py:payments.send_to_gateway"
    falsified_by: [test_payments.py::test_x]
'''


def test_a_root_that_makes_an_exclude_match_more_is_a_weakening(tmp_path):
    # the reviewer's r02: exclude `legacy` matches nothing under ".", but app/legacy/pay.py once
    # app is a root (excludes match root-relative paths)
    root = write_project(tmp_path / "r02", {
        "pyproject.toml": '[project]\nname="x"\nversion="0"\n[tool.invariants.scan]\nroots=["."]\n'
                          'exclude=["tests", "*/tests", "legacy"]\n',
        "invariants.yaml": R02_RULES,
        "payments.py": PAYMENTS.replace("PAY-1", "PAY-1"),
        "app/legacy/pay.py": "from payments import send_to_gateway\ndef pay():\n    send_to_gateway('c', 5)\n",
    })
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "bypass" in r.stdout, r.stdout
    py = root / "pyproject.toml"
    py.write_text(py.read_text().replace('roots=["."]', 'roots=[".", "app"]'))
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 1 and "roots added" in r.stdout and "WEAKENED" in r.stdout, r.stdout


# --- M1: the always-skipped directories are pinned ------------------------------------------------
def test_the_skip_list_is_pinned_from_what_the_scan_skips():
    from invariants.scan import SKIP_DIRS
    pin = lock.scan_pin(["."], [])
    assert pin["skip"] == sorted({*SKIP_DIRS, ".*"}) == lock.scan_skip()


def test_a_wider_skip_list_is_a_weakening_and_a_narrower_one_is_not():
    base = {"lock_version": lock.LOCK_VERSION, "scan": lock.scan_pin(["."], [])}
    wider = {"lock_version": lock.LOCK_VERSION, "scan": lock.scan_pin(["."], [], [*lock.scan_skip(), "lib"])}
    cs = lock.changes(base, wider)
    assert [str(c) for c in cs] == ["scan: skip added: (none) -> 'lib'  WEAKENED "
                                    "(the scan now always skips directories with this name)"]
    cs = lock.changes(wider, base)
    assert cs and not any(c.weakened for c in cs)


def test_a_scan_pin_with_a_malformed_skip_list_is_refused(tmp_path):
    p = tmp_path / "invariants.lock"
    p.write_text(json.dumps({"lock_version": 3, "scan": {"roots": ["."], "exclude": [], "skip": "build"}}))
    with pytest.raises(lock.LockError, match="scan.skip"):
        lock.read(p)


# --- the hook: pyproject settings the scan roots come from ----------------------------------------
def _hook(payload: dict, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


V0 = 'version="0"\n'


@pytest.mark.parametrize("old,new,blocked", [
    (V0, V0 + '[tool.setuptools.packages.find]\nwhere=["src", "src/app/sub"]\n', True),    # the reviewer's probe
    (V0, V0 + '[tool.invariants.scan]\nroots=["src", "src/app/sub"]\n', True),
    ("[project]\n", 'tool.setuptools.packages.find.where=["src/app/sub"]\n[project]\n', True),   # a dotted key
    (V0, V0 + '[tool.setuptools]\npackages=["app"]\n', True),
    (V0, 'version="1"\n', False),
    (V0, V0 + '[tool.setuptools.package-data]\napp=["*.json"]\n', False),
])
def test_hook_blocks_edits_of_the_pyproject_settings_the_scan_roots_come_from(tmp_path, old, new, blocked):
    root = write_project(tmp_path / "r01", R01)
    r = _hook({"tool_name": "Edit", "cwd": str(root),
               "tool_input": {"file_path": "pyproject.toml", "old_string": old, "new_string": new}}, root)
    assert r.returncode == (2 if blocked else 0), r.stderr
    assert ("scan settings in pyproject.toml" in r.stderr) is blocked


def test_hook_blocks_an_existing_where_being_changed(tmp_path):
    root = write_project(tmp_path / "p", {"pyproject.toml": '[tool.setuptools.packages.find]\nwhere = ["src"]\n'})
    r = _hook({"tool_name": "Edit", "cwd": str(root), "tool_input": {
        "file_path": "pyproject.toml", "old_string": 'where = ["src"]', "new_string": 'where = ["src", "lib"]'}}, root)
    assert r.returncode == 2, r.stderr


def test_hook_scan_inputs_are_what_the_scan_settings_read():
    from invariants.cli import scan_inputs
    doc = {"tool": {"setuptools": {"packages": {"find": {"where": ["src"]}}, "package-data": {}},
                    "invariants": {"scan": {"roots": ["."]}}, "ruff": {}}}
    assert scan_inputs(doc) == {"tool.invariants": {"scan": {"roots": ["."]}},
                                "tool.setuptools.packages": {"find": {"where": ["src"]}}}


# --- L1: inv mcp confines contract and artifact reads to its --root -------------------------------
OUTSIDE = "OUTSIDE-ROOT-CONTENT-sk-live-123"
MCP_RULES = '''\
spec: invariant/v0.1
contracts:
  api: {type: openapi, ref: ../outside_api.json}
targets:
  "py:payments.refund": {effects: external}
rules:
  - id: PAY-1
    status: enforce
    owner: payments
    statement: refund positive
    subject: {target: "py:payments.refund"}
    bad:
      - {when: 'args.amount <= 0', message: refund must be positive}
    enforcers:
      - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject}
    falsified_by: [test_payments.py::test_x]
'''
OPENAPI = {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": {"/a": {"get": {
    "operationId": "a", "responses": {"200": {"description": "ok", "content": {"application/json": {
        "schema": {"type": "string", "enum": ["placeholder"]}}}}}}}}}


def _mcp_project(tmp_path: pathlib.Path) -> pathlib.Path:
    proj = write_project(tmp_path / "proj", {"invariants.yaml": MCP_RULES, "payments.py": PAYMENTS})
    (tmp_path / "outside_api.json").write_text(json.dumps(OPENAPI))
    assert run_inv("lock", cwd=proj).returncode == 0
    spec = json.loads(json.dumps(OPENAPI).replace("placeholder", OUTSIDE))
    (tmp_path / "outside_api.json").write_text(json.dumps(spec))
    return proj


@pytest.mark.parametrize("tool", ["check", "status"])
def test_mcp_does_not_read_a_contract_outside_its_root(tmp_path, tool):
    from invariants.mcp import tools
    from invariants.mcp.confine import ToolError
    proj = _mcp_project(tmp_path)
    try:
        out = json.dumps(getattr(tools, "tool_" + tool)(proj.resolve(), None, {}))
    except ToolError as e:
        out = str(e)
    assert OUTSIDE not in out
    if tool == "check":
        assert "confines reads to" in out, out


def test_plain_inv_check_still_reads_a_contract_beside_the_project(tmp_path):
    proj = _mcp_project(tmp_path)
    r = run_inv("check", cwd=proj)
    assert r.returncode == 1 and OUTSIDE in r.stdout and "confines" not in r.stdout, r.stdout


def test_an_artifact_outside_the_confined_root_is_refused(tmp_path, monkeypatch):
    from invariants.confinement import CONFINE_ENV, confined
    from invariants.errors import LoadError
    (tmp_path / "proj").mkdir()
    (tmp_path / "art.txt").write_text("x")
    (tmp_path / "proj" / "link.txt").symlink_to(tmp_path / "art.txt")
    monkeypatch.delenv(CONFINE_ENV, raising=False)
    assert confined(tmp_path / "proj" / "../art.txt", "artifact")          # unset: anywhere
    monkeypatch.setenv(CONFINE_ENV, str(tmp_path / "proj"))
    for ref in ("../art.txt", "link.txt"):
        with pytest.raises(LoadError, match="confines reads to"):
            confined(tmp_path / "proj" / ref, "R-1: artifact")
    assert confined(tmp_path / "proj" / "inside.txt", "R-1: artifact")


def test_mcp_loads_an_artifact_check_under_confinement(tmp_path):
    from invariants.mcp import tools
    proj = write_project(tmp_path / "proj", {"invariants.yaml": MCP_RULES.replace(
        "  api: {type: openapi, ref: ../outside_api.json}\n", "  {}\n").replace("contracts:\n  {}\n", "").replace(
        "on_violation: reject}", "on_violation: reject,\n         artifact: '../art.txt#sha256:00', conformance_test: t}"),
        "payments.py": PAYMENTS})
    (tmp_path / "art.txt").write_text("secret")
    out = json.dumps(tools.tool_check(proj.resolve(), None, {}))
    assert "artifact ../art.txt resolves outside" in out, out
