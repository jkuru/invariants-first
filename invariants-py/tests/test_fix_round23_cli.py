"""Fix round 23 (first run): what a new user meets in the first minutes. `inv --version` (1); bare `inv`
lists every command (2); every option a user reaches for has help, and --path talks about the user's
project (3); `inv hook stop invariants.yaml` works as well as the old order (4); the GitHub Action runs
on bash 3.2 with no arguments (5); a passing verify says which modules it checked, never that every
guard names a rule (6); `inv init` says what to do after a failed lock step, refuses an empty
directory, and its header says whether it locked (7); the no-door message states the rule doors.py
applies (8); an error raised by the app's own inv.load() while `inv` imports it says so (9); the
jsonschema note shows on a passing check too (10)."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml

import invariants
from cli_support import run_inv, write_project
from invariants import cli, doors
from invariants.cli import commands

REPO = pathlib.Path(__file__).resolve().parents[2]


# --- 1. --version ----------------------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_the_package_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out.strip() == f"inv (invariants) {invariants.__version__}"


# --- 2. bare inv -----------------------------------------------------------------------------------
def test_bare_inv_lists_every_command(capsys):
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    line = next(x for x in err.splitlines() if "choose a command" in x)
    assert line == "inv: choose a command: " + ", ".join(c[0] for c in cli._COMMANDS)
    assert "hook" in line and "mcp" in line


# --- 3. help text ----------------------------------------------------------------------------------
def _sub(name: str) -> argparse.ArgumentParser:
    sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices[name]


@pytest.mark.parametrize("command, option", [("check", "--import"), ("status", "--root"), ("status", "--exclude"),
                                             ("status", "--import"), ("status", "--json"), ("hook", "event")])
def test_options_have_help(command, option):
    act = next(a for a in _sub(command)._actions if option in a.option_strings or a.dest == option)
    assert act.help, (command, option)


def test_path_help_talks_about_the_users_project():
    act = next(a for a in _sub("check")._actions if "--path" in a.option_strings)
    assert "this checkout" not in act.help and "your project" in act.help and "your app" in act.help


# --- 4. hook event in either order -----------------------------------------------------------------
@pytest.mark.parametrize("argv, rulefile, event", [
    (["hook", "stop", "rules.yaml"], "rules.yaml", "stop"),
    (["hook", "rules.yaml", "stop"], "rules.yaml", "stop"),
    (["hook", "pretool", "rules.yaml"], "rules.yaml", "pretool"),
    (["hook", "pretool"], None, "pretool"),
])
def test_hook_takes_the_event_before_or_after_the_rule_file(argv, rulefile, event):
    a = cli.parser().parse_args(argv)
    assert (a.rulefile, a.event) == (rulefile, event)


def test_hook_still_refuses_an_unknown_event(capsys):
    assert cli.main(["hook", "a.yaml", "b.yaml"]) == 2
    assert "invalid choice: 'b.yaml'" in capsys.readouterr().err


def test_hook_stop_with_the_rule_file_second_runs_check(tmp_path):
    root = write_project(tmp_path / "proj", {"invariants.yaml": "spec: invariant/v0.1\nrules: []\n"})
    assert run_inv("lock", cwd=root).returncode == 0
    r = subprocess.run([sys.executable, "-m", "invariants.cli", "hook", "stop", "invariants.yaml"], cwd=root,
                       input=json.dumps({"hook_event_name": "Stop"}), capture_output=True, text=True,
                       timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0 and "invalid choice" not in r.stderr, r.stderr


# --- 5. the action on bash 3.2 ---------------------------------------------------------------------
@pytest.mark.parametrize("bash", ["/bin/bash", "bash"])
def test_action_runs_with_no_arguments_under_set_u(tmp_path, bash):
    if not (REPO / "action.yml").exists():
        pytest.skip("action.yml lives at the monorepo root, not in the sdist")
    if not shutil.which(bash):
        pytest.skip(f"no {bash}")
    run = yaml.safe_load((REPO / "action.yml").read_text())["runs"]["steps"][-1]["run"]
    # the gate's own interpreter (round 29), standing in for `python -I -m invariants.cli`, and the
    # project's interpreter, which reports no site-packages
    gate = tmp_path / "runner" / "invariants-gate" / "bin"
    gate.mkdir(parents=True)
    (gate / "python").write_text('#!/bin/sh\nshift 3\necho "inv $# $*"\n')
    (gate / "python").chmod(0o755)
    (tmp_path / "project-python").write_text("#!/bin/sh\n")
    (tmp_path / "project-python").chmod(0o755)
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path / "runner"), "INV_PYTHON": str(tmp_path / "project-python"),
           "INV_PATH": "", "INV_BASE": "", "INV_RULE_FILE": ""}
    r = subprocess.run([shutil.which(bash), "-c", run], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "inv 1 check", r.stderr
    r = subprocess.run([shutil.which(bash), "-c", run], cwd=tmp_path, capture_output=True, text=True,
                       env={**env, "INV_RULE_FILE": "my rules.yaml"})
    assert r.returncode == 0 and r.stdout.strip() == "inv 2 check my rules.yaml", r.stderr


# --- 6. verify says what it checked ----------------------------------------------------------------
APP6 = {"app6/__init__.py": "",
        "app6/svc.py": "import invariants as inv\n\n\n@inv.guard('A-1')\ndef pay(amount: int) -> int:\n"
                       "    return amount\n",
        # a guard naming a rule the file does not have, in a module nothing imports
        "app6/other.py": "import invariants as inv\n\n\n@inv.guard('X')\ndef f(x):\n    return x\n",
        "invariants.yaml": "spec: invariant/v0.1\nrules:\n  - id: A-1\n    statement: positive\n"
                           "    guard: \"py:app6.svc.pay\"\n    bad: \"args.amount <= 0\"\n    tests: []\n"}


def test_a_passing_verify_names_what_it_checked_not_every_guard(tmp_path):
    root = write_project(tmp_path / "proj", APP6)
    r = run_inv("verify", cwd=root)
    assert r.returncode == 0, r.stderr
    assert "every guard names a rule" not in r.stdout
    assert "checked the guards in 2 imported module(s)" in r.stdout and "--import MODULE" in r.stdout
    r = run_inv("verify", "--import", "app6.other", cwd=root)
    assert r.returncode == 1 and "guards X, which is not in the rule file" in r.stderr
    r = run_inv("verify", "--json", cwd=root)               # the JSON form is unchanged
    assert json.loads(r.stdout) == {"passed": True, "problems": [], "targeted_rules": 1}


def test_verify_with_no_module_imported_says_no_guard_was_checked():
    assert "every guard" not in commands._checked_text([])
    assert commands._checked_text([]).startswith("no guard module was imported, so no guard was checked")
    assert "--import MODULE" in commands._checked_text([])


# --- 7. inv init recovery --------------------------------------------------------------------------
BROKEN = {"pyproject.toml": '[project]\nname = "app7"\nversion = "0"\n',
          "src/app7/__init__.py": "",
          "src/app7/models.py": "import notinstalledpkg  # noqa\nfrom pydantic import BaseModel, Field\n\n\n"
                                "class Order(BaseModel):\n    qty: int = Field(gt=0)\n"}


def test_init_comments_out_a_contract_whose_model_does_not_import(tmp_path):
    # round 24: init tries the lock's pin on each drafted contract first, so a model module that
    # imports a missing package is written commented out with the reason, and the lock is written
    pytest.importorskip("pydantic")
    root = write_project(tmp_path / "proj", BROKEN)
    r = run_inv("init", cwd=root)
    assert r.returncode == 0 and (root / "invariants.lock").exists(), r.stderr
    text = (root / "invariants.yaml").read_text()
    assert "# not written as a contract: `inv lock` cannot pin it: contract order: cannot import app7.models" in text
    assert '  # order: {type: pydantic, ref: "app7.models:Order"}' in text and "contracts: {}" in text
    assert "left 1 model(s) commented out" in r.stdout


def test_init_after_a_failed_lock_says_what_to_do_next(tmp_path, capsys):
    # the lock step failing anyway (here: a rule file whose contract does not import) still says what to do
    root = write_project(tmp_path / "proj", BROKEN)
    path = root / "invariants.yaml"
    path.write_text('spec: invariant/v0.1\ncontracts:\n  order: {type: pydantic, ref: "app7.models:Order"}\n')
    cwd = os.getcwd()
    os.chdir(root)
    try:
        code = commands._init_lock(argparse.Namespace(path=[]), pathlib.Path("invariants.yaml"))
    finally:
        os.chdir(cwd)
    err = capsys.readouterr().err
    assert code == 1 and not (root / "invariants.lock").exists()
    assert "cannot import app7.models" in err
    assert "no lock was written" in err and "then run `inv lock invariants.yaml`" in err
    assert "Do not rerun `inv init`" in err and "--no-lock" in err and "often a model module" not in err
    r = run_inv("init", cwd=root)
    assert r.returncode == 2 and "already exists" in r.stderr
    assert "no invariants.lock yet" in r.stderr and "run `inv lock invariants.yaml`" in r.stderr


def test_init_already_exists_with_a_lock_does_not_mention_lock(tmp_path):
    root = write_project(tmp_path / "proj", {"invariants.yaml": "spec: invariant/v0.1\nrules: []\n",
                                             "invariants.lock": "{}"})
    r = run_inv("init", cwd=root)
    assert r.returncode == 2 and r.stderr.strip() == (
        "inv init: invariants.yaml already exists; inv init never overwrites a rule file")


def test_init_in_a_directory_with_no_source_writes_nothing(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    r = run_inv("init", cwd=root)
    assert r.returncode == 2 and "found no Python source file" in r.stderr and "--root DIR" in r.stderr
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("no_lock, header", [
    (False, "# invariants.yaml, drafted by `inv init` and pinned in invariants.lock. Review both, then commit them."),
    (True, "# invariants.yaml, drafted by `inv init`. Review it, then run `inv lock` and commit both files."),
])
def test_init_header_says_whether_it_locked(tmp_path, no_lock, header):
    root = write_project(tmp_path / "proj", {"app/__init__.py": "", "app/x.py": "def f(x):\n    return x\n"})
    r = run_inv("init", *(["--no-lock"] if no_lock else []), cwd=root)
    assert r.returncode == 0, r.stderr
    assert (root / "invariants.yaml").read_text().splitlines()[0] == header
    assert (root / "invariants.lock").exists() is not no_lock


# --- 8. the no-door message ------------------------------------------------------------------------
def test_no_door_message_states_the_rule_doors_applies():
    line = doors.summary(doors.DoorsFound([], [], {}, 0))[-1]
    assert "two or more callers go through to a write)" not in line
    assert "service function" in line and "route handlers" in line and f"{doors.MIN_CALLERS} or more" in line
    assert "Graphify" not in line
    assert "Graphify" in doors.summary(doors.DoorsFound([], [], {}, 0, graph="graphify: 3 nodes"))[-1]


# --- 9. an error from the app's own inv.load() -----------------------------------------------------
APP9 = {"pyproject.toml": '[project]\nname = "app9"\nversion = "0"\n',
        "src/app9/__init__.py": "",
        "src/app9/svc.py": "import invariants as inv\n\n\n@inv.guard('A-1')\ndef pay(amount: int) -> int:\n"
                           "    return amount\n\n\ninv.load('invariants.yaml')\n",
        "invariants.yaml": APP6["invariants.yaml"].replace("app6.svc", "app9.svc")}


def test_an_app_load_failing_on_a_relative_path_says_it_came_from_importing_the_app(tmp_path):
    root = write_project(tmp_path / "proj", APP9)
    r = run_inv("check", "proj/invariants.yaml", cwd=tmp_path)
    assert r.returncode == 1 and "FAIL  rules" in r.stdout
    assert "(raised while importing app9.svc: " in r.stdout, r.stdout
    assert 'inv.load(pathlib.Path(__file__).parent / "invariants.yaml")' in r.stdout
    r = run_inv("verify", "proj/invariants.yaml", cwd=tmp_path)
    assert r.returncode == 1 and "raised while importing app9.svc" in r.stderr, r.stderr
    assert run_inv("lock", cwd=root).returncode == 0          # from the project directory it loads


def test_an_import_error_is_not_described_twice():
    try:
        try:
            raise ImportError("No module named 'x'")
        except ImportError as e:
            raise invariants.LoadError("cannot import app.m: No module named 'x'") from e
    except invariants.LoadError as e:
        assert cli._describe(e) == "cannot import app.m: No module named 'x'"


# --- 10. the jsonschema note on a pass -------------------------------------------------------------
def test_check_shows_the_jsonschema_note_on_a_pass(tmp_path):
    block = tmp_path / "block"
    block.mkdir()
    (block / "jsonschema.py").write_text("raise ImportError('blocked for the test')\n")
    root = write_project(tmp_path / "proj", {"invariants.yaml": "spec: invariant/v0.1\nrules: []\n"})
    src = pathlib.Path(invariants.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": f"{block}{os.pathsep}{src}", "PYTHONDONTWRITEBYTECODE": "1"}
    run = [sys.executable, "-m", "invariants.cli"]
    assert subprocess.run([*run, "lock"], cwd=root, env=env, capture_output=True).returncode == 0
    r = subprocess.run([*run, "check"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout
    assert r.stdout.count("jsonschema is not installed") == 1
    assert f"  note  rules: {commands.NO_JSONSCHEMA}" in r.stdout
    r = subprocess.run([*run, "validate"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stderr.count("jsonschema is not installed") == 1
