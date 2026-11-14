"""Round 29, the gate (security review 2): `inv check --base` reads no verdict input after the child that
imports the app ran (a module rewriting pyproject.toml back to the base's scan settings no longer hides
an added exclude); the GitHub Action runs invariants from an interpreter of its own, never the `inv` on
PATH; `python -m invariants.cli` drops the working directory from sys.path (a project's yaml.py is not
imported by the deciding process); control characters and `::` workflow commands from file names are
escaped in text output; the agent hook sees `patch`, a git command that moves the tree to another
commit, and a recursive copy of a directory holding the lock."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

from cli_support import run_inv, write_project
from invariants import lock

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
REPO = pathlib.Path(__file__).resolve().parents[2]


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, env=GIT_ENV, check=True, capture_output=True)


# --- 1. no verdict input is read after the child ran --------------------------------------------------
RULES = '''\
spec: invariant/v0.1
rules:
  - id: PAY-1
    guard: py:r29app.pay.pay
    bad: 'args.amount > 5.0'
  - id: DOOR
    door: py:r29app.service.BillingService.refund
    sink: py:r29app.gateway.RefundGateway.refund
'''
FILES = {
    "r29app/__init__.py": "",
    "r29app/gateway.py": "class RefundGateway:\n    def refund(self, n): return n\n",
    "r29app/service.py": ("from r29app.gateway import RefundGateway\n\n\nclass BillingService:\n"
                          "    def refund(self, n): return RefundGateway().refund(n)\n"),
    "r29app/pay.py": ('import invariants as inv\n\n\n@inv.guard("PAY-1")\ndef pay(amount: float) -> None:\n'
                      '    pass\n'),
    "pyproject.toml": '[project]\nname = "p"\nversion = "0"\n[tool.invariants.scan]\nroots = ["."]\n',
    "invariants.yaml": RULES,
}
BACK_DOOR = "from r29app.gateway import RefundGateway\n\n\ndef goodwill():\n    return RefundGateway().refund(10**6)\n"
# the pull request's module, imported by the child: puts pyproject.toml back to the base's settings
REWRITES = ('import pathlib as _p\n_f = _p.Path(__file__).resolve().parent.parent / "pyproject.toml"\n'
            '_f.write_text(_f.read_text().replace(\'extend_exclude = ["scripts/evil.py"]\\n\', ""))\n')


def _excluding_branch(tmp_path: pathlib.Path) -> pathlib.Path:
    """main scans everything; the branch excludes a back door, re-pins, and rewrites pyproject.toml at import."""
    root = write_project(tmp_path / "p", FILES)
    assert run_inv("lock", cwd=root).returncode == 0
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "pr")
    write_project(root, {"scripts/__init__.py": "", "scripts/evil.py": BACK_DOOR})
    py = root / "pyproject.toml"
    py.write_text(py.read_text() + 'extend_exclude = ["scripts/evil.py"]\n')
    r = run_inv("lock", "--accept-weakening", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    (root / "r29app" / "pay.py").write_text(REWRITES + FILES["r29app/pay.py"])
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "pr")
    return root


@needs_git
def test_a_module_rewriting_pyproject_cannot_hide_an_added_exclude(tmp_path):
    root = _excluding_branch(tmp_path)
    r = run_inv("check", "--base", "main", cwd=root)
    assert 'extend_exclude' not in (root / "pyproject.toml").read_text()      # the module did rewrite it
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL  since main" in r.stdout and "exclude added" in r.stdout and "WEAKENED" in r.stdout, r.stdout


AUDIT = '''\
import sys
import invariants.cli.worker as w
real = w.run_child
state = {"after": False}


def run_child(*a, **k):
    got = real(*a, **k)
    state["after"] = True
    return got


w.run_child = run_child
root = sys.argv[1]


def hook(event, args):
    if state["after"] and event in ("open", "os.listdir", "os.scandir", "subprocess.Popen"):
        p = str(args[0])
        if root in p and "__pycache__" not in p:
            sys.__stderr__.write(f"READ-AFTER-CHILD {event} {p}\\n")


sys.addaudithook(hook)
from invariants import cli
print("CODE", cli.main(sys.argv[2:]))
'''


@needs_git
@pytest.mark.parametrize("args", [["check", "--base", "main"], ["check", "--base", "main", "--relock"],
                                  ["check"], ["lock", "--check"]])
def test_no_project_file_is_read_after_the_child_ran(tmp_path, args):
    root = _excluding_branch(tmp_path)
    spy = tmp_path / "audit.py"
    spy.write_text(AUDIT)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("INV_ALLOW_WEAKENING", None)
    r = subprocess.run([sys.executable, str(spy), str(root.resolve()), *args], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert "CODE" in r.stdout, r.stdout + r.stderr
    assert "READ-AFTER-CHILD" not in r.stderr, r.stderr


def test_build_refuses_scan_settings_not_registered_before_the_child(tmp_path, monkeypatch):
    from invariants.cli.worker import Imported
    from invariants.errors import LoadError
    root = write_project(tmp_path / "p", FILES)
    imp = Imported(root / "invariants.yaml", [], yaml.safe_load(RULES))
    first = lock.scan_pin(["."], [])
    imp.want_load()
    imp.want_build(first)
    monkeypatch.chdir(root)
    assert "notes" in imp.load()                     # the child has run: it imported the app
    imp.want_build(first)                            # the same settings: fine
    with pytest.raises(LoadError, match="before the app was imported"):
        imp.build(lock.scan_pin(["."], ["scripts/evil.py"]))


# --- 2. the GitHub Action runs its own interpreter -------------------------------------------------
def _action() -> dict:
    return yaml.safe_load((REPO / "action.yml").read_text())


def test_the_action_installs_invariants_before_the_project_and_never_runs_inv_from_path():
    steps = _action()["runs"]["steps"]
    names = [s["name"] for s in steps]
    assert names.index("set up the invariants gate") < names.index("install the project's dependencies") \
        < names.index("inv check")
    gate, check = steps[0]["run"], steps[-1]["run"]
    assert "-m venv" in gate and "GITHUB_ACTION_PATH" in gate and "invariants-py" in gate
    assert '"$gate_py" -I -m invariants.cli check' in check
    assert not any(line.strip().startswith("inv ") for s in steps for line in s["run"].splitlines())
    assert "-I -S -c" in check and "--site" in check            # no .pth of the project runs
    assert steps[-1]["env"]["BASH_ENV"] == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_action_scripts_parse(tmp_path):
    for i, step in enumerate(_action()["runs"]["steps"]):
        script = tmp_path / f"step{i}.sh"
        script.write_text(step["run"])
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, (step["name"], r.stderr)


def test_site_dirs_reach_the_child_after_its_own_path(tmp_path):
    """--site: the dependencies' site-packages are importable by the app, after inv's own path, and
    the deciding process never puts them on its own sys.path."""
    site = write_project(tmp_path / "site", {"r29dep.py": "VALUE = 7\n"})
    root = write_project(tmp_path / "p", {
        "r29s/__init__.py": "", "invariants.yaml": RULES.split("  - id: DOOR")[0].replace("r29app.pay", "r29s.pay"),
        "r29s/pay.py": "import r29dep\n" + FILES["r29app/pay.py"]})
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "r29dep" in r.stdout + r.stderr, r.stdout + r.stderr
    r = run_inv("lock", "--site", site, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_inv("check", "--site", site, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    from invariants.cli import common, worker
    common.SITE[:] = [str(site)]
    try:
        got = worker._child_path()
        assert got[-1] == str(site) and str(site) not in sys.path
    finally:
        common.SITE.clear()


# --- 3. python -m invariants.cli does not import the project's own yaml.py ------------------------
SHADOW = '''\
import os, sys
with open(os.environ["R29_SHADOW_LOG"], "a") as f:
    f.write(f"{__name__} {sys.argv[0]}\\n")
sys.modules.pop(__name__)
here = os.path.dirname(os.path.abspath(__file__))
saved = list(sys.path)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
import importlib
real = importlib.import_module(__name__)
sys.path[:] = saved
sys.modules[__name__] = real
'''


@pytest.mark.parametrize("module", ["yaml", "tomllib", "jsonschema", "json"])
def test_a_project_module_does_not_shadow_the_deciding_process(tmp_path, module):
    if module == "jsonschema":
        pytest.importorskip("jsonschema")
    root = write_project(tmp_path / "p", {**FILES, f"{module}.py": SHADOW})
    log = tmp_path / "shadow.log"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "R29_SHADOW_LOG": str(log)}
    for args in (["validate"], ["check"], ["scan"]):
        r = subprocess.run([sys.executable, "-m", "invariants.cli", *args], cwd=root, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode in (0, 1, 3), r.stdout + r.stderr
    lines = log.read_text().splitlines() if log.exists() else []
    # the child that imports the app (python -I -c) may import it, as it imports the app; the parent never
    assert all(line.endswith(" -c") for line in lines), lines


def test_the_cwd_is_kept_when_invariants_is_imported_by_an_app(tmp_path):
    r = subprocess.run([sys.executable, "-c", "import sys, invariants; print(repr(sys.path[0]))"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "''", r.stdout + r.stderr


def test_the_mcp_server_runs_inv_with_safe_path(tmp_path, monkeypatch):
    from invariants.mcp import tools
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    tools._inv(tmp_path, tmp_path, tmp_path / "invariants.yaml", "check")
    assert seen and seen[0][1:4] == ["-P", "-m", "invariants.cli"], seen


# --- 4. control characters from the tree are escaped ---------------------------------------------
def test_plain_and_safe_output():
    from invariants.scan.text import plain, safe_output
    assert plain("a\n::error::x\r\x1b[2J") == "a\\n::error::x\\r\\x1b[2J"
    assert safe_output("ok\n::error::pwn\n   ::warning::x\nfine :: here") == \
        "ok\n\\::error::pwn\n   \\::warning::x\nfine :: here"
    assert safe_output("a\rb\x1b[31m\tc") == "a\\rb\\x1b[31m\tc"


@pytest.mark.skipif(sys.platform == "win32", reason="a newline in a file name")
def test_a_file_name_cannot_inject_a_workflow_command(tmp_path):
    bad = "r29app/x\n::error file=a::pwned.py"
    root = write_project(tmp_path / "p", {**FILES, bad: BACK_DOOR})
    assert run_inv("lock", cwd=root).returncode == 0
    for args in (["scan"], ["check"]):
        r = run_inv(*args, cwd=root)
        assert r.returncode != 0, r.stdout
        out = r.stdout + r.stderr
        assert "x\\n::error file=a::pwned.py" in out, out
        assert not any(line.lstrip().startswith("::") for line in out.splitlines()), out
        assert "\r" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="a newline in a file name")
def test_an_error_naming_a_file_cannot_inject_a_workflow_command(tmp_path):
    root = write_project(tmp_path / "p", {"x\n::error::pwned.yaml": "rules: [1]\n"})
    r = run_inv("validate", "x\n::error::pwned.yaml", cwd=root)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "pwned" in out, out
    assert not any(line.lstrip().startswith("::") for line in out.splitlines()), out


# --- 5. the agent hook ---------------------------------------------------------------------------
def _hook(root: pathlib.Path, command: str) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=root, timeout=60)


def _hook_project(tmp_path: pathlib.Path) -> pathlib.Path:
    return write_project(tmp_path / "p", {lock.LOCK_NAME: "{}\n", "pyproject.toml": "[project]\n", "app.py": "x = 1\n",
                                          "fix.diff": "--- a/invariants.lock\n+++ b/invariants.lock\n",
                                          "ok.diff": "--- a/app.py\n+++ b/app.py\n"})


@pytest.mark.parametrize("command,blocked", [
    ("patch -p1 < fix.diff", True), ("patch -i fix.diff", True), ("patch -p1 -ifix.diff", True),
    ("patch --input=fix.diff -p1", True), ("patch -p 1 --input fix.diff", True), ("busybox patch -p1 -i fix.diff", True),
    ("gpatch -p1 < fix.diff", True), ("cat fix.diff | patch -p1", True), ("patch app.py fix.diff", True),
    ("sudo patch -p1 < fix.diff", True), ("patch -d . -p1 < fix.diff", True),
    ("patch -p1 < ok.diff", False), ("patch -i ok.diff", False), ("patch --dry-run -p1 < fix.diff", False),
    ("patch app.py ok.diff", False),
])
def test_hook_sees_patch(tmp_path, command, blocked):
    root = _hook_project(tmp_path)
    r = _hook(root, command)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


@needs_git
@pytest.mark.parametrize("changed,blocked", [(lock.LOCK_NAME, True), ("pyproject.toml", True), ("app.py", False)])
def test_hook_sees_git_moving_the_tree_to_another_commit(tmp_path, changed, blocked):
    root = _hook_project(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "other")
    (root / changed).write_text((root / changed).read_text() + "\n# changed\n")
    _git(root, "commit", "-qam", "other")
    _git(root, "checkout", "-q", "main")
    for command in ("git merge other", "git merge --no-ff -m msg other", "git cherry-pick other",
                    "git cherry-pick main..other", "git reset --hard other", "git checkout other",
                    "git switch other", "git checkout -b new other", "git switch -c new other"):
        r = _hook(root, command)
        assert r.returncode == (2 if blocked else 0), (command, r.stderr)
    for command in ("git reset --soft other", "git checkout -b new", "git switch -c new", "git merge --abort",
                    "git checkout main"):
        r = _hook(root, command)
        assert r.returncode == 0, (command, r.stderr)


@pytest.mark.parametrize("command,blocked", [
    ("cp -a ../staging/ .", True), ("cp -R ../staging/. .", True), ("rsync -a ../staging/ ./", True),
    ("ditto ../staging .", True), ("cp -a ../staging ../elsewhere", False), ("cp -a ../plain/ .", False),
    ("cp ../staging/app.py .", False),
])
def test_hook_sees_a_recursive_copy_of_a_directory_holding_the_lock(tmp_path, command, blocked):
    root = _hook_project(tmp_path)
    write_project(tmp_path / "staging", {lock.LOCK_NAME: "{}\n", "app.py": "x = 2\n"})
    write_project(tmp_path / "plain", {"app.py": "x = 2\n"})
    (tmp_path / "elsewhere").mkdir()
    r = _hook(root, command)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)
