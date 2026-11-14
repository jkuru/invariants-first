"""One reading of Python annotations: the load-time type check (typecheck.from_py), the lock's type pin
(pin_types._type_schema), the JSON Schemas a pin takes (pin_jsonschema's stdlib reading and
pin_signatures' pydantic one) and a provider's declared return type (loader) all see the same
underlying type through Annotated, NewType, PEP 695 aliases (`type X = ...` and typing_extensions'
TypeAliasType), Optional/Union, Literal, dataclasses, TypedDicts, pydantic models and Enums. They
share invariants._typeinfo; before it, the type check read an alias or a NewType as dyn."""
from __future__ import annotations

import ast
import dataclasses
import enum
import sys
import typing
from typing import Annotated, Any, Literal, NewType, Optional, Union

import pydantic
import pytest
import typing_extensions as te

from invariants import _typeinfo as ti
from invariants import typecheck as tc
from invariants.lock import pin_jsonschema, pin_signatures, pin_types

Desc = Any          # frozenset of atoms ("int", "string", "record", ("list", Desc), ("map", Desc)), None: unknown

_ALIASES: dict[str, Any] = {}
if sys.version_info >= (3, 12):
    _ALIASES["Literal"] = Literal
    exec("type A = int\ntype AA = A\ntype L = Literal['a', 'b']\ntype G[T] = list[T]\n", _ALIASES)
# typing_extensions' backport: the same alias on 3.11
TA = te.TypeAliasType("TA", int)
TL = te.TypeAliasType("TL", Literal["a", "b"])
N = NewType("N", int)
NTA = NewType("NTA", TA)                        # type: ignore[valid-newtype]


@dataclasses.dataclass
class DC:
    x: int


class TD(typing.TypedDict):
    x: int


class TETD(te.TypedDict):                       # typing.is_typeddict misses it on 3.11
    x: int


class PM(pydantic.BaseModel):
    x: int


class E(enum.Enum):
    a = "x"


class IE(enum.IntEnum):
    a = 1


class SE(str, enum.Enum):
    a = "s"


def _cases() -> list[tuple[str, Any, Desc]]:
    i, s = frozenset({"int"}), frozenset({"string"})
    cases: list[tuple[str, Any, Desc]] = [
        ("int", int, i), ("Annotated", Annotated[int, pydantic.Field(gt=0)], i), ("NewType", N, i),
        ("alias (backport)", TA, i), ("Optional[alias]", Optional[TA], frozenset({"int", "null"})),
        ("Literal alias", TL, s), ("NewType of alias", NTA, i), ("TypedDict", TD, frozenset({"record"})),
        ("typing_extensions TypedDict", TETD, frozenset({"record"})), ("dataclass", DC, frozenset({"record"})),
        ("pydantic model", PM, frozenset({"record"})), ("Enum", E, s), ("IntEnum", IE, i), ("str Enum", SE, s),
        ("list[alias]", list[TA], frozenset({("list", i)})), ("dict[str, alias]", dict[str, TA], frozenset({("map", i)})),
        ("tuple", tuple[int, TA], frozenset({("list", i)})), ("Union of aliases", Union[TA, TL], i | s),
        ("Annotated alias", Annotated[TA, pydantic.Field(gt=0)], i), ("list[NewType]", list[N], frozenset({("list", i)})),
    ]
    if sys.version_info >= (3, 11) and hasattr(enum, "StrEnum"):
        cases.append(("StrEnum", enum.StrEnum("SE2", {"a": "s"}), s))
    if _ALIASES:
        A, AA, L, G = _ALIASES["A"], _ALIASES["AA"], _ALIASES["L"], _ALIASES["G"]
        cases += [("type A = int", A, i), ("alias of alias", AA, i), ("Optional[A]", Optional[A], frozenset({"int", "null"})),
                  ("A | None", A | None, frozenset({"int", "null"})), ("type L = Literal", L, s),
                  ("NewType of A", NewType("NA", A), i), ("list[A]", list[A], frozenset({("list", i)})),
                  ("dict[str, A]", dict[str, A], frozenset({("map", i)})), ("Union[A, L]", Union[A, L], i | s),
                  ("G[int]", G[int], frozenset({("list", i)})), ("tuple[A, A]", tuple[A, A], frozenset({("list", i)}))]
    return cases


def _kind(v: Any) -> str:
    v = v.value if isinstance(v, enum.Enum) else v
    return {bool: "bool", int: "int", float: "double", str: "string", type(None): "null"}[type(v)]


def _typecheck_view(t: tc.T) -> Desc:
    if t.kinds is None:
        return None
    out: set[Any] = set()
    for k in t.kinds:
        if k == "list":
            out.add(("list", _typecheck_view(t.elem) if t.elem is not None else None))
        elif k == "map":
            out.add("record" if t.py is not None else ("map", _typecheck_view(t.elem) if t.elem else None))
        else:
            out.add(k)
    return frozenset(out)


_PIN_NAMES = {"int": "int", "float": "double", "str": "string", "bool": "bool", "null": "null"}


def _union(descs: list[Desc]) -> Desc:
    return None if any(d is None for d in descs) else frozenset().union(*descs)


def _pin_view(p: Any) -> Desc:
    """A pin_types._type_schema value (an Enum's values are pinned as their reprs)."""
    if isinstance(p, str):
        return frozenset({_PIN_NAMES[p]}) if p in _PIN_NAMES else None
    if "literal" in p:
        return frozenset(_kind(v) for v in p["literal"])
    if "values" in p:
        return frozenset(_kind(ast.literal_eval(v)) for v in p["values"])
    if "anyOf" in p:
        return _union([_pin_view(o) for o in p["anyOf"]])
    if "$ref" in p:
        return frozenset({"record"})
    if p.get("generic") in ("list", "tuple"):
        return frozenset({("list", _union([_pin_view(a) for a in p["args"]]))})
    if p.get("generic") == "dict":
        return frozenset({("map", _pin_view(p["args"][1]))})
    return None


