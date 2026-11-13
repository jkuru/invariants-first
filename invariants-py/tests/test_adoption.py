"""The low-friction path: the short rule form, pydantic constraints pinned by the lock,
`inv init`, `inv check` (with --relock and --base) and `inv hook` for AI coding agents."""
from __future__ import annotations

import ast
import json
import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from invariants import bootstrap, lockfile
from invariants.errors import LoadError
from invariants.loader import expand_short_forms, schema_path
from cli_support import run_inv, write_project

PAYMENTS = '''\
import invariants as inv

MAX_REFUND = 10_000
CURRENCY = "usd"
retries = 3
retries = 4


def refund(charge_id: str, charged: int, refunded: int, amount: int) -> None:
    """Send a refund."""
    if amount <= 0:
        raise ValueError("refund must be positive")
    if refunded + amount > charged:
        raise ValueError("refund exceeds what is left of the charge")
    if amount > MAX_REFUND:
        raise ValueError("refund above the cap")


def pay(amount: int, currency: str) -> None:
    if currency != CURRENCY or amount > retries:
        raise ValueError("bad payment")


def apply_credit(account: str, credit: int, limit: int) -> int:
    assert credit >= 0, "credit is never negative"
    assert credit <= limit
    return credit


def lookup(name: str, table: dict) -> int:
    if name not in table:
        raise KeyError(name)
    return table[name]
'''
MODELS = '''\
from typing import Literal
from pydantic import BaseModel, Field


class RefundRequest(BaseModel):
    charge_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0, le=10_000)
    currency: Literal["usd", "eur"]


class Plain(BaseModel):
    text: str
'''
SQL = "CREATE TABLE refunds (id TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK (amount > 0));\n"


def _project(tmp_path: pathlib.Path, pkg: str) -> pathlib.Path:
    root = tmp_path / "proj"
    write_project(root, {
        "pyproject.toml": f'[project]\nname = "{pkg}"\nversion = "0"\n[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        f"src/{pkg}/__init__.py": "",
        f"src/{pkg}/payments.py": PAYMENTS,
        f"src/{pkg}/models.py": MODELS,
        "schema.sql": SQL,
        "tests/test_x.py": "def test_x():\n    assert 1 > 0\n",
    })
    return root


def _edit(p: pathlib.Path, old: str, new: str) -> None:
    t = p.read_text()
    assert old in t, old
    p.write_text(t.replace(old, new, 1))


# --- the short form --------------------------------------------------------------------------------
def _short(**extra):
    return {"rules": [{"id": "P-1", "guard": "py:m.f", "bad": "args.x > 1", **extra}]}


def test_short_form_expands_to_the_full_form():
    doc = expand_short_forms(_short())
    r = doc["rules"][0]
    assert r["subject"] == {"target": "py:m.f"}
    assert r["enforcers"] == [{"point": "decorator", "timing": "pre_effect", "mode": "enforce"}]
    assert r["status"] == "detect" and "guard" not in r and "falsified_by" not in r
    r = expand_short_forms(_short(tests="t.py::test_a"))["rules"][0]
    assert r["status"] == "enforce" and r["falsified_by"] == ["t.py::test_a"] and "tests" not in r
    r = expand_short_forms(_short(tests=["t.py::a"], status="candidate"))["rules"][0]
    assert r["status"] == "candidate"                       # an explicit status is kept


@pytest.mark.parametrize("extra,msg", [
    ({"subject": {"target": "py:m.g"}}, "not both"),
    ({"enforcers": []}, "drop enforcers"),
    ({"tests": ["a"], "falsified_by": ["b"]}, "not both"),
    ({"tests": [3]}, "pytest node id"),
    ({"guard": "m.f"}, "py:module.function"),
    ({"subject": "x"}, "mapping"),
])
def test_short_form_mistakes_are_load_errors(extra, msg):
    with pytest.raises(LoadError, match=msg):
        expand_short_forms(_short(**extra))


