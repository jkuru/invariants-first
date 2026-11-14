"""Fix round 21 (lock): each test failed before its fix, except two guards that passed before and
must keep passing (a loosened bound in a nested class, one class used twice is one definition).

1. A parameterised PEP 695 alias (`type Box[T] = list[T]`, `type PosBox[T] = Annotated[T, Gt(0)]`)
   is pinned with its type parameters substituted (Box[int] is list[int], not list[~T]); a parameter
   typed PosBox[int] carries its constraint into param_schemas; Annotated[Box[int], MaxLen(3)] bounds
   an array (maxItems), not a string.
2. A contract file that cannot be read (a permission error) is a LoadError, whatever its type.
3. The stdlib JSON Schema of a TypedDict unwraps Required[] / NotRequired[] / ReadOnly[] (typing's and
   typing_extensions'), keeping the constraints inside, and required-ness follows them. Its $defs keys
   hold no "." (a nested class is `Inner`, pydantic's key, not `Outer.Inner`, which a diff path splits:
   an equivalent bound swap inside it was judged a weakening), and two classes of one name are two
   definitions (the second one's constraints were dropped, so loosening them showed no change).
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import sys
import types
import typing
from typing import Annotated, Any

import annotated_types as at
import pytest

from invariants.errors import LoadError
from invariants.lock import pinning
from invariants_schema_diff.changes import _json_paths
from invariants_schema_diff.schemas import _param_schema_changes, constraint_weakening
from invariants.lock import pin_jsonschema
from invariants.lock import pin_signatures
from invariants.lock import pin_types

te = pytest.importorskip("typing_extensions")
try:
    TypeAliasType: Any = typing.TypeAliasType  # type: ignore[attr-defined]
except AttributeError:                                         # Python 3.11
    TypeAliasType = te.TypeAliasType

T = typing.TypeVar("T")
K = typing.TypeVar("K")
V = typing.TypeVar("V")
Box = TypeAliasType("Box", list[T], type_params=(T,))
PosBox = TypeAliasType("PosBox", Annotated[T, at.Gt(0)], type_params=(T,))
Swapped = TypeAliasType("Swapped", dict[V, K], type_params=(K, V))


def _weakenings(sa: Any, sb: Any) -> list[str]:
    ra, rb = {"jsonschema": sa}, {"jsonschema": sb}
    return [w for p, x, y in _json_paths(ra, rb) if (w := constraint_weakening(p, x, y, ra, rb))]


# --- 1. parameterised PEP 695 aliases -------------------------------------------------------------
def box_int(a: Box[int], b: PosBox[int], c: Swapped[str, int]) -> None: ...


def test_alias_arguments_are_substituted_in_types() -> None:
    pin = pinning._signature_pin(box_int, "t", pinning.Schemas(inline=True))
    assert pin["types"]["a"] == {"generic": "list", "args": ["int"]}
    assert pin["types"]["b"] == "int"
    assert pin["types"]["c"] == {"generic": "dict", "args": ["int", "str"]}


def test_constraint_of_an_alias_argument_is_pinned() -> None:
    pin = pinning._signature_pin(box_int, "t", pinning.Schemas(inline=True))
    assert pin["param_schemas"]["b"]["exclusiveMinimum"] == 0
    assert pin_signatures._has_constraints(PosBox[int])


def test_json_type_substitutes_alias_arguments() -> None:
    assert pin_jsonschema._json_type(Box[int], {}, 0) == {"type": "array", "items": {"type": "integer"}}
    assert pin_jsonschema._json_type(PosBox[int], {}, 0) == {"type": "integer", "exclusiveMinimum": 0}
    assert pin_jsonschema._json_type(Annotated[Box[int], at.MaxLen(3)], {}, 0)["maxItems"] == 3
    assert pin_jsonschema._json_type(PosBox[Box[int]], {}, 0)["type"] == "array"


Loose = TypeAliasType("Loose", Annotated[T, at.Gt(-5)], type_params=(T,))


def tight(b: PosBox[int]) -> None: ...


def loose(b: Loose[int]) -> None: ...


def test_alias_constraint_loosened_is_a_weakening() -> None:
    a = pinning._signature_pin(tight, "t", pinning.Schemas(inline=True))["param_schemas"]
    b = pinning._signature_pin(loose, "t", pinning.Schemas(inline=True))["param_schemas"]
    assert any(c.weakened for c in _param_schema_changes("target t", a, b))


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax needs Python 3.12")
def test_pep695_syntax_alias() -> None:
    ns: dict[str, Any] = {}
    exec("from typing import Annotated\nimport annotated_types as at\n"
         "type Box[T] = list[T]\ntype PosBox[T] = Annotated[T, at.Gt(0)]\n"
         "def f(a: Box[int], b: PosBox[int]) -> None: ...\n", ns)
    pin = pinning._signature_pin(ns["f"], "t", pinning.Schemas(inline=True))
    assert pin["types"] == {"a": {"generic": "list", "args": ["int"]}, "b": "int", "return": "null"}
    assert pin["param_schemas"]["b"]["exclusiveMinimum"] == 0
    assert pin_jsonschema._json_type(ns["PosBox"][int], {}, 0) == {"type": "integer", "exclusiveMinimum": 0}


def test_alias_arguments_that_do_not_fit_keep_both() -> None:
    Ts = te.TypeVarTuple("Ts")
    Row = TypeAliasType("Row", tuple[Annotated[int, at.Gt(0)], te.Unpack[Ts]], type_params=(Ts,))
    ann = Row[Annotated[int, at.Le(3)]]
    assert pin_signatures._has_constraints(ann)
    got = pin_types._type_schema(ann, {}, set())
    assert got["generic"] == "Row" and "value" in got and got["args"]


# --- 2. an unreadable contract file ---------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs file permissions that apply")
@pytest.mark.parametrize("ctype", ["jsonschema", "openapi", "protobuf"])
def test_unreadable_contract_file_is_a_load_error(tmp_path: pathlib.Path, ctype: str) -> None:
    f = tmp_path / "c.json"
    f.write_text("{}", encoding="utf-8")
    f.chmod(0)
    try:
        with pytest.raises(LoadError, match=r"contract c: c.json cannot be read \(PermissionError\)"):
            pinning.contract_hash("c", {"type": ctype, "ref": "c.json"}, tmp_path)
    finally:
        f.chmod(0o644)


# --- 3. TypedDict key qualifiers, $defs keys ------------------------------------------------------
_RO = getattr(typing, "ReadOnly", te.ReadOnly)


class PartTD(typing.TypedDict, total=False):
    a: typing.Required[Annotated[int, at.Le(5)]]
    b: typing.NotRequired[str]
    c: _RO[typing.Required[int]]
    d: int


class FullTD(te.TypedDict):
    a: te.NotRequired[Annotated[int, at.Le(5)]]
    b: te.ReadOnly[te.NotRequired[int]]
    c: Annotated[te.Required[int], at.Ge(0)]
    d: int


class MixedTD(typing.TypedDict):                               # typing's class, typing_extensions' wrappers
    a: te.ReadOnly[te.NotRequired[int]]
    b: int


@pytest.mark.parametrize("cls, required", [(PartTD, ["a", "c"]), (FullTD, ["c", "d"]), (MixedTD, ["b"])])
def test_typeddict_qualifiers_are_unwrapped(cls: type, required: list[str]) -> None:
    s = pin_jsonschema.stdlib_json_schema(cls)
    assert s["required"] == required
    assert all("x-python-type" not in p for p in s["properties"].values()), s
    if "a" in s["properties"] and cls is not MixedTD:
        assert s["properties"]["a"] == {"type": "integer", "maximum": 5}


def test_typeddict_qualifier_keeps_annotated_metadata() -> None:
    assert pin_jsonschema.stdlib_json_schema(FullTD)["properties"]["c"] == {"type": "integer", "minimum": 0}


def test_typeddict_qualifiers_in_the_resolved_schema() -> None:
    got = pinning.resolved_schema(PartTD)
    assert got["fields"]["b"] == "str" and got["fields"]["c"] == "int" and got["required"] == ["a", "c"]


def _nested(bound: str) -> type:
    mod = types.ModuleType("r21_nested")
    sys.modules["r21_nested"] = mod
    exec("import dataclasses\nfrom typing import Annotated\nimport annotated_types as at\n"
         "@dataclasses.dataclass\nclass Outer:\n"
         "    @dataclasses.dataclass\n    class Inner:\n"
         f"        x: Annotated[int, {bound}]\n"
         "    inner: 'Outer.Inner'\n", mod.__dict__)
    return typing.cast(type, mod.__dict__["Outer"])


def test_nested_class_key_has_no_dot() -> None:
    s = pin_jsonschema.stdlib_json_schema(_nested("at.Le(5)"))
    assert list(s["$defs"]) == ["Inner"] and s["properties"]["inner"] == {"$ref": "#/$defs/Inner"}


@pytest.mark.parametrize("old, new", [("at.Le(5)", "at.Lt(5)"), ("at.Ge(1)", "at.Gt(0)")])
def test_nested_class_equivalent_bound_is_no_weakening(old: str, new: str) -> None:
    assert _weakenings(pin_jsonschema.stdlib_json_schema(_nested(old)), pin_jsonschema.stdlib_json_schema(_nested(new))) == []


def test_nested_class_loosened_bound_is_a_weakening() -> None:
    assert _weakenings(pin_jsonschema.stdlib_json_schema(_nested("at.Le(5)")),
                       pin_jsonschema.stdlib_json_schema(_nested("at.Le(50)"))) == ["maximum raised: larger values pass"]


def test_old_dotted_key_re_pinned_is_no_weakening() -> None:
    new = pin_jsonschema.stdlib_json_schema(_nested("at.Le(5)"))
    old = {**new, "properties": {"inner": {"$ref": "#/$defs/Outer.Inner"}}, "$defs": {"Outer.Inner": new["$defs"]["Inner"]}}
    assert _weakenings(old, new) == []


def _two_items(bound: int) -> type:
    for name, field, b in (("r21_ma", "x", 5), ("r21_mb", "y", bound)):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        exec("import dataclasses\nfrom typing import Annotated\nimport annotated_types as at\n"
             f"@dataclasses.dataclass\nclass Item:\n    {field}: Annotated[int, at.Le({b})]\n", mod.__dict__)

    @dataclasses.dataclass
    class Top:
        a: sys.modules["r21_ma"].Item  # type: ignore[name-defined]
        b: sys.modules["r21_mb"].Item  # type: ignore[name-defined]
    return Top


def test_two_classes_of_one_name_are_two_definitions() -> None:
    s = pin_jsonschema.stdlib_json_schema(_two_items(5))
    assert s["properties"]["a"] != s["properties"]["b"]
    assert sorted(s["$defs"]) == ["Item", "r21_mb__Item"]


def test_two_classes_of_one_name_loosened_is_a_weakening() -> None:
    assert _weakenings(pin_jsonschema.stdlib_json_schema(_two_items(5)),
                       pin_jsonschema.stdlib_json_schema(_two_items(50))) == ["maximum raised: larger values pass"]


def test_one_class_twice_is_one_definition() -> None:
    @dataclasses.dataclass
    class Twice:
        a: PartTD
        b: list[PartTD]
    s = pin_jsonschema.stdlib_json_schema(Twice)
    assert list(s["$defs"]) == ["PartTD"] and s["properties"]["b"]["items"] == {"$ref": "#/$defs/PartTD"}
