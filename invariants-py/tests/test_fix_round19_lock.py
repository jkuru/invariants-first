"""Fix round 19 (lock review): each test failed before its fix.

3. additionalProperties absent on a map (`dict`, `dict[str, Any]` read without pydantic, a free-form
   OpenAPI object) to a value schema (`dict[str, int]`) is a tightening, not WEAKENED. (`true` to a
   schema was already judged a tightening.)
4. Pins that were changes only now get a weakening check: a `{field: type}` contract, an Enum
   contract, a dataclass a signature names (judged as the JSON Schema the pin describes), a
   whole-file OpenAPI contract (its operations are pinned beside the digest), and Annotated
   constraints (`Field(gt=0)`, `MaxLen(3)`) on a target's or provider's parameters (`param_schemas`).
5. Prose (statement, why, owner, class) never makes the lock stale: `inv lock --check` reports it and
   passes. `mediation.chokepoint` is not prose: a new or edited one is WEAKENED (it decides `held`).
   A signature spelled differently with the same parameters, kinds, defaults and resolved types is
   reported, not stale; a renamed parameter, a new default or a changed Annotated marker is stale.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import sys
import textwrap
import uuid
from typing import Any

import pytest

from invariants import lock as lockmod
from cli_support import CLEAN, copy_app, run_inv


# --- 3. additionalProperties absent on a map ----------------------------------------------------------
def _schema_diff(a, b, kind="pydantic"):
    ch = lockmod.schema_diff._schema_changes([("contract c", "schema", "A", "B")], {"A": {"name": "M", kind: a}},
                                  {"B": {"name": "M", kind: b}})
    return any(c.weakened for c in ch), [str(c) for c in ch]


def _with_field(field):
    return {"type": "object", "properties": {"m": field}, "required": ["m"]}


@pytest.mark.parametrize("old,new,weak", [
    ({"type": "object"}, {"type": "object", "additionalProperties": {"type": "integer"}}, False),
    ({"type": "object"}, {"type": "object", "additionalProperties": True}, False),
    ({"type": "object", "additionalProperties": True}, {"type": "object", "additionalProperties": {"type": "integer"}},
     False),
    ({"type": "object", "additionalProperties": {}}, {"type": "object", "additionalProperties": {"type": "integer"}},
     False),
    ({"type": "object", "additionalProperties": {"type": "integer"}}, {"type": "object"}, True),
    ({"type": "object", "additionalProperties": {"type": "integer"}}, {"type": "object", "additionalProperties": True},
     True),
], ids=["absent-schema", "absent-true", "true-schema", "empty-schema", "schema-absent", "schema-true"])
@pytest.mark.parametrize("kind", ["pydantic", "jsonschema"])
def test_a_map_value_schema_is_judged_by_what_it_lets_pass(kind, old, new, weak):
    got, cs = _schema_diff(_with_field(old), _with_field(new), kind)
    assert got is weak, cs


def test_a_model_root_with_extras_ignored_stays_closed():
    a = {"type": "object", "properties": {"x": {"type": "integer"}}}
    got, cs = _schema_diff(a, {**a, "additionalProperties": {"type": "integer"}})
    assert got, cs
    got, cs = _schema_diff({"$defs": {"N": a}, "type": "object"},
                           {"$defs": {"N": {**a, "additionalProperties": True}}, "type": "object"})
    assert got, cs


@dataclasses.dataclass
class MapBare:
    m: dict


@dataclasses.dataclass
class MapInt:
    m: dict[str, int]


@dataclasses.dataclass
class MapAny:
    m: dict[str, Any]


def test_a_stdlib_dataclass_dict_narrowed_to_dict_of_int_is_not_weakened():
    for old in (MapBare, MapAny):
        got, cs = _schema_diff(lockmod.stdlib_json_schema(old), lockmod.stdlib_json_schema(MapInt), "jsonschema")
        assert not got, cs
    got, cs = _schema_diff(lockmod.stdlib_json_schema(MapInt), lockmod.stdlib_json_schema(MapBare), "jsonschema")
    assert got, cs


def test_an_openapi_free_form_object_given_a_value_schema_is_not_weakened():
    def op(body):
        return {"type": "openapi", "app": "m:app", "operations": {"r": {"method": "POST", "path": "/r", "requestBody": {
            "required": True, "content": {"application/json": {"schema": {
                "type": "object", "properties": {"meta": body}}}}}}}}
    cs = lockmod.changes({"contracts": {"api": op({"type": "object"})}},
                          {"contracts": {"api": op({"type": "object", "additionalProperties": {"type": "string"}})}})
    assert cs and not any(c.weakened for c in cs), [str(c) for c in cs]


# --- 4. pins with a weakening check ------------------------------------------------------------------
@pytest.fixture
def mod(tmp_path, monkeypatch):
    """write(src) -> a fresh importable module name (one per call, so each build imports anew)."""
    monkeypatch.syspath_prepend(str(tmp_path))
    made: list[str] = []

    def write(src: str) -> str:
        name = "r19_" + uuid.uuid4().hex[:10]
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(src))
        made.append(name)
        return name
    yield write
    for n in made:
        sys.modules.pop(n, None)


def _norm(lock, name):
    return json.loads(re.sub(r"r19_[0-9a-f]{10}", "MOD", json.dumps(lock)))


def _diff(mod, tmp_path, a_src, b_src, contracts=None, targets=None, providers=None):
    locks = []
    for src in (a_src, b_src):
        name = mod(src)
        doc = {"contracts": {k: {**v, **({"ref": v["ref"].replace("MOD", name)} if "MOD" in v.get("ref", "") else {})}
                             for k, v in (contracts or {}).items()},
               "targets": {t.replace("MOD", name): {"effects": "external"} for t in (targets or [])},
               "providers": {k: {**v, "from": v["from"].replace("MOD", name)} for k, v in (providers or {}).items()},
               "rules": []}
        locks.append(_norm(lockmod.build(doc, tmp_path), name))
    return lockmod.changes(*locks)


def _weak(cs):
    return any(c.weakened for c in cs)


FIELDS = "import typing\nFIELDS = {{'amount': {}, 'note': str}}\n"


@pytest.mark.parametrize("old,new,weak", [
    ("int", "typing.Any", True), ("int", "int | None", True), ("typing.Literal['a']", "typing.Literal['a', 'b']", True),
    ("typing.Any", "int", False), ("int | None", "int", True), ("float", "int", False)],
    ids=["to-any", "optional", "literal-widened", "from-any", "optional-dropped", "number-to-integer"])
def test_a_field_type_contract_is_judged(mod, tmp_path, old, new, weak):
    cs = _diff(mod, tmp_path, FIELDS.format(old), FIELDS.format(new),
               contracts={"c": {"type": "schema", "ref": "MOD:FIELDS"}})
    assert cs and _weak(cs) is weak, [str(c) for c in cs]


def test_a_field_removed_from_a_field_type_contract_is_weakened(mod, tmp_path):
    cs = _diff(mod, tmp_path, FIELDS.format("int"), "FIELDS = {'note': str}\n",
               contracts={"c": {"type": "schema", "ref": "MOD:FIELDS"}})
    assert _weak(cs), [str(c) for c in cs]


ENUM = "import enum\nclass E(enum.Enum):\n    A = 1\n{}"


def test_an_enum_contract_member_added_is_weakened_and_removed_is_not(mod, tmp_path):
    c = {"c": {"type": "python", "ref": "MOD:E"}}
    cs = _diff(mod, tmp_path, ENUM.format(""), ENUM.format("    B = 2\n"), contracts=c)
    assert _weak(cs) and any("enum widened" in str(x) for x in cs), [str(x) for x in cs]
    cs = _diff(mod, tmp_path, ENUM.format("    B = 2\n"), ENUM.format(""), contracts=c)
    assert cs and not _weak(cs), [str(x) for x in cs]


def test_a_dataclass_a_target_signature_names_is_judged(mod, tmp_path):
    src = "import dataclasses\n@dataclasses.dataclass\nclass Body:\n    amount: {}\ndef f(body: Body) -> None:\n    pass\n"
    cs = _diff(mod, tmp_path, src.format("int"), src.format("int | str"), targets=["py:MOD.f"])
    assert _weak(cs), [str(c) for c in cs]


SIG = "import typing\nimport annotated_types as at\nimport pydantic\ndef f(amount: {}) -> None:\n    pass\n"


@pytest.mark.parametrize("old,new,weak", [
    ("typing.Annotated[int, pydantic.Field(gt=0)]", "int", True),
    ("typing.Annotated[int, pydantic.Field(lt=10)]", "typing.Annotated[int, pydantic.Field(lt=100)]", True),
    ("typing.Annotated[int, at.Gt(0)]", "typing.Annotated[int, at.Ge(0)]", True),
    ("typing.Annotated[str, at.MaxLen(3)]", "typing.Annotated[str, at.MaxLen(9)]", True),
    ("typing.Optional[typing.Annotated[int, at.Gt(0)]]", "typing.Optional[int]", True),
    ("typing.Annotated[int, pydantic.Field(lt=100)]", "typing.Annotated[int, pydantic.Field(lt=10)]", False),
    ("int", "typing.Annotated[int, pydantic.Field(gt=0)]", False),
], ids=["removed", "raised", "gt-to-ge", "max-len", "inside-optional", "lowered", "added"])
def test_annotated_constraints_on_a_target_are_judged(mod, tmp_path, old, new, weak):
    cs = _diff(mod, tmp_path, SIG.format(old), SIG.format(new), targets=["py:MOD.f"])
    assert cs and _weak(cs) is weak, [str(c) for c in cs]


def test_annotated_constraints_on_a_provider_are_judged(mod, tmp_path):
    src = "import typing\nimport pydantic\ndef p(amount: {}) -> int:\n    return 1\n"
    prov = {"amt": {"type": "int", "from": "py:MOD.p", "args": {}}}
    cs = _diff(mod, tmp_path, src.format("typing.Annotated[int, pydantic.Field(le=5)]"), src.format("int"),
               providers=prov)
    assert _weak(cs) and any(c.where == "provider amt" and c.weakened for c in cs), [str(c) for c in cs]


def test_a_non_constraint_marker_is_not_a_param_schema(mod, tmp_path):
    name = mod("import typing\nclass Dep:\n    pass\ndef f(repo: typing.Annotated[int, Dep()], n: int) -> None:\n"
               "    pass\n")
    lock = lockmod.build({"targets": {f"py:{name}.f": {"effects": "external"}}, "rules": []}, tmp_path)
    assert "param_schemas" not in lock["targets"][f"py:{name}.f"]


def _openapi(maximum, description="d"):
    return json.dumps({"openapi": "3.0.0", "info": {"title": "t", "version": "1", "description": description},
                       "paths": {"/r": {"post": {"operationId": "r", "requestBody": {"content": {"application/json": {
                           "schema": {"type": "object", "properties": {"a": {"type": "integer",
                                                                             "maximum": maximum}}}}}}}}}})


def test_a_whole_file_openapi_contract_is_judged_by_its_operations(tmp_path):
    c = {"api": {"type": "openapi", "ref": "api.json"}}

    def lock(text):
        (tmp_path / "api.json").write_text(text)
        return lockmod.build({"contracts": c, "rules": []}, tmp_path)
    base = lock(_openapi(5))
    assert "hash" in base["contracts"]["api"] and "r" in base["contracts"]["api"]["operations"]
    cs = lockmod.changes(base, lock(_openapi(500)))
    assert _weak(cs), [str(x) for x in cs]
    cs = lockmod.changes(base, lock(_openapi(5, "edited")))
    assert cs and not _weak(cs), [str(x) for x in cs]                # the digest still pins every edit
    cs = lockmod.changes(base, lock(_openapi(3)))
    assert cs and not _weak(cs), [str(x) for x in cs]
    (tmp_path / "api.json").write_text("{not json")
    assert "operations" not in lockmod.build({"contracts": c, "rules": []}, tmp_path)["contracts"]["api"]


# --- 5. prose never makes the lock stale; chokepoint and signatures are judged ------------------------
@pytest.fixture
def app(tmp_path):
    a = copy_app(CLEAN, tmp_path / "clean")
    assert run_inv("lock", a / "invariants.yaml").returncode == 0
    return a


def _edit(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def _check(app):
    return run_inv("lock", "--check", app / "invariants.yaml")


@pytest.mark.parametrize("old,new", [
    ("statement: A withdrawal is never for a non-positive amount.", "statement: Withdrawals are positive."),
    ("owner: team-ledger", "owner: team-payments"),
    ("class: law", "class: policy"),
    ("    scope: call\n    bad: args.amount <= 0", "    why: money must move forward\n    scope: call\n"
                                                   "    bad: args.amount <= 0"),
], ids=["statement", "owner", "class", "why-added"])
def test_prose_edits_do_not_make_the_lock_stale(app, old, new):
    _edit(app / "invariants.yaml", old, new)
    r = _check(app)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "text" in r.stdout and "(not stale)" in r.stdout and "WEAKENED" not in r.stdout
    r = run_inv("check", app / "invariants.yaml", cwd=app)
    lock_line = next(l for l in r.stdout.splitlines() if "lock" in l and ("ok" in l or "FAIL" in l))
    assert lock_line.strip().startswith("ok"), r.stdout


@pytest.mark.parametrize("old,new,weak", [
    ("chokepoint: withdraw is the only caller of store.write_balance",
     "chokepoint: py:ledger.service.withdraw is the only caller", True),
    ("      chokepoint: withdraw is the only caller of store.write_balance\n", "", False),
], ids=["edited", "removed"])
def test_a_chokepoint_edit_is_stale_and_judged(app, old, new, weak):
    _edit(app / "invariants.yaml", old, new)
    r = _check(app)
    assert r.returncode == 1 and "mediation.chokepoint" in r.stderr, r.stdout + r.stderr
    assert ("WEAKENED" in r.stderr) is weak, r.stderr


SERVICE = "ledger/service.py"


@pytest.mark.parametrize("old,new", [
    ("def withdraw(conn, account: str, amount: int) -> None:",
     "def withdraw(conn, account: 'str', amount: typing.Optional[int]) -> None:"),
], ids=["spelling"])
def test_a_signature_spelled_differently_is_not_stale(app, old, new):
    _edit(app / SERVICE, "import invariants as inv\n", "import typing\nimport invariants as inv\nText = str\n")
    if "Optional" in new:          # Optional[int] resolves to int | None: a real type change, pinned in types
        new = new.replace("typing.Optional[int]", "int")
    _edit(app / SERVICE, old, new)
    r = _check(app)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "signature" in r.stdout and "(not stale)" in r.stdout


def test_an_annotation_renamed_to_an_alias_is_stale(app):
    """An alias may stand for an Annotated[...] whose Depends or validator no pin carries, so a renamed
    annotation is stale even when the resolved type is the same (Text = str here)."""
    _edit(app / SERVICE, "import invariants as inv\n", "import invariants as inv\nText = str\n")
    _edit(app / SERVICE, "def withdraw(conn, account: str, amount: int) -> None:",
          "def withdraw(conn, account: 'Text', amount: int) -> None:")
    r = _check(app)
    assert r.returncode == 1 and "signature" in r.stderr, r.stdout + r.stderr


@pytest.mark.parametrize("new", [
    "def withdraw(conn, acct: str, amount: int) -> None:",
    "def withdraw(conn, account: str, amount: int = 1) -> None:",
    "def withdraw(conn, account: str, *, amount: int) -> None:",
    "def withdraw(conn, account: str, amount: float) -> None:",
], ids=["renamed", "default", "keyword-only", "type"])
def test_a_semantic_signature_change_is_stale(app, new):
    _edit(app / SERVICE, "def withdraw(conn, account: str, amount: int) -> None:", new)
    if "acct" in new:
        _edit(app / SERVICE, "_apply(conn, account, store.read_balance(conn, account)",
              "_apply(conn, acct, store.read_balance(conn, acct)")
    r = _check(app)
    assert r.returncode == 1 and "signature" in r.stderr, r.stdout + r.stderr


def test_a_changed_annotated_marker_is_stale(mod, tmp_path):
    src = "import typing\nclass Dep:\n    def __init__(self, f):\n        self.f = f\n    def __repr__(self):\n" \
          "        return f'Dep({{self.f}})'\ndef f(repo: typing.Annotated[int, Dep({!r})]) -> None:\n    pass\n"
    cs = _diff(mod, tmp_path, src.format("get_repo"), src.format("get_fake"), targets=["py:MOD.f"])
    assert any(c.what == "signature" and not c.info for c in cs), [str(c) for c in cs]


def test_a_signature_compared_without_types_stays_stale():
    cs = lockmod.changes({"targets": {"t": {"signature": "f(a: int) -> None"}}},
                          {"targets": {"t": {"signature": "f(a: str) -> None"}}})
    assert [c.info for c in cs] == [False]