def test_short_form_and_rules_null_pass_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(schema_path().read_text())
    jsonschema.validate({"rules": [{"id": "A", "guard": "py:m.f", "bad": "args.x > 1", "tests": "t::a"}]}, schema)
    jsonschema.validate({"rules": None, "contracts": {}}, schema)
    with pytest.raises(jsonschema.ValidationError):           # the long form still needs a status
        jsonschema.validate({"rules": [{"id": "A", "bad": "x"}]}, schema)


def test_every_new_schema_field_is_classified():
    assert lockfile.policy("rule.guard") == lockfile.ANALYSED
    assert lockfile.policy("rule.tests") == lockfile.NEUTRAL


# --- pydantic constraints ------------------------------------------------------------------------
@pytest.mark.parametrize("path,old,new,weak", [
    ("pydantic.properties.amount.maximum", 10000, 100000, True),
    ("pydantic.properties.amount.maximum", 10000, 5000, False),
    ("pydantic.properties.amount.maximum", 10000, None, True),
    ("pydantic.properties.amount.maximum", None, 10, False),
    ("pydantic.properties.amount.exclusiveMinimum", 0, -1, True),
    ("pydantic.properties.amount.minimum", 0, 1, False),
    ("pydantic.properties.s.maxLength", 64, 65, True),
    ("pydantic.properties.s.minLength", 1, None, True),
    ("pydantic.properties.c.enum added", None, ["gbp"], True),
    ("pydantic.properties.c.enum removed", ["eur"], None, False),
    ("pydantic.properties.c.enum", ["eur"], ["gbp"], True),
    ("pydantic.required removed", ["amount"], None, True),
    ("pydantic.required added", None, ["amount"], False),
    ("pydantic.properties.s.pattern", "^a$", "^.*$", True),
    ("pydantic.properties.s.pattern", None, "^a$", False),
    ("pydantic.additionalProperties", False, None, True),
    ("pydantic.additionalProperties", None, False, False),
    ("pydantic.properties.amount", {"type": "integer"}, None, True),       # a field removed
    ("pydantic.properties.note", None, {"type": "string"}, False),         # a field added
    ("pydantic.properties.maximum", {"type": "integer"}, None, True),      # a field named like a keyword
    ("pydantic.properties.amount.type", "integer", "number", True),
    ("pydantic.properties.amount.default", 1, 2, False),
    ("fields.amount.maximum", 1, 2, False),                                # not a pydantic schema: a pin only
])
def test_constraint_weakening(path, old, new, weak):
    assert (lockfile.constraint_weakening(path, old, new) is not None) is weak


# --- Python -> CEL -------------------------------------------------------------------------------
@pytest.mark.parametrize("src,cel", [
    ("amount <= 0", "args.amount <= 0"),
    ("refunded + amount > charged", "args.refunded + args.amount > args.charged"),
    ("a - (b - c) > 0", "args.a - (args.b - args.c) > 0"),
    ("(a + b) * c > 0", "(args.a + args.b) * args.c > 0"),
    ("0 < a < 10", "0 < args.a && args.a < 10"),
    ("a is None or len(name) > 3", "args.a == null || size(args.name) > 3"),
    ("not (a > 1 and b)", "!(args.a > 1 && args.b)"),
    ("a > 1 and (b or c)", "args.a > 1 && (args.b || args.c)"),
    ("cur not in ('usd', 'eur')", '!(args.cur in ["usd", "eur"])'),
    ("req.amount > self.limit", "args.req.amount > self.limit"),
    ("name.startswith('x')", 'args.name.startsWith("x")'),
    ("d['k'] == -1", 'args.d["k"] == -1'),
    ("flag == True", "args.flag == true"),
])
def test_to_cel(src, cel):
    params = {"amount", "refunded", "charged", "a", "b", "c", "name", "cur", "req", "d", "flag"}
    assert bootstrap.to_cel(ast.parse(src, mode="eval").body, params) == cel


