"""The weakening gate's holes: `inv check --base` failing open, the scan config, contracts and
schemas the lock did not judge, tightenings it called weakenings, the agent hooks and pre-commit."""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Annotated, Literal, Optional, TypedDict

import pytest

from invariants import lock as lockmod
from cli_support import run_inv, write_project

MODELS = '''\
from pydantic import BaseModel, Field


class Refund(BaseModel):
    amount: int = Field(gt=0, le=10_000)
'''
API = '''\
import invariants as inv


@inv.guard("R-1")
def refund(amount: int) -> None:
    pass
'''
RULES = '''\
contracts:
  refund: {type: pydantic, ref: "gate_app.models:Refund"}
rules:
  - id: R-1
    guard: "py:gate_app.api.refund"
    bad: "args.amount > 10000"
'''
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _app(tmp_path: pathlib.Path, rules: str = RULES) -> pathlib.Path:
    root = tmp_path / "proj"
    write_project(root, {
        "pyproject.toml": '[project]\nname = "gate_app"\nversion = "0"\n',
        "src/gate_app/__init__.py": "",
        "src/gate_app/models.py": MODELS,
        "src/gate_app/api.py": API,
        "invariants.yaml": rules,
    })
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, env=GIT_ENV, capture_output=True)


def _commit(root: pathlib.Path, msg: str = "base") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


