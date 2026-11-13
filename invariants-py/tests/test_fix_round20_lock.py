"""Fix round 20 (lock): each test failed before its fix.

1. stdlib_json_schema of a self-referential dataclass (Node with children: list["Node"]) keeps the
   Node definition the schema refers to ({"$ref": "#/$defs/Node", "$defs": {...}}, pydantic's shape),
   so no $ref dangles and a loosened bound inside it is still judged a weakening.
2. The stdlib JSON Schema reading unwraps a PEP 695 alias (`type X = ...`, typing.TypeAliasType),
   alone, inside X | None, and a parameterised one.
3. An OpenAPI contract file that cannot be opened (a permission error) says it cannot be read, with
   the error type, not that it is not UTF-8 text.
4. lockmod.read reads the lock as UTF-8 whatever the locale's preferred encoding is.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import typing
from typing import Annotated, Any

import annotated_types as at
import pytest

from invariants import lock as lockmod
from invariants.errors import LoadError
from invariants.lock.contracts import _json_paths
from invariants.lock import pin_contracts
from invariants.lock import pin_jsonschema

try:
    TypeAliasType: Any = typing.TypeAliasType  # type: ignore[attr-defined]
except AttributeError:                                         # Python 3.11
    TypeAliasType = pytest.importorskip("typing_extensions").TypeAliasType


# --- 1. a self-referential schema class ---------------------------------------------------------
@dataclasses.dataclass
class Node:
    n: Annotated[int, at.Le(5)]
    children: list[Node]


@dataclasses.dataclass
class LooseNode:
    n: Annotated[int, at.Le(10)]
    children: list[LooseNode]


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [*([node["$ref"]] if isinstance(node.get("$ref"), str) else []),
                *(r for v in node.values() for r in _refs(v))]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def _resolves(schema: dict[str, Any], ref: str) -> bool:
    target: Any = schema
    for part in ref[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            return False
        target = target[part]
    return True


def test_self_referential_dataclass_keeps_its_definition() -> None:
    s = pin_jsonschema.stdlib_json_schema(Node)
    assert _refs(s) and all(_resolves(s, r) for r in _refs(s))
    assert s["$ref"] == "#/$defs/Node"
    assert s["$defs"]["Node"]["properties"]["n"] == {"type": "integer", "maximum": 5}


def test_self_referential_loosened_bound_is_a_weakening() -> None:
    old = pin_jsonschema.stdlib_json_schema(Node)
    new = json.loads(json.dumps(pin_jsonschema.stdlib_json_schema(LooseNode)).replace("LooseNode", "Node"))
    oe, ne = {"name": "Node", "jsonschema": old}, {"name": "Node", "jsonschema": new}
    found = [(p, why) for p, a, b in _json_paths(oe, ne)
             if (why := lockmod.constraint_weakening(p, a, b, oe, ne)) is not None]
    assert [p for p, _ in found] == ["jsonschema.$defs.Node.properties.n.maximum"], found


@dataclasses.dataclass
class Leaf:
    x: int


@dataclasses.dataclass
class Top:
    leaf: Leaf
    more: list[Leaf]


def test_non_recursive_top_class_stays_inline() -> None:
    s = pin_jsonschema.stdlib_json_schema(Top)
    assert "$ref" not in s and s["type"] == "object" and set(s["$defs"]) == {"Leaf"}


# --- 2. PEP 695 aliases ---------------------------------------------------------------------------
Small = TypeAliasType("Small", Annotated[int, at.Le(5)])
Names = TypeAliasType("Names", list[str])
T = typing.TypeVar("T")
Box = TypeAliasType("Box", list[T], type_params=(T,))


@pytest.mark.parametrize("ann, want", [
    (Small, {"type": "integer", "maximum": 5}),
    (Small | None, {"anyOf": [{"type": "integer", "maximum": 5}, {"type": "null"}]}),
    (typing.Optional[Names], {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}),
    (list[Small], {"type": "array", "items": {"type": "integer", "maximum": 5}}),
])
def test_json_type_unwraps_a_type_alias(ann: Any, want: Any) -> None:
    assert pin_jsonschema._json_type(ann, {}, 0) == want


def test_json_type_unwraps_a_parameterised_alias() -> None:
    assert pin_jsonschema._json_type(Box[int], {}, 0)["type"] == "array"


# --- 3. an unreadable OpenAPI file ----------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs file permissions that apply")
def test_openapi_permission_error_is_not_called_an_encoding_error(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "api.json"
    f.write_text("{}", encoding="utf-8")
    f.chmod(0)
    try:
        with pytest.raises(LoadError) as e:
            pin_contracts._openapi_file("api", f)
    finally:
        f.chmod(0o644)
    assert "UTF-8" not in str(e.value) and "cannot be read (PermissionError)" in str(e.value)


def test_openapi_bad_bytes_still_say_utf8(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "api.json"
    f.write_bytes(b"\xff\xfe{}")
    with pytest.raises(LoadError, match="cannot be read as UTF-8 text"):
        pin_contracts._openapi_file("api", f)


# --- 4. the lock reads the same under any locale --------------------------------------------------
def test_lock_reads_as_utf8_under_a_latin1_locale(tmp_path: pathlib.Path) -> None:
    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1",
           "PYTHONIOENCODING": "latin-1"}
    probe = subprocess.run([sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
                           env=env, capture_output=True, text=True)
    if "utf" in probe.stdout.lower().replace("-", ""):
        pytest.skip("no non-UTF-8 locale on this machine")
    lock = {"lock_version": lockmod.LOCK_VERSION, "spec": "invariants/v1", "rules": {}, "targets": {},
            "providers": {}, "contracts": {}, "schemas": {"x": {"note": "Überweisung ≤ 5 €"}}}
    path = tmp_path / lockmod.LOCK_NAME
    path.write_bytes(lockmod.dumps(lock).encode("utf-8"))
    here = lockmod.read(path)
    code = ("import json, pathlib, sys; from invariants import lock; "
            "print(json.dumps(lock.read(pathlib.Path(sys.argv[1])), sort_keys=True))")
    r = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == here