_JSON_NAMES = {"integer": "int", "number": "double", "string": "string", "boolean": "bool", "null": "null"}


def _json_view(s: dict[str, Any], defs: dict[str, Any]) -> Desc:
    if "$ref" in s:
        target = defs.get(s["$ref"].rpartition("/")[2], {})      # an Enum, an alias, a model
        return _json_view(target, defs) if "$ref" not in target else None
    if "enum" in s or "const" in s:
        return frozenset(_kind(v) for v in s.get("enum", [s.get("const")]))
    if "anyOf" in s:
        return _union([_json_view(o, defs) for o in s["anyOf"]])
    if s.get("type") == "array":
        items = s.get("prefixItems") or [s["items"]]
        return frozenset({("list", _union([_json_view(o, defs) for o in items]))})
    if s.get("type") == "object":
        if "properties" in s:
            return frozenset({"record"})
        return frozenset({("map", _json_view(s["additionalProperties"], defs))})
    return frozenset({_JSON_NAMES[s["type"]]}) if s.get("type") in _JSON_NAMES else None


def _views(t: Any) -> dict[str, Desc]:
    defs = pin_jsonschema._Defs()
    stdlib = pin_jsonschema._json_type(t, defs, 0)
    pyd = pin_signatures._annotation_schema(t)
    return {"typecheck": _typecheck_view(tc.from_py(t)), "pin": _pin_view(pin_types._type_schema(t, {}, set())),
            "pydantic schema": _json_view(pyd, pyd.get("$defs", {})),
            # the stdlib reading is the fallback for what pydantic cannot describe: it has no pydantic model
            **({} if ti.is_pydantic_model(t) else {"stdlib schema": _json_view(stdlib, defs)})}


@pytest.mark.parametrize("name, ann, want", _cases(), ids=[c[0] for c in _cases()])
def test_every_view_reads_the_same_underlying_type(name: str, ann: Any, want: Desc) -> None:
    views = _views(ann)
    assert views == dict.fromkeys(views, want), name


# --- what the type check now refuses through an alias or a NewType --------------------------------
def _amount(x: N, y: TA, z: list[TA]) -> Optional[TA]:
    return x


def test_typecheck_refuses_through_newtype_and_alias() -> None:
    params, ret = tc.signature_types(_amount)
    env = {"args": tc.T({"map"}, fields=params), "result": ret}
    for clause in ('args.x == "a"', 'args.y.startsWith("a")', 'args.z.exists(v, v == "a")', 'result > "a"'):
        with pytest.raises(tc.TypeMismatch):
            tc.check_clause(clause, env)
    tc.check_clause("args.x > 0 && args.y < 10 && args.z.all(v, v > 0) && result != null", env)


def test_typecheck_reads_a_typing_extensions_typeddict_as_a_closed_record() -> None:
    with pytest.raises(tc.TypeMismatch, match="no field 'y'"):
        tc.check_clause("args.r.y == 1", {"args": tc.T({"map"}, fields={"r": tc.from_py(TETD)})})


def test_unbound_generic_alias_reads_its_value() -> None:
    G = te.TypeAliasType("G", list[typing.Any], type_params=())
    assert tc.from_py(G).kinds == frozenset({"list"})


# --- a provider declared `int` may return an alias, a NewType or an Annotated int -----------------
@pytest.mark.parametrize("ann, decl", [(N, "int"), (TA, "int"), (Annotated[int, pydantic.Field(gt=0)], "int"),
                                       (Optional[TA], "int | null"), (int, "int")])
def test_provider_type_matches_through_aliases(ann: Any, decl: str) -> None:
    from invariants.loader.schemas import _parse_type, _type_set
    typ, nullable = _parse_type(decl)
    assert _type_set(ann) == frozenset({typ} | ({type(None)} if nullable else set()))


def test_provider_type_still_refuses_another_type() -> None:
    from invariants.loader.schemas import _type_set
    assert _type_set(NewType("S", str)) != frozenset({int})
    assert _type_set(Optional[TA]) != frozenset({int})


# --- the shared helpers ---------------------------------------------------------------------------
def test_strip_removes_every_wrapper() -> None:
    assert ti.strip(Annotated[NTA, "m"]) is int
    assert ti.strip(Annotated[Annotated[TA, 1], 2]) is int
    assert ti.union_args(Optional[TA]) == (TA, type(None))
    assert ti.union_args(int) is None
    assert ti.is_typeddict(TETD) and ti.is_typeddict(TD) and not ti.is_typeddict(dict)
    assert ti.is_schema_class(PM) and ti.is_schema_class(DC) and not ti.is_schema_class(int)
    assert ti.is_enum(IE) and not ti.is_enum(IE.a)
    assert ti.is_newtype(N) and not ti.is_newtype(int)


class RO(te.TypedDict):
    x: te.ReadOnly[TA]
    y: te.NotRequired[te.ReadOnly[int]]


def test_typecheck_reads_through_typeddict_key_qualifiers() -> None:
    env = {"args": tc.T({"map"}, fields={"r": tc.from_py(RO)})}
    for clause in ('args.r.x == "a"', 'args.r.y == "a"'):
        with pytest.raises(tc.TypeMismatch, match="int and string"):
            tc.check_clause(clause, env)
    assert tc.from_py(te.ReadOnly[TA]).kinds == frozenset({"int"})
