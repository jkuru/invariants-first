"""Round 31, the gate. Each failed before its fix.

1. `inv check`'s guards step took the child's `inv verify` verdict as it was, and the child imports
   the app first: a guarded module that replaced `invariants.cli.child.cmd_verify` at import passed the
   step with the guard removed from the door. The deciding process now reads each target's decorators
   from the source itself (cli/static_guards.py), and the step passes only when that and the child both
   pass; the child also refuses to report when the check it runs was replaced.
2. `inv init --root/--exclude` wrote [tool.invariants.scan] into pyproject.toml even when a lock already
   existed beside the rule file, and the agent hook let `inv init ... --exclude` through. Init now only
   prints what to add there, and the hook blocks it under a lock.
3. A key repeated in one mapping of the rule file (YAML or JSON), or in the lock, was read as its last
   value, silently. It is refused, naming the lines.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

from cli_support import CLEAN, copy_app, run_inv, write_project
from invariants.cli.static_guards import guard_problems
from invariants.errors import LoadError
from invariants.loader import read_document

FAKE = ("try:\n    import invariants.cli.child as _c\n    _c.cmd_verify = lambda ns: (print('verify: pass'), 0)[1]\n"
        "except ImportError:\n    pass\n")


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", *args], cwd=root, check=True,
                   capture_output=True)


def _service(root: pathlib.Path) -> pathlib.Path:
    return root / "ledger" / "service.py"


# --- 1. the guards verdict is not the imported code's to give -------------------------------------
def test_check_fails_a_removed_guard_even_when_the_module_fakes_verify(tmp_path):
    root = copy_app(CLEAN, tmp_path / "app")
    _git(root, "init", "-q")
    assert run_inv("lock", cwd=root).returncode == 0
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    s = _service(root).read_text()
    s = s.replace('@inv.guard("L-1")\ndef withdraw', "def withdraw", 1)
    s = s.replace("from . import store\n", "from . import store\n" + FAKE, 1)
    _service(root).write_text(s)
    r = run_inv("check", "--base", "HEAD", cwd=root)
    assert r.returncode != 0, r.stdout
    assert "FAIL  guards" in r.stdout and "py:ledger.service.withdraw is not decorated with @inv.guard('L-1')" \
        in r.stdout, r.stdout


def test_the_child_refuses_when_the_app_replaced_its_verify(tmp_path):
    """The guard is in place (the source check passes); the app replacing verify still fails the step."""
    root = copy_app(CLEAN, tmp_path / "app")
    s = _service(root).read_text().replace("from . import store\n", "from . import store\n" + FAKE, 1)
    _service(root).write_text(s)
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode != 0 and "replaced the guard check" in r.stdout, r.stdout


def test_the_child_refuses_when_the_app_replaced_a_decorator(tmp_path):
    root = copy_app(CLEAN, tmp_path / "app")
    s = _service(root).read_text().replace("from . import store\n", "from . import store\n"
                                            "import sys\nsys.modules['invariants.guard.decorators'].edge = None\n", 1)
    _service(root).write_text(s)
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode != 0 and "replaced the guard check" in r.stdout, r.stdout


def test_the_clean_fixture_passes_both(tmp_path):
    root = copy_app(CLEAN, tmp_path / "app")
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode == 0 and "ok    guards" in r.stdout, r.stdout


RULES = """\
spec: invariant/v0.1
rules:
  - id: R-1
    status: detect
    subject: {{target: "{target}"}}
    bad: "false"
    enforcers: [{{point: {point}, timing: pre_effect, mode: detect}}]
