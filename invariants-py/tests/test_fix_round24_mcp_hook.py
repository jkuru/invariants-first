"""`inv mcp` and the agent hook, after the round-24 review.

MCP (1): a rule file inside --root naming `py:os.environ` echoed the server's whole environment in the
lock step's error, and any importable module outside --root was imported (`py:this` printed the Zen).
Errors about a resolved object now name its type, never its repr; under `inv mcp` (INV_CONFINE_ROOT) a
target, contract or provider module outside --root is refused before it is imported.

Hook (2): `inv hook pretool` let through a command that rewrites the lock or pyproject.toml without
naming it (a project script, `git apply`, `git stash pop`, `git checkout REF -- .`, `git restore
--source`), and an edit of the hooks' own configuration (.claude/settings.json)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from cli_support import run_inv, write_project
from invariants import lock

SECRET = "sk-live-PLANTED-r24-7d1f"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _rules(target: str, contracts: str = "") -> str:
    return (f"spec: invariant/v0.1\n{contracts}rules:\n  - id: X-1\n    status: candidate\n"
            f"    subject: {{target: \"{target}\"}}\n    bad: \"false\"\n"
            f"    enforcers: [{{point: decorator, timing: pre_effect, mode: detect}}]\n")


CFG = f'''\
class Holder:
    """An object whose repr is a secret (a settings object, a client holding a key)."""
    def __repr__(self):
        return "Holder({SECRET!r})"

    __str__ = __repr__


HOLDER = Holder()


def f(x: int) -> int:
    return x
'''


def _mcp_check(proj: pathlib.Path) -> str:
    from invariants.mcp import tools
    from invariants.mcp.confine import ToolError
    try:
        return json.dumps(tools.tool_check(proj.resolve(), None, {}))
    except ToolError as e:
        return str(e)


def _mcp_status(proj: pathlib.Path) -> str:
    from invariants.mcp import tools
    from invariants.mcp.confine import ToolError
    try:
        return json.dumps(tools.tool_status(proj.resolve(), None, {}))
    except ToolError as e:
        return str(e)


# --- 1a. never an object's repr in an error ----------------------------------------------------------
@pytest.mark.parametrize("tool", ["check", "status"])
def test_mcp_never_echoes_the_environment_of_an_environ_target(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("MY_SECRET", SECRET)
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:os.environ")})
    out = _mcp_check(proj) if tool == "check" else _mcp_status(proj)
    assert SECRET not in out and "MY_SECRET" not in out
    if tool == "check":
        assert "module os is not a file inside" in out and "confines imports" in out, out


def test_plain_check_names_the_type_of_a_target_without_a_signature(tmp_path):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:os.environ")})
    env = {**os.environ, "MY_SECRET": SECRET}
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "check"], cwd=proj, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert SECRET not in r.stdout + r.stderr and "MY_SECRET" not in r.stdout + r.stderr
    assert "target py:os.environ: no signature (a _Environ object" in r.stdout, r.stdout


@pytest.mark.parametrize("confine", [False, True])
def test_a_project_object_with_a_secret_repr_never_reaches_the_output(tmp_path, confine):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:cfg.HOLDER"), "cfg.py": CFG})
    out = _mcp_check(proj) if confine else (lambda r: r.stdout + r.stderr)(run_inv("check", cwd=proj))
    assert SECRET not in out, out
    assert "target py:cfg.HOLDER: no signature (a Holder object" in out, out


@pytest.mark.parametrize("confine", [False, True])
def test_a_python_contract_with_a_secret_repr_never_reaches_the_output(tmp_path, confine):
    proj = write_project(tmp_path / "proj", {
        "invariants.yaml": _rules("py:cfg.f", "contracts:\n  c: {type: python, ref: 'cfg:HOLDER'}\n"),
        "cfg.py": CFG})
    for out in ((lambda r: r.stdout + r.stderr)(run_inv("lock", cwd=proj)),
                _mcp_check(proj) if confine else (lambda r: r.stdout + r.stderr)(run_inv("check", cwd=proj))):
        assert SECRET not in out, out
        assert "contract c: cannot pin cfg:HOLDER: a Holder object has no source" in out, out


def test_a_signature_pin_error_names_the_type_only():
    from invariants.errors import LoadError
    from invariants.lock.pin_signatures import _signature_pin
    from invariants.lock.pin_types import Schemas
    ns: dict = {}
    exec(CFG, ns)
    with pytest.raises(LoadError) as e:
        _signature_pin(ns["HOLDER"], "target py:cfg.HOLDER", Schemas())
    assert SECRET not in str(e.value) and "a Holder object" in str(e.value)


def test_a_provider_pin_error_names_the_exception_type_only(monkeypatch):
    from invariants.errors import LoadError
    from invariants.lock import pinning
    from invariants.lock.pin_types import Schemas

    def boom(*a, **k):
        raise ValueError(f"<object {SECRET}>")
    monkeypatch.setattr(pinning, "resolve", lambda src: object())
    monkeypatch.setattr(pinning, "_signature_pin", boom)
    with pytest.raises(LoadError) as e:
        pinning._provider_pin("p", {"type": "int", "from": "py:cfg.p", "args": {}}, Schemas())
    assert SECRET not in str(e.value) and "(ValueError)" in str(e.value)

    def load_error(*a, **k):
        raise LoadError("provider p: no signature (a Holder object, not a function or class)")
    monkeypatch.setattr(pinning, "_signature_pin", load_error)
    with pytest.raises(LoadError, match="a Holder object"):
        pinning._provider_pin("p", {"type": "int", "from": "py:cfg.p", "args": {}}, Schemas())


def test_params_of_a_callable_without_a_signature_is_empty_not_its_repr(monkeypatch):
    import inspect
    from invariants.loader import schemas

    def no_signature(obj, *a, **k):
        raise ValueError(f"no signature found for {obj!r}")
    ns: dict = {}
    exec(CFG, ns)
    monkeypatch.setattr(inspect, "signature", no_signature)
    assert schemas._params(ns["f"]) == set()


def test_a_schema_instance_error_names_its_class_not_its_repr():
    import dataclasses
    from invariants.loader.schemas import schema_fields

    @dataclasses.dataclass
    class Settings:
        key: "Undefined"  # noqa: F821  (an annotation that does not resolve)

        def __repr__(self):
            return f"Settings({SECRET})"
    with pytest.raises(TypeError) as e:
        schema_fields(Settings(key=1))
    assert SECRET not in str(e.value) and "Settings" in str(e.value)


# --- 1b. under inv mcp, no module outside --root is imported -------------------------------------------
def test_mcp_does_not_import_a_stdlib_module_a_target_names(tmp_path):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:this.s")})
    out = _mcp_check(proj)
    assert "Beautiful is better than ugly" not in out and "Gur Mra" not in out
    assert "module this is not a file inside" in out, out


def test_plain_check_still_imports_a_module_outside_the_project(tmp_path):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:json.dumps")})
    r = run_inv("check", cwd=proj)
    assert "confines imports" not in r.stdout + r.stderr and "rules: 1 rule(s)" in r.stdout, r.stdout


def test_mcp_does_not_import_an_installed_package_or_an_outside_contract_module(tmp_path):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:invariants.load")})
    assert "module invariants is not a file inside" in _mcp_check(proj)
    proj2 = write_project(tmp_path / "proj2", {
        "invariants.yaml": _rules("py:cfg.f", "contracts:\n  c: {type: python, ref: 'json:dumps'}\n"),
        "cfg.py": CFG})
    out = _mcp_check(proj2)
    assert "module json is not a file inside" in out, out


def test_the_lock_does_not_import_an_outside_contract_module_under_confinement(tmp_path, monkeypatch):
    from invariants.confinement import CONFINE_ENV
    from invariants.errors import LoadError
    from invariants.lock.pin_contracts import _python_ref
    monkeypatch.setenv(CONFINE_ENV, str(tmp_path))
    with pytest.raises(LoadError, match="contract c: module json is not a file inside"):
        _python_ref("c", "json:dumps")
    monkeypatch.delenv(CONFINE_ENV)
    assert _python_ref("c", "json:dumps")(1) == "1"


def test_mcp_does_not_import_a_module_beside_its_root(tmp_path):
    marker = tmp_path / "imported.txt"
    (tmp_path / "evil.py").write_text(f"open({str(marker)!r}, 'w').close()\ndef f(x: int) -> int:\n    return x\n")
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:evil.f")})
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")])}
    from invariants.confinement import CONFINE_ENV
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "check"], cwd=proj,
                       env={**env, CONFINE_ENV: str(proj.resolve())}, capture_output=True, text=True, timeout=60)
    assert not marker.exists() and "module evil is not a file inside" in r.stdout, r.stdout
    subprocess.run([sys.executable, "-m", "invariants.cli", "check"], cwd=proj, env=env,
                   capture_output=True, text=True, timeout=60)
    assert marker.exists()                                           # plain inv: unchanged


def test_mcp_still_checks_the_project_s_own_modules(tmp_path):
    proj = write_project(tmp_path / "proj", {"invariants.yaml": _rules("py:pkg.mod.f"), "pkg/__init__.py": "",
                                             "pkg/mod.py": CFG})
    assert run_inv("lock", cwd=proj).returncode == 0
    out = _mcp_check(proj)
    steps = {s["step"]: s["result"] for s in json.loads(out)["steps"]}
    assert "confines imports" not in out and steps["rules"] == steps["lock"] == "ok", out


def test_import_module_under_confinement(tmp_path, monkeypatch):
    from invariants.confinement import CONFINE_ENV, import_module
    from invariants.errors import LoadError
    from invariants.resolve import resolve
    (tmp_path / "inside_r24.py").write_text("def f():\n    return 1\n")
    (tmp_path / "ns_r24").mkdir()
    (tmp_path / "ns_r24" / "m.py").write_text("X = 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(CONFINE_ENV, str(tmp_path))
    assert resolve("py:inside_r24.f")() == 1
    assert import_module("ns_r24.m", "t").X == 2                     # a namespace package inside
    with pytest.raises(LoadError, match="no importable module"):
        resolve("py:missing_r24.f")
    for name in ("os", "sys", "json", "invariants", "pytest"):          # frozen, built-in, stdlib, installed
        with pytest.raises(LoadError, match=f"module {name} is not a file inside"):
            import_module(name, "t")
    with pytest.raises(LoadError, match="module os is not a file inside"):
        resolve("py:os.environ")
    monkeypatch.delenv(CONFINE_ENV)
    assert import_module("json", "t").dumps(1) == "1"


# --- 2. the hook --------------------------------------------------------------------------------------
def _hook(payload: dict, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


def _bash(root: pathlib.Path, command: str) -> subprocess.CompletedProcess[str]:
    return _hook({"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}, root)


FIX_LOCK = "import pathlib\np = pathlib.Path('invariants.lock')\np.write_text(p.read_text().replace('a', 'b'))\n"
FIX_PY = "import pathlib\npathlib.Path('pyproject.toml').write_text('')\n"
FIX_NAME = "from invariants import lock\nopen(lock.LOCK_NAME, 'w').write('{}')\n"
APP = "print('hello')\n"


def _project(tmp_path: pathlib.Path) -> pathlib.Path:
    return write_project(tmp_path / "p", {lock.LOCK_NAME: "{}\n", "pyproject.toml": "[project]\n",
                                          "fix.py": FIX_LOCK, "fixpy.py": FIX_PY, "fixname.py": FIX_NAME,
                                          "app.py": APP, "tools/__init__.py": "", "tools/fix.py": FIX_LOCK,
                                          "fix.diff": "--- a/invariants.lock\n+++ b/invariants.lock\n",
                                          "ok.diff": "--- a/app.py\n+++ b/app.py\n"})


@pytest.mark.parametrize("command", [
    "python3 fix.py", "python fix.py", "python3.12 -u fix.py", "uv run python fix.py", "uv run fix.py",
    "uv run --with x python3 fix.py", "python3 -m tools.fix", "python3 - < fix.py", "python3 < fix.py",
    "./fix.py", "env X=1 python3 fix.py", "cd . && python3 fix.py", "python3 tools/fix.py",
    "python3 fixpy.py", "python3 fixname.py",
])
def test_hook_blocks_running_a_project_script_that_names_the_lock(tmp_path, command):
    r = _bash(_project(tmp_path), command)
    assert r.returncode == 2, (command, r.stderr)
    assert "without naming it" in r.stderr


@pytest.mark.parametrize("command", [
    "python3 app.py", "python3 -m pytest", "pytest -q", "uv run pytest", "python3 -c 'print(1)'",
    "python3 missing.py", "uv run app.py", "python3 -V",
])
def test_hook_allows_ordinary_python(tmp_path, command):
    r = _bash(_project(tmp_path), command)
    assert r.returncode == 0, (command, r.stderr)


def test_hook_allows_a_lock_naming_script_outside_any_locked_tree(tmp_path):
    root = write_project(tmp_path / "nolock", {"fix.py": FIX_LOCK})
    assert _bash(root, "python3 fix.py").returncode == 0


@pytest.mark.parametrize("command,blocked", [
    ("git apply fix.diff", True), ("git am fix.diff", True), ("git apply < fix.diff", True),
    ("git apply", True), ("git -C . apply fix.diff", True),
    ("git apply ok.diff", False), ("git apply --check fix.diff", False), ("git apply --stat fix.diff", False),
    ("git checkout HEAD~1 -- .", True), ("git checkout HEAD -- '*.lock'", True), ("git checkout HEAD -- '*'", True),
    ("git checkout main .", True), ("git checkout HEAD -- ':/'", True),
    ("git checkout HEAD --pathspec-from-file=list.txt", True),
    ("git restore --source HEAD~1 .", True), ("git restore --source=HEAD~1 -- .", True),
    ("git restore -s HEAD~1 .", True), ("git restore -sHEAD~1 .", True),
    ("git checkout main", False), ("git checkout -b feature", False), ("git checkout -b feature origin/main", False),
    ("git checkout HEAD -- app.py", False), ("git checkout HEAD -- '*.py'", False), ("git checkout -- .", False),
    ("git restore app.py", False), ("git restore --source HEAD~1 app.py", False), ("git restore .", False),
    ("git status", False), ("git diff HEAD~1", False), ("git log -p", False),
])
def test_hook_git_commands_that_rewrite_the_lock_from_elsewhere(tmp_path, command, blocked):
    r = _bash(_project(tmp_path), command)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


def test_hook_blocks_a_checkout_of_a_directory_holding_the_lock(tmp_path):
    proj = _project(tmp_path)
    assert _bash(tmp_path, "git checkout HEAD~1 -- p").returncode == 2
    assert _bash(tmp_path, "git checkout HEAD~1 -- p/app.py").returncode == 0
    assert _bash(proj / "tools", "git checkout HEAD~1 -- ..").returncode == 2


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


@needs_git
@pytest.mark.parametrize("changed,blocked", [(lock.LOCK_NAME, True), ("pyproject.toml", True), ("app.py", False)])
def test_hook_git_stash_pop_of_an_entry_that_changes_the_lock(tmp_path, changed, blocked):
    proj = _project(tmp_path)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    (proj / changed).write_text((proj / changed).read_text() + "\n# changed\n")
    _git(proj, "stash", "push", "-q")
    for command in ("git stash pop", "git stash apply", "git stash apply stash@{0}"):
        r = _bash(proj, command)
        assert r.returncode == (2 if blocked else 0), (command, r.stderr)
    assert _bash(proj, "git stash list").returncode == 0


@pytest.mark.parametrize("tool,name", [("Write", ".claude/settings.json"), ("Edit", ".claude/settings.local.json"),
                                       ("Write", ".CLAUDE/Settings.json"), ("MultiEdit", ".claude/settings.json")])
def test_hook_blocks_an_edit_of_its_own_configuration(tmp_path, tool, name):
    root = _project(tmp_path)
    r = _hook({"tool_name": tool, "cwd": str(root), "tool_input": {
        "file_path": name, "content": "{}", "old_string": "a", "new_string": "b"}}, root)
    assert r.returncode == 2 and ".claude/settings.json" in r.stderr, r.stderr


@pytest.mark.parametrize("command,blocked", [
    ("echo '{}' > .claude/settings.json", True), ("rm .claude/settings.local.json", True),
    ("sed -i '' 's/a/b/' .claude/settings.json", True), ("cp x.json .claude/", True), ("rm -rf .claude", True),
    ("cp x.json .claude/settings*.json", True), ("eval \"rm .claude/settings.json\"", True),
    ("cat .claude/settings.json", False), ("ls .claude", False), ("grep hook .claude/settings.local.json", False),
    ("cp x.json .claude/agents/x.md", False), ("echo $(date) > out.txt", False),
])
def test_hook_shell_writes_of_its_own_configuration(tmp_path, command, blocked):
    r = _bash(_project(tmp_path), command)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


def test_hook_allows_other_settings_files(tmp_path):
    root = _project(tmp_path)
    for name in ("config/settings.json", "settings.json", ".claude/agents/reviewer.md"):
        r = _hook({"tool_name": "Write", "cwd": str(root), "tool_input": {"file_path": name, "content": "{}"}}, root)
        assert r.returncode == 0, (name, r.stderr)


def test_the_hook_docs_say_ci_is_the_gate():
    from invariants.cli import hooks
    doc = hooks.cmd_hook.__doc__ or ""
    assert "`inv check --base` in CI is the gate" in doc and ".claude/settings.json" in doc
    agents = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "agents.md").read_text()
    assert ".claude/settings.json" in agents and "git stash pop" in agents