# --- 1. --base fails closed ------------------------------------------------------------------------
@needs_git
def test_base_ref_that_does_not_exist_fails(tmp_path):
    root = _app(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _commit(root)
    r = run_inv("check", "--base", "no-such-ref", cwd=root)
    assert r.returncode == 1 and "FAIL  since no-such-ref" in r.stdout and "not found" in r.stdout, r.stdout


def test_base_outside_a_git_checkout_fails(tmp_path):
    root = _app(tmp_path)
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 1 and "not a git checkout" in r.stdout, r.stdout


@needs_git
def test_base_without_a_lock_is_a_first_adoption(tmp_path):
    root = _app(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n")
    _git(root, "add", "README")
    _git(root, "commit", "-qm", "before invariants")
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 0 and "first adoption" in r.stdout, r.stdout


@needs_git
@pytest.mark.parametrize("stored,msg", [
    ("not json", "not a lock this invariants reads"),
    (json.dumps({"lock_version": 2, "rules": {}}), "lock version 2"),
    (json.dumps({"lock_version": 99, "rules": {}}), "newer invariants"),
])
def test_base_lock_that_cannot_be_compared_fails(tmp_path, stored, msg):
    root = _app(tmp_path)
    fresh = (root / "invariants.lock").read_text()
    _git(root, "init", "-q", "-b", "main")
    (root / "invariants.lock").write_text(stored)
    _commit(root)
    (root / "invariants.lock").write_text(fresh)
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 1 and msg in r.stdout, r.stdout
    r = run_inv("check", "--base", "main", "--allow-weakening", cwd=root)
    assert r.returncode == 0 and "allowed by a reviewer" in r.stdout, r.stdout


@needs_git
def test_base_compares_a_matching_lock(tmp_path):
    root = _app(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    _commit(root)
    r = run_inv("check", "--base", "main", cwd=root)
    assert r.returncode == 0 and "0 change(s) since main" in r.stdout, r.stdout


def test_action_fails_when_the_base_cannot_be_fetched():
    root = pathlib.Path(__file__).resolve().parents[2]
    if not (root / "action.yml").exists():
        pytest.skip("action.yml lives at the monorepo root, not in the sdist")
    import yaml
    run = yaml.safe_load((root / "action.yml").read_text())["runs"]["steps"][0]["run"]
    assert "|| true" not in run
    assert "rev-parse --verify" in run and "exit 1" in run


# --- 2. the scan configuration is pinned -----------------------------------------------------------
DOOR_FILES = {
    "src/gate_door/__init__.py": "",
    "src/gate_door/repo.py": "def write(x):\n    return x\n",
    "src/gate_door/service.py": "from gate_door.repo import write\n\n\ndef save(x):\n    return write(x)\n",
    "src/gate_door/admin.py": "from gate_door.repo import write\n\n\ndef sneak(x):\n    return write(x)\n",
    "invariants.yaml": 'rules:\n  - id: DOOR\n    door: "py:gate_door.service.save"\n'
                       '    sink: "py:gate_door.repo.write"\n'
                       '    allow: ["py:gate_door.admin.sneak"]\n',
}


def _door_app(tmp_path: pathlib.Path, scan: str = "") -> pathlib.Path:
    root = tmp_path / "door"
    write_project(root, {**DOOR_FILES, "pyproject.toml": '[project]\nname = "gate_door"\nversion = "0"\n' + scan})
    return root


def test_lock_pins_the_scan_roots_and_excludes(tmp_path):
    root = _door_app(tmp_path)
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stderr
    pin = json.loads((root / "invariants.lock").read_text())["scan"]
    assert pin["roots"] == [".", "src"] and "tests" in pin["exclude"]
    assert run_inv("check", cwd=root).returncode == 0
    r = run_inv("check", "--exclude", "gate_door/admin.py", cwd=root)
    assert r.returncode == 1 and "exclude added" in r.stdout and "WEAKENED" in r.stdout, r.stdout
    r = run_inv("check", "--relock", "--exclude", "gate_door/admin.py", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout, r.stdout


def test_a_pyproject_exclude_is_a_weakening_and_removing_one_is_not(tmp_path):
    root = _door_app(tmp_path)
    assert run_inv("lock", cwd=root).returncode == 0
    py = root / "pyproject.toml"
    py.write_text(py.read_text() + '[tool.invariants.scan]\nexclude = ["tests", "gate_door/admin.py"]\n')
    r = run_inv("lock", cwd=root)
    assert r.returncode == 1 and "scan: exclude added: (none) -> 'gate_door/admin.py'  WEAKENED" in r.stdout, r.stdout
    assert "exclude removed" in r.stdout and "refused" in r.stderr
    py.write_text(py.read_text().replace('exclude = ["tests", "gate_door/admin.py"]', 'exclude = ["tests"]'))
    r = run_inv("lock", "--accept-weakening", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    py.write_text(py.read_text().replace('exclude = ["tests"]', "exclude = []"))
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0 and "exclude removed" in r.stdout and "WEAKENED" not in r.stdout, r.stdout


@pytest.mark.parametrize("old,new,weak", [
    ({"roots": ["src"], "exclude": []}, {"roots": ["src"], "exclude": ["x"]}, True),
    ({"roots": ["src"], "exclude": ["x"]}, {"roots": ["src"], "exclude": []}, False),
    ({"roots": [".", "src"], "exclude": []}, {"roots": ["src"], "exclude": []}, True),
    # an added root renames the files under it and widens the excludes (fix round 23)
    ({"roots": ["src"], "exclude": []}, {"roots": [".", "src"], "exclude": []}, True),
    (None, {"roots": ["src"], "exclude": list(lockmod.DEFAULT_SCAN_EXCLUDE)}, False),
    (None, {"roots": ["src"], "exclude": ["x"]}, True),
])
def test_scan_pin_changes(old, new, weak):
    a = {"lock_version": lockmod.LOCK_VERSION, **({"scan": old} if old else {})}
    cs = lockmod.changes(a, {"lock_version": lockmod.LOCK_VERSION, "scan": new})
    assert cs and any(c.weakened for c in cs) is weak, [str(c) for c in cs]


# --- 3. lock weakening holes -----------------------------------------------------------------------
def _lock(contracts=None, rules=None, schemas=None):
    return {"lock_version": lockmod.LOCK_VERSION, "rules": rules or {}, "targets": {}, "providers": {},
            "contracts": contracts or {}, "schemas": schemas or {}}


@pytest.mark.parametrize("rules,note", [
    ({"R": {"semantic": {"subject": {"contract": "refund"}}, "targets": [], "doc": {}}}, "rule R named it"),
    ({}, "no longer checked"),
])
def test_removing_a_contract_is_a_weakening(rules, note):
    old = _lock({"refund": {"type": "pydantic", "ref": "m:Refund"}}, rules)
    cs = lockmod.changes(old, _lock(rules=rules))
    c = next(c for c in cs if c.where == "contract refund")
    assert c.weakened and note in c.note


def _schema_diff(a, b):
    """The changes between two versions of one pinned pydantic schema used by a contract."""
    ea, eb = {"name": "m.M", "pydantic": a}, {"name": "m.M", "pydantic": b}
    ka, kb = lockmod.contracts._sha(lockmod.canonical(ea)), lockmod.contracts._sha(lockmod.canonical(eb))
    old = _lock({"c": {"type": "pydantic", "ref": "m:M", "schema": {"$ref": ka}}}, schemas={ka: ea})
    new = _lock({"c": {"type": "pydantic", "ref": "m:M", "schema": {"$ref": kb}}}, schemas={kb: eb})
    return lockmod.changes(old, new)


ITEM = {"type": "object", "properties": {"n": {"type": "integer", "maximum": 5}}}


@pytest.mark.parametrize("a,b,weak", [
    # a $ref replaced by {} (Any): the value is no longer checked
    ({"properties": {"i": {"$ref": "#/$defs/Item"}}, "$defs": {"Item": ITEM}},
     {"properties": {"i": {}}}, True),
    # pointed at a renamed but identical schema: not a weakening
    ({"properties": {"i": {"$ref": "#/$defs/Item"}}, "$defs": {"Item": ITEM}},
     {"properties": {"i": {"$ref": "#/$defs/Item2"}}, "$defs": {"Item2": ITEM}}, False),
    # pointed at a different, looser schema
    ({"properties": {"i": {"$ref": "#/$defs/Item"}}, "$defs": {"Item": ITEM}},
     {"properties": {"i": {"$ref": "#/$defs/Loose"}}, "$defs": {"Loose": {"type": "object"}}}, True),
    # additionalProperties: closed (extra=forbid) or absent, then open
    ({"type": "object", "additionalProperties": False}, {"type": "object"}, True),
    ({"type": "object", "additionalProperties": False}, {"type": "object", "additionalProperties": True}, True),
    ({"type": "object"}, {"type": "object", "additionalProperties": True}, True),
    ({"type": "object"}, {"type": "object", "additionalProperties": False}, False),
    ({"type": "object", "additionalProperties": True}, {"type": "object"}, False),
])
def test_schema_reference_and_extra_property_changes(a, b, weak):
    cs = _schema_diff(a, b)
    assert any(c.weakened for c in cs) is weak, [str(c) for c in cs]


@pytest.mark.parametrize("path,old,new,weak", [
    ("pydantic.$defs.Item", ITEM, None, True),            # no root: assumed referenced
    ("pydantic.properties.i.$ref", "#/$defs/Item", None, True),
    ("pydantic.properties.i.$ref", None, "#/$defs/Item", False),
    ("pydantic.additionalProperties", None, True, True),
    ("pydantic.additionalProperties", None, {"type": "integer"}, True),
    ("jsonschema.properties.n.maximum", 5, 6, True),     # a dataclass or TypedDict contract
    ("operations.op.requestBody.content.application/json.properties.n.maximum", 5, 6, True),
    ("operations.op.parameters[0].required", True, False, True),
    ("operations.op.parameters[0].required", False, True, False),
])
def test_constraint_weakening_without_neighbours(path, old, new, weak):
    assert (lockmod.constraint_weakening(path, old, new) is not None) is weak


def test_a_removed_def_that_was_referenced_is_a_weakening():
    old = {"pydantic": {"properties": {"i": {"$ref": "#/$defs/Item"}}, "$defs": {"Item": ITEM}}}
    unused = {"pydantic": {"properties": {}, "$defs": {"Item": ITEM}}}
    new = {"pydantic": {"properties": {"i": {"$ref": "#/$defs/Item"}}}}
    assert lockmod.constraint_weakening("pydantic.$defs.Item", ITEM, None, old, new)
    assert lockmod.constraint_weakening("pydantic.$defs.Item", ITEM, None, unused, {"pydantic": {}}) is None


# --- 5. tightenings are not weakenings -------------------------------------------------------------
@pytest.mark.parametrize("a,b,weak", [
    ({"type": "integer", "minimum": 1}, {"type": "integer", "exclusiveMinimum": 0}, False),   # ge=1 -> gt=0
    ({"type": "integer", "minimum": 1}, {"type": "integer", "exclusiveMinimum": -1}, True),
    ({"type": "number", "minimum": 1}, {"type": "number", "exclusiveMinimum": 0}, True),      # 0.5 now passes
    ({"type": "number", "minimum": 1}, {"type": "number", "exclusiveMinimum": 1}, False),
    ({"type": "integer", "exclusiveMinimum": 0}, {"type": "integer", "minimum": 1}, False),   # gt=0 -> ge=1
    ({"type": "integer", "exclusiveMinimum": 0}, {"type": "integer", "minimum": 0}, True),
    ({"type": "integer", "maximum": 9}, {"type": "integer", "exclusiveMaximum": 10}, False),  # le=9 -> lt=10
    ({"type": "integer", "maximum": 9}, {"type": "integer", "exclusiveMaximum": 11}, True),
    ({"type": "number", "maximum": 10}, {"type": "number", "exclusiveMaximum": 10}, False),   # le -> lt
    ({"type": "integer", "exclusiveMaximum": 10}, {"type": "integer", "maximum": 9}, False),
    ({"type": "integer", "exclusiveMaximum": 10}, {"type": "integer", "maximum": 10}, True),
    ({"enum": ["a", "b"], "type": "string"}, {"const": "a", "type": "string"}, False),
    ({"enum": ["a", "b"], "type": "string"}, {"const": "c", "type": "string"}, True),
    ({"type": "number"}, {"type": "integer"}, False),
    ({"type": "integer"}, {"type": "number"}, True),
    ({"type": ["string", "null"]}, {"type": ["string"]}, False),
    ({"type": ["string"]}, {"type": ["string", "null"]}, True),
    ({"pattern": "[a-z]+"}, {"pattern": "^[a-z]+$"}, False),
    ({"pattern": "^[a-z]+"}, {"pattern": "^[a-z]+$"}, False),
    ({"pattern": "[a-z]+\\$"}, {"pattern": "^[a-z]+\\$"}, False),
    ({"pattern": "^[a-z]+$"}, {"pattern": "[a-z]+"}, True),
    ({"pattern": "a|b"}, {"pattern": "^a|b$"}, True),
    ({"pattern": "[a-z]+"}, {"pattern": "[a-y]+"}, True),
])
def test_tightenings_are_not_weakenings(a, b, weak):
    cs = _schema_diff({"properties": {"x": a}}, {"properties": {"x": b}})
    assert cs and any(c.weakened for c in cs) is weak, [str(c) for c in cs]


# --- 4. dataclass, TypedDict and live OpenAPI contracts ---------------------------------------------
SHAPES = '''\
import dataclasses
from typing import Annotated, Literal, TypedDict

from pydantic import Field


@dataclasses.dataclass
class Refund:
    amount: Annotated[int, Field(gt=0, le=10_000)]
    currency: Literal["usd", "eur"] = "usd"


class Transfer(TypedDict):
    amount: Annotated[int, Field(le=500)]
'''
SHAPE_RULES = '''\
contracts:
  refund: {type: dataclass, ref: "gate_shapes.models:Refund"}
  transfer: {type: schema, ref: "gate_shapes.models:Transfer"}
'''


@pytest.mark.parametrize("old,new,weak", [
    ("le=10_000", "le=100_000", True),
    ("le=10_000", "le=5_000", False),
    ('Literal["usd", "eur"]', 'Literal["usd", "eur", "gbp"]', True),
    ("Field(le=500)", "Field(le=600)", True),
])
def test_dataclass_and_typeddict_contracts_are_judged_as_json_schema(tmp_path, old, new, weak):
    root = tmp_path / "shapes"
    write_project(root, {"pyproject.toml": '[project]\nname = "gate_shapes"\nversion = "0"\n',
                         "src/gate_shapes/__init__.py": "", "src/gate_shapes/models.py": SHAPES,
                         "invariants.yaml": SHAPE_RULES})
    assert run_inv("lock", cwd=root).returncode == 0
    lock = json.loads((root / "invariants.lock").read_text())
    body = lock["schemas"][lock["contracts"]["refund"]["schema"]["$ref"]]
    assert body["jsonschema"]["properties"]["amount"]["maximum"] == 10_000
    models = root / "src/gate_shapes/models.py"
    models.write_text(models.read_text().replace(old, new))
    r = run_inv("lock", "--check", cwd=root)
    assert r.returncode == 1 and ("WEAKENED" in r.stderr) is weak, r.stderr


class Kind(enum.Enum):
    A = "a"


class Bound:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@dataclasses.dataclass
class Inner:
    n: int


@dataclasses.dataclass
class Outer:
    amount: Annotated[int, Bound(gt=0), Bound(le=10)]
    tags: Annotated[list[str], Bound(max_length=3)]
    kind: Kind
    mode: Literal["x"]
    inner: Optional[Inner] = None


class TD(TypedDict, total=False):
    a: dict[str, float]


def test_stdlib_json_schema_without_pydantic():
    s = lockmod.stdlib_json_schema(Outer)
    p = s["properties"]
    assert p["amount"] == {"type": "integer", "exclusiveMinimum": 0, "maximum": 10}
    assert p["tags"] == {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    assert p["kind"] == {"enum": ["a"]} and p["mode"] == {"const": "x"}
    assert p["inner"] == {"anyOf": [{"$ref": "#/$defs/" + Inner.__qualname__}, {"type": "null"}]}
    assert s["required"] == ["amount", "kind", "mode", "tags"] and Inner.__qualname__ in s["$defs"]
    td = lockmod.stdlib_json_schema(TD)
    assert td == {"type": "object", "properties": {"a": {"type": "object", "additionalProperties": {"type": "number"}}},
                  "required": []}


FASTAPI_APP = '''\
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class Order(BaseModel):
    quantity: int = Field(gt=0, le=100)


@app.post("/orders", operation_id="placeOrder")
def place(order: Order) -> Order:
    return order


@app.get("/health", operation_id="health")
def health() -> dict:
    return {}
'''


def test_openapi_contract_from_a_live_app(tmp_path):
    pytest.importorskip("fastapi")
    root = tmp_path / "api"
    write_project(root, {"pyproject.toml": '[project]\nname = "gate_api"\nversion = "0"\n',
                         "src/gate_api/__init__.py": "", "src/gate_api/main.py": FASTAPI_APP,
                         "invariants.yaml": 'contracts:\n  api: {type: openapi, app: "gate_api.main:app"}\n'})
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0, r.stderr
    entry = json.loads((root / "invariants.lock").read_text())["contracts"]["api"]
    assert entry["app"] == "gate_api.main:app" and set(entry["operations"]) == {"placeOrder", "health"}
    main = root / "src/gate_api/main.py"
    main.write_text(main.read_text().replace("le=100", "le=50"))
    r = run_inv("lock", cwd=root)
    assert r.returncode == 0 and "maximum" in r.stdout and "WEAKENED" not in r.stdout, r.stdout + r.stderr
    main.write_text(main.read_text().replace("le=50", "le=1000"))
    r = run_inv("lock", "--check", cwd=root)
    assert r.returncode == 1 and "maximum raised" in r.stderr and "WEAKENED" in r.stderr, r.stderr


@pytest.mark.parametrize("contract,msg", [
    ({"type": "openapi", "app": "nocolon"}, "module:attribute"),
    ({"type": "openapi", "app": "json:dumps", "ref": "x.json"}, "not both"),
    ({"type": "openapi", "app": "json:dumps"}, r"no openapi\(\) method"),
])
def test_openapi_app_contract_mistakes(tmp_path, contract, msg):
    from invariants.errors import LoadError
    with pytest.raises(LoadError, match=msg):
        lockmod.contract_hash("api", contract, tmp_path)


# --- 6. the pretool hook -----------------------------------------------------------------------------
def _hook(event: str, payload: dict, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "invariants.cli", "hook", event], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd, timeout=60)


@pytest.mark.parametrize("command,blocked", [
    ("python -c \"open('invariants.lock','w').write('{}')\"", True),
    ("git checkout main -- invariants.lock", True),
    ("cat x > invariants.lock", True),
    ("cat new.json | tee -a invariants.lock", True),
    ("printf '{}' | sponge invariants.lock", True),
    ("F=invariants.lock; echo {} > $F", True),
    ("cp /tmp/x invariants.l*", True),
    ("ln -sf /tmp/x invariants.lock", True),
    ("cat invariants.lock", False),
    ("head -5 invariants.lock && grep -c rule invariants.lock 2>/dev/null", False),
    ("git diff invariants.lock | less", False),
    ("git -C sub log -p invariants.lock", False),
    ("inv lock --check && inv check && inv status", False),
    ("inv lock && git add invariants.lock", False),
    ("echo 'x = 1' >> pyproject.toml", True),
    ("sed -i '' s/tests/tmp/ pyproject.toml", True),
    ("cat pyproject.toml", False),
    ("pytest -q", False),
])
def test_hook_blocks_shell_writes_unless_clearly_read_only(tmp_path, command, blocked):
    r = _hook("pretool", {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    assert r.returncode == (2 if blocked else 0), (command, r.stderr)


def test_hook_resolves_symlinks_to_the_lock(tmp_path):
    (tmp_path / "invariants.lock").write_text("{}")
    (tmp_path / "notes.json").symlink_to(tmp_path / "invariants.lock")
    for tool_input in ({"file_path": "notes.json", "content": "{}"},
                       {"file_path": str(tmp_path / "notes.json"), "old_string": "{", "new_string": "["}):
        r = _hook("pretool", {"tool_name": "Write", "tool_input": tool_input, "cwd": str(tmp_path)}, tmp_path)
        assert r.returncode == 2 and "direct edit of invariants.lock" in r.stderr


PYPROJECT = '[project]\nname = "x"\n\n[tool.invariants.scan]\nexclude = ["tests"]\n\n[tool.ruff]\nline-length = 100\n'


@pytest.mark.parametrize("tool,tool_input,blocked", [
    ("Edit", {"old_string": 'exclude = ["tests"]', "new_string": 'exclude = ["tests", "admin"]'}, True),
    ("Edit", {"old_string": "line-length = 100", "new_string": "line-length = 120"}, False),
    ("Edit", {"old_string": "[tool.ruff]", "new_string": '[tool.invariants]\nx = 1\n\n[tool.ruff]'}, True),
    ("MultiEdit", {"edits": [{"old_string": "100", "new_string": "90"},
                             {"old_string": '["tests"]', "new_string": "[]"}]}, True),
    ("Write", {"content": PYPROJECT.replace("100", "88")}, False),
    ("Write", {"content": '[project]\nname = "x"\n'}, True),
])
def test_hook_blocks_edits_of_the_invariants_table(tmp_path, tool, tool_input, blocked):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    payload = {"tool_name": tool, "tool_input": {"file_path": "pyproject.toml", **tool_input}, "cwd": str(tmp_path)}
    r = _hook("pretool", payload, tmp_path)
    assert r.returncode == (2 if blocked else 0), r.stderr
    assert ("[tool.invariants]" in r.stderr) is blocked


# --- 7. pre-commit: a re-pinned lock must be staged ------------------------------------------------
def test_fail_on_repin_fails_once_after_re_pinning(tmp_path):
    root = _app(tmp_path)
    models = root / "src/gate_app/models.py"
    models.write_text(models.read_text().replace("le=10_000", "le=5_000"))         # a tightening
    r = run_inv("check", "--relock", "--fail-on-repin", cwd=root)
    assert r.returncode == 1, r.stdout
    assert "invariants.lock re-pinned; review and `git add invariants.lock`" in r.stdout
    assert '"maximum": 5000' in (root / "invariants.lock").read_text()
    assert run_inv("check", "--relock", "--fail-on-repin", cwd=root).returncode == 0   # now fresh
    models.write_text(models.read_text().replace("le=5_000", "le=50_000"))          # a weakening
    r = run_inv("check", "--relock", "--fail-on-repin", cwd=root)
    assert r.returncode == 1 and "WEAKENED" in r.stdout and "re-pinned" not in r.stdout