"""


def _static(tmp_path: pathlib.Path, files: dict[str, str], target: str, point: str = "decorator") -> list[str]:
    root = write_project(tmp_path / "p", {k: textwrap.dedent(v) for k, v in files.items()})
    (root / "invariants.yaml").write_text(RULES.format(target=target, point=point))
    return guard_problems(root / "invariants.yaml", read_document(root / "invariants.yaml"), [])[1]


GUARDED = "import invariants as inv\n\n@inv.guard('R-1')\ndef f(x):\n    return x\n"


@pytest.mark.parametrize("files,target", [
    ({"app/__init__.py": "", "app/m.py": GUARDED}, "py:app.m.f"),
    ({"app/__init__.py": "", "app/m.py": "from invariants import guard as g\n@g('R-1')\ndef f(x): ...\n"},
     "py:app.m.f"),
    ({"app/__init__.py": "", "app/m.py": "import invariants.guard as ig\n@ig.guard('py:app.m.f')\ndef f(x): ...\n"},
     "py:app.m.f"),
    ({"app/__init__.py": "from .m import f\n", "app/m.py": GUARDED}, "py:app.f"),
    ({"app/__init__.py": "", "app/m.py": "import invariants as inv\nclass C:\n    @inv.guard('R-1')\n"
      "    @staticmethod\n    def f(x): ...\n"}, "py:app.m.C.f"),
    ({"app/__init__.py": "", "app/m.py": "from invariants.integrations import fastapi as fa\n"
      "@fa.guard('R-1')\nasync def f(x): ...\n"}, "py:app.m.f"),
], ids=["inv", "from-import-alias", "submodule-alias", "re-export", "method", "fastapi"])
def test_the_source_check_reads_the_import_forms(tmp_path, files, target):
    assert _static(tmp_path, files, target) == []


@pytest.mark.parametrize("files,target,why", [
    ({"app/__init__.py": "", "app/m.py": "def f(x): ...\n"}, "py:app.m.f", "is not decorated with @inv.guard"),
    ({"app/__init__.py": "", "app/m.py": GUARDED + "f = print\n"}, "py:app.m.f", "is bound 2 times"),
    ({"app/__init__.py": "", "app/m.py": "import invariants as inv\nK = 'R-1'\n@inv.guard(K)\ndef f(x): ...\n"},
     "py:app.m.f", "not literal strings"),
    ({"app/__init__.py": "", "app/m.py": "import invariants as inv\nif True:\n    @inv.guard('R-1')\n"
      "    def f(x): ...\n"}, "py:app.m.f", "inside a block"),
    ({"app/__init__.py": "", "app/m.py": GUARDED + "inv = None\n"}, "py:app.m.f", "is not decorated"),
    ({"app/__init__.py": "", "app/m.py": "import invariants as inv\n@inv.edge('R-1')\ndef f(x): ...\n"},
     "py:app.m.f", "is not decorated with @inv.guard"),
    ({"app/__init__.py": "", "app/m.py": GUARDED}, "py:app.nothere.f", "cannot be checked from its source"),
    ({"app/__init__.py": "", "app/m.py": GUARDED}, "py:app.m.g", "cannot be checked from its source"),
], ids=["no-guard", "rebound", "expression-key", "in-a-branch", "alias-rebound", "wrong-kind", "no-module",
        "no-name"])
def test_the_source_check_fails_closed(tmp_path, files, target, why):
    problems = _static(tmp_path, files, target)
    assert problems and why in problems[0], problems


def test_the_source_check_reads_the_points(tmp_path):
    files = {"app/__init__.py": "", "app/m.py": GUARDED}
    assert "does not evaluate point middleware" in _static(tmp_path, files, "py:app.m.f", "middleware")[0]
    files = {"app/__init__.py": "", "app/m.py": GUARDED.replace("'R-1')", "'R-1', points=('middleware',))")}
    assert _static(tmp_path / "b", files, "py:app.m.f", "middleware") == []


def test_the_source_check_reads_edges_and_model_calls(tmp_path):
    root = write_project(tmp_path / "p", {"app/__init__.py": "", "app/m.py": (
        "import invariants as inv\n@inv.model_call('R-1', schema=S, returns=C)\ndef f(x): ...\n"
        "def g(c): ...\n")})
    (root / "invariants.yaml").write_text(
        "spec: invariant/v0.1\nrules:\n  - id: R-1\n    status: detect\n    subject: {target: 'py:app.m.f'}\n"
        "    bad: 'false'\n    model_policy: {schema: S}\n"
        "    enforcers: [{point: edge, target: 'py:app.m.g', timing: pre_effect, mode: detect}]\n")
    n, problems = guard_problems(root / "invariants.yaml", read_document(root / "invariants.yaml"), [])
    assert n == 2 and len(problems) == 1 and "py:app.m.g is not decorated with @inv.edge('R-1')" in problems[0]


# --- 2. init leaves the scan settings of a locked project alone; the hook blocks it ------------------
def test_init_does_not_write_scan_settings_beside_a_lock(tmp_path):
    root = write_project(tmp_path / "p", {"app/__init__.py": "", "app/service.py": "def f():\n    pass\n",
                                          "pyproject.toml": "[project]\nname = 'p'\n", "invariants.lock": "{}\n"})
    r = run_inv("init", "alt.yaml", "--exclude", "app/service.py", "--no-lock", cwd=root)
    assert (root / "pyproject.toml").read_text() == "[project]\nname = 'p'\n", r.stdout + r.stderr
    assert "add extend_exclude" in r.stdout + r.stderr


def _pretool(root: pathlib.Path, command: str) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command}}
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "pretool"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=root, timeout=60)


@pytest.mark.parametrize("command", ["inv init alt.yaml --exclude app/service.py --no-lock",
                                     "inv init --root=src", "python -m invariants.cli init x.yaml --exclude a",
                                     "cd . && env A=1 inv init x.yaml --root src"])
def test_hook_blocks_init_with_scan_flags_under_a_lock(tmp_path, command):
    root = write_project(tmp_path / "p", {"invariants.lock": "{}\n"})
    r = _pretool(root, command)
    assert r.returncode == 2 and "inv init" in r.stderr, r.stderr
    free = write_project(tmp_path / "free", {"x.txt": ""})        # no lock: init may write its settings
    assert _pretool(free, command).returncode == 0, command


def test_hook_lets_init_without_scan_flags_through(tmp_path):
    root = write_project(tmp_path / "p", {"invariants.lock": "{}\n"})
    assert _pretool(root, "inv init alt.yaml --no-lock").returncode == 0


# --- 3. repeated keys ------------------------------------------------------------------------------
def test_a_repeated_yaml_key_is_refused_naming_both_lines(tmp_path):
    p = tmp_path / "invariants.yaml"
    p.write_text("spec: invariant/v0.1\nrules:\n  - id: L\n    sink:\n      py:a.s: py:a.d1\n      py:a.s: py:a.d2\n")
    with pytest.raises(LoadError, match=r"invariants.yaml:6: the key 'py:a.s' appears twice .*lines 5 and 6"):
        read_document(p)


def test_a_repeated_json_key_is_refused(tmp_path):
    p = tmp_path / "invariants.json"
    p.write_text('{"spec": "invariant/v0.1",\n "rules": [{"id": "L",\n  "door": "py:a.x",\n  "door": "py:a.y"}]}\n')
    with pytest.raises(LoadError, match=r"the key 'door' appears twice in one object \(lines 3, 4\)"):
        read_document(p)


def test_a_repeated_lock_key_is_refused(tmp_path):
    root = copy_app(CLEAN, tmp_path / "app")
    assert run_inv("lock", cwd=root).returncode == 0
    lock = root / "invariants.lock"
    text = lock.read_text()
    lock.write_text(text.replace("{", '{"rules": {},', 1))
    r = run_inv("lock", "--check", cwd=root)
    assert r.returncode != 0 and "appears twice" in r.stdout + r.stderr, r.stdout + r.stderr