@pytest.mark.parametrize("src", ["a / 2 > 1", "a // 2 > 1", "LIMIT > a", "name in table", "isinstance(a, int)",
                                 "a ** 2 > 1", "a._secret > 1", "f(a) > 1", "a > 1e30"])
def test_to_cel_refuses_what_it_cannot_translate_faithfully(src):
    with pytest.raises(bootstrap.Unsupported):
        bootstrap.to_cel(ast.parse(src, mode="eval").body, {"a", "name", "table"})


def test_assert_is_negated_exactly():
    n = bootstrap.negate(ast.parse("credit <= limit", mode="eval").body)
    assert bootstrap.to_cel(n, {"credit", "limit"}) == "args.credit > args.limit"
    n = bootstrap.negate(ast.parse("a and b", mode="eval").body)
    assert bootstrap.to_cel(n, {"a", "b"}) == "!(args.a && args.b)"


def test_discover_finds_sqlmodel_rootmodel_and_generic_bases(tmp_path):
    src = tmp_path / "src" / "shopx"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "models.py").write_text(textwrap.dedent("""
        from typing import Generic, TypeVar
        import pydantic
        from pydantic import Field, RootModel
        from sqlmodel import SQLModel
        T = TypeVar("T")
        class OrderBase(SQLModel):
            sku: str = Field(max_length=40)
        class Order(OrderBase, table=True):
            pct: int = Field(ge=0, le=50)
        class Tags(RootModel[list[str]]):
            root: list[str] = Field(max_length=5)
        class Page(pydantic.BaseModel, Generic[T]):
            size: int = Field(le=100)
        class NotAModel:
            x: int = Field(le=1)
    """))
    found = bootstrap.discover(tmp_path, [tmp_path / "src"])
    assert sorted(m.name for m in found.models) == ["Order", "OrderBase", "Page", "Tags"]


# --- inv init, check, hook (subprocess: the fixture app never enters this process) ----------------
def test_init_drafts_a_rule_file_that_passes_check(tmp_path):
    root = _project(tmp_path, "adopt_init")
    r = run_inv("init", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "invariants.yaml").read_text()
    assert 'refund_request: {type: pydantic, ref: "adopt_init.models:RefundRequest"}' in text
    assert "plain:" not in text                                      # a model with no constraint is not pinned
    assert '{when: "args.amount > 10000", message: "refund above the cap"}' in text   # a module constant
    assert "retries is not a parameter or a module constant" in text                  # rebound: not a constant
    assert '#      - {when: "args.refunded + args.amount > args.charged"' in text
    assert '#      - {when: "args.credit > args.limit", message: "assert credit <= limit"}' in text
    assert "lookup" in text and "not translated" in text               # `name not in table` is reported, not guessed
    assert (root / "invariants.lock").is_file()
    r = run_inv("check", cwd=root)
    assert r.returncode == 0 and r.stdout.rstrip().endswith("pass"), r.stdout
    r = run_inv("init", cwd=root)
    assert r.returncode == 2 and "never overwrites" in r.stderr


def test_init_pins_api_boundaries_only(tmp_path):
    root = _project(tmp_path, "adopt_api")
    r = run_inv("init", "--no-lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SQL" not in r.stdout                                     # the schema is below the API: not read
    text = (root / "invariants.yaml").read_text()
    assert "type: sql" not in text and "schema.sql" not in text and "type: pydantic" in text
    r = run_inv("init", "--sql", cwd=tmp_path / "proj")
    assert r.returncode == 2                                         # the option is gone


@pytest.mark.parametrize("consts,src,cel", [
    ("CAP = 10\n", "x > CAP", "args.x > 10"),
    ("CAP: int = -5\n", "x < CAP", "args.x < -5"),
    ("NAME = 'a'\n", "s == NAME", 'args.s == "a"'),
    ("ON = True\n", "f == ON", "args.f == true"),
    ("NONE = None\n", "s == NONE", "args.s == null"),
    ("CURRENCIES = ('usd', 'eur')\n", "s not in CURRENCIES", '!(args.s in ["usd", "eur"])'),
    ("SIZES = [1, 2, 4]\n", "x in SIZES", "args.x in [1, 2, 4]"),
])
def test_module_constants_are_inlined(consts, src, cel):
    tree = ast.parse(consts + f"def f(x, s, f):\n    if {src}:\n        raise ValueError('no')\n")
    (g,) = bootstrap._guards("m", tree)
    assert g.clauses == [(cel, "no")]


@pytest.mark.parametrize("consts", [
    "CAP = 10\nCAP = 11\n",                                         # bound twice
    "CAP = 10\ndef bump():\n    global CAP\n    CAP += 1\n",            # rebound from a function
    "CAP = {'a': 10}\n",                                             # a dict: not a literal here
    "CAP = [[10]]\n",                                                # nested: not a literal here
    "for CAP in range(3):\n    pass\n",                                # a loop variable
    "import CAP\n",
])
def test_names_that_are_not_constants_are_not_inlined(consts):
    tree = ast.parse(consts + "def f(x):\n    if x > CAP:\n        raise ValueError('no')\n")
    g = next(g for g in bootstrap._guards("m", tree) if g.qualname == "f")
    assert g.clauses == [] and "CAP is not a parameter or a module constant" in g.skipped[0]


def test_a_suggested_rule_works_once_uncommented(tmp_path):
    root = _project(tmp_path, "adopt_rule")
    assert run_inv("init", "--no-lock", cwd=root).returncode == 0
    lines = (root / "invariants.yaml").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("#  - id: REFUND"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "#")
    lines[start:end] = [l[1:] for l in lines[start:end] if "not translated" not in l]
    (root / "invariants.yaml").write_text("\n".join(lines) + "\n")
    _edit(root / "src/adopt_rule/payments.py", "def refund(", '@inv.guard("REFUND")\ndef refund(')
    assert run_inv("lock", cwd=root).returncode == 0
    r = run_inv("check", cwd=root)
    assert r.returncode == 0 and "guards: verify: pass" in r.stdout, r.stdout
    _edit(root / "src/adopt_rule/payments.py", '@inv.guard("REFUND")\n', "")
    r = run_inv("check", cwd=root)
    assert r.returncode == 1 and "not wrapped by its guard" in r.stdout


def test_check_refuses_a_loosened_model_and_relock_takes_a_tightened_one(tmp_path):
    root = _project(tmp_path, "adopt_relock")
    assert run_inv("init", cwd=root).returncode == 0
    models = root / "src/adopt_relock/models.py"
    _edit(models, "le=10_000", "le=5_000")                             # tighter: re-pinned without a person
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 0 and "re-pinned invariants.lock" in r.stdout, r.stdout
    _edit(models, "le=5_000", "le=100_000")                            # looser: refused
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 1 and "maximum raised" in r.stdout and "WEAKENED" in r.stdout
    _edit(models, "le=100_000", "le=5_000")
    _edit(root / "schema.sql", " CHECK (amount > 0)", "")          # below the API: not a rule, not pinned
    r = run_inv("check", "--relock", cwd=root)
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_check_base_sees_a_weakening_the_change_re_pinned(tmp_path):
    root = _project(tmp_path, "adopt_base")
    assert run_inv("init", cwd=root).returncode == 0
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", *cmd], cwd=root, check=True, env=env, capture_output=True)
    _edit(root / "src/adopt_base/models.py", "le=10_000", "le=100_000")
    assert run_inv("lock", "--accept-weakening", cwd=root).returncode == 0   # the agent re-pins it itself
    assert run_inv("check", cwd=root).returncode == 0                        # the lock alone now agrees
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 1 and "since main" in r.stdout and "maximum raised" in r.stdout, r.stdout
    r = run_inv("check", "--base", "main", "--allow-weakening", cwd=root)
    assert r.returncode == 0 and "allowed by a reviewer" in r.stdout
    r = run_inv("check", "--base", "no-such-ref", cwd=root)
    assert r.returncode == 1 and "base ref no-such-ref not found" in r.stdout   # never fails open


def _hook(event: str, payload: dict, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    import sys
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", event], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


@pytest.mark.parametrize("command,blocked", [
    ("inv lock --accept-weakening", True), ("INV_ALLOW_WEAKENING=1 inv check --base main", True),
    ("inv check --base main --allow-weakening", True), ("inv lock", False), ("pytest -q", False)])
def test_hook_pretool_blocks_accepting_a_weakening(tmp_path, command, blocked):
    r = _hook("pretool", {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    assert r.returncode == (2 if blocked else 0)
    assert ("a person's decision" in r.stderr) is blocked


@pytest.mark.parametrize("tool,tool_input,blocked", [
    ("Edit", {"file_path": "/repo/invariants.lock", "old_string": "a", "new_string": "b"}, True),
    ("Write", {"file_path": "invariants.lock", "content": "{}"}, True),
    ("MultiEdit", {"file_path": "sub/invariants.lock", "edits": []}, True),
    ("Edit", {"file_path": "/repo/invariants.yaml", "old_string": "a", "new_string": "b"}, False),
    ("Edit", {"file_path": "/repo/invariants.lock.md"}, False),
    ("Bash", {"command": "echo {} > invariants.lock"}, True),
    ("Bash", {"command": "sed -i '' s/a/b/ invariants.lock"}, True),
    ("Bash", {"command": "cp /tmp/x invariants.lock"}, True),
    ("Bash", {"command": "git rm invariants.lock && rm invariants.lock"}, True),
    ("Bash", {"command": "cat invariants.lock | jq ."}, False),
    ("Bash", {"command": "git diff invariants.lock"}, False),
    ("Bash", {"command": "inv lock && git add invariants.lock"}, False),
])
def test_hook_pretool_blocks_direct_lock_edits(tmp_path, tool, tool_input, blocked):
    r = _hook("pretool", {"tool_name": tool, "tool_input": tool_input}, tmp_path)
    assert r.returncode == (2 if blocked else 0), r.stderr
    assert ("direct edit of invariants.lock" in r.stderr) is blocked


def test_hook_stop_sends_the_agent_back_once(tmp_path):
    root = _project(tmp_path, "adopt_hook")
    assert run_inv("init", cwd=root).returncode == 0
    assert _hook("stop", {"cwd": str(root)}, root).returncode == 0
    _edit(root / "src/adopt_hook/models.py", "le=10_000", "le=100_000")
    r = _hook("stop", {"cwd": str(root)}, root)
    assert r.returncode == 2 and "maximum raised" in r.stderr and "do not accept a weakening" in r.stderr
    assert _hook("stop", {"cwd": str(root), "stop_hook_active": True}, root).returncode == 0
    assert _hook("stop", {"cwd": str(tmp_path / "nowhere")}, root).returncode == 0   # no rule file: nothing to do


def test_shipped_hook_configs_name_real_commands():
    root = pathlib.Path(__file__).resolve().parents[2]          # the monorepo root, above invariants-py/
    if not (root / "action.yml").exists():
        pytest.skip("action.yml and .pre-commit-hooks.yaml live at the monorepo root, not in the sdist")
    import yaml
    hooks = yaml.safe_load((root / ".pre-commit-hooks.yaml").read_text())
    assert hooks[0]["id"] == "inv-check" and hooks[0]["entry"] == "inv check --relock --fail-on-repin"
    assert hooks[0]["language"] == "system" and hooks[0]["pass_filenames"] is False
    action = yaml.safe_load((root / "action.yml").read_text())
    run = action["runs"]["steps"][0]["run"]
    assert 'inv check "${args[@]}"' in run and "--base" in run
    assert "INV_ALLOW_WEAKENING" in action["runs"]["steps"][0]["env"]
    assert textwrap.dedent(run)
