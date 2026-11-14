"""The lock package's layering, and the pure functions of its stages (tested directly, without mocks).

Layering: no module of invariants/lock/ imports the runtime (invariants.guard, invariants.registry) or
the CLI (invariants.cli), lazily or not, and the stages import one another in one direction only:
contracts <- pin_types <- pin_jsonschema <- pin_signatures <- pin_contracts <- pinning, and
cel_canon <- cel_sites <- cel_mono; contracts <- schema_diff; schema_diff, cel_mono <- rule_diff <-
the package (the orchestrator). schema_diff, cel_canon, cel_sites, cel_mono and contracts are the
schema-diff core, which imports nothing of invariants but the CEL engine (cel/, typecheck, _typeinfo):
test_layering_packages.py checks it; the loader's type environments are passed in (clause_types)."""
from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import typing

import pytest

import invariants.lock as lock
from invariants.cel import subset
from invariants.errors import LoadError
from invariants.lock import cel_mono, contracts, pinning, rule_diff, schema_diff
from invariants.lock import cel_canon
from invariants.lock import cel_sites
from invariants.lock import pin_contracts
from invariants.lock import pin_types

LOCK_DIR = pathlib.Path(lock.__file__).parent
FORBIDDEN = ("invariants.guard", "invariants.registry", "invariants.cli")
# the lock modules each stage may import (the package __init__ composes them all)
STAGES = {"contracts": set(), "pin_types": {"contracts"}, "pin_jsonschema": {"contracts", "pin_types"},
          "pin_signatures": {"contracts", "pin_types", "pin_jsonschema"},
          "pin_contracts": {"contracts", "pin_types", "pin_jsonschema", "pin_signatures"},
          "pinning": {"contracts", "pin_types", "pin_jsonschema", "pin_signatures", "pin_contracts"},
          "schema_diff": {"contracts"}, "cel_canon": set(), "cel_sites": {"cel_canon"},
          "cel_mono": {"cel_canon", "cel_sites"}, "rule_diff": {"contracts", "cel_canon", "cel_mono", "schema_diff"}}


def _imports(path: pathlib.Path) -> set[str]:
    """Every module a lock/ file imports, at any depth (lazy imports too), as an absolute name: a
    relative import resolves against invariants.lock, the package every lock/ file is in."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:                                     # `.x` is invariants.lock.x, `..x` invariants.x
                pkg = ["invariants", "lock"][:3 - node.level]
                base = ".".join([*pkg, *([node.module] if node.module else [])])
            out |= {base} | {f"{base}.{a.name}" for a in node.names}
    return out


def _lock_files() -> list[pathlib.Path]:
    files = sorted(LOCK_DIR.glob("*.py"))
    assert {f.stem for f in files} >= {"__init__", *STAGES}
    return files


@pytest.mark.parametrize("path", _lock_files(), ids=lambda p: p.name)
def test_lock_never_imports_the_runtime_or_the_cli(path: pathlib.Path) -> None:
    bad = sorted(m for m in _imports(path) for f in FORBIDDEN if m == f or m.startswith(f + "."))
    assert not bad, f"lock/{path.name} imports {bad}"


@pytest.mark.parametrize("stage", sorted(STAGES))
def test_stages_import_in_one_direction(stage: str) -> None:
    mods = _imports(LOCK_DIR / f"{stage}.py")
    used = {m.split(".")[2] for m in mods if m.startswith("invariants.lock.") and m.count(".") >= 2}
    assert used - {"__init__"} <= STAGES[stage], f"lock/{stage}.py imports lock.{sorted(used - STAGES[stage])}"
    assert "invariants.lock" not in mods, f"lock/{stage}.py imports the orchestrator"


def test_every_stage_is_listed_and_the_order_has_no_cycle() -> None:
    assert {f.stem for f in LOCK_DIR.glob("*.py")} - {"__init__"} == set(STAGES)
    done: set[str] = set()
    while len(done) < len(STAGES):                    # peel the stages whose imports are all peeled
        ready = {s for s, deps in STAGES.items() if s not in done and deps <= done}
        assert ready, f"a cycle among lock stages {sorted(set(STAGES) - done)}"
        done |= ready


def test_the_import_reader_resolves_relative_imports(tmp_path: pathlib.Path) -> None:
    p = LOCK_DIR / "cel_canon.py"
    assert {"invariants.typecheck", "invariants.cel.subset"} <= _imports(p)
    bad = tmp_path / "bad.py"
    bad.write_text("from ..guard import guard\ndef f():\n    from .. import registry\n    import invariants.cli\n"
                   "from .contracts import Change\n")
    assert {"invariants.guard", "invariants.registry", "invariants.cli", "invariants.lock.contracts"} <= _imports(bad)


def test_expand_applies_to() -> None:
    doc = {"targets": {"py:m.a": {}, "py:m.b": {}}, "rules": [{"id": "R-1", "applies_to": "py:m.*"}]}
    assert lock.expand_applies_to(doc) == {"R-1": ["py:m.a", "py:m.b"]}


# --- cel_mono ---------------------------------------------------------------------------------------
X = ("id", "x")
ONE, TWO, ZERO = ("lit", "int", 1), ("lit", "int", 2), ("lit", "int", 0)


@pytest.mark.parametrize("node, raises", [
    (("sel", X, "f"), True),                              # a missing field
    (("idx", X, ONE), True),
    (("call", "has", None, [("sel", X, "f")]), False),    # has() is total
    (("call", "size", None, [X]), True),
    (("un", "-", X), True),
    (("nary", "+", (ONE, TWO)), False),                   # literals: provably in int64
    (("nary", "+", (X, ONE)), True),                      # x may be near int64 max
    (("bin", "/", X, TWO), False),                        # a non-zero literal divisor other than -1
    (("bin", "/", X, ZERO), True),
    (("bin", "%", X, ("lit", "int", -1)), True),
    (("bin", "==", X, ONE), False),
])
def test_can_raise(node: typing.Any, raises: bool) -> None:
    assert cel_sites._can_raise(node) is raises


def _in(*items: typing.Any) -> tuple[typing.Any, ...]:
    return ("bin", "in", X, ("list", list(items)))


def test_walk_in_list_grown_shrunk_and_mixed() -> None:
    out: list[int] = []
    assert cel_mono._walk_in(_in(ONE), _in(ONE, TWO), 1, out, None, None) and out == [1]
    out = []
    assert cel_mono._walk_in(_in(ONE, TWO), _in(ONE), 1, out, None, None) and out == [-1]
    assert not cel_mono._walk_in(_in(ONE), _in(TWO), 1, [], None, None)       # one added, one removed
    assert not cel_mono._walk_in(_in(ONE), _in(ONE, TWO), 0, [], None, None)   # unknown direction
    assert not cel_mono._walk_in(_in(ONE), ("bin", "in", ("id", "y"), ("list", [ONE])), 1, [], None, None)


def test_walk_dispatch_undecidable_kinds() -> None:
    assert cel_mono._walk(("id", "a"), ("id", "b"), 1, []) is False              # no step for `id`
    assert cel_mono._walk(("un", "!", X), ("un", "-", X), 1, []) is False
    out: list[int] = []
    assert cel_mono._walk(("nary", "||", (X, ("id", "y"))), X, 1, out) and out == [-1]   # a disjunct dropped


def test_canon_chain_flattens_and_drops_repeats() -> None:
    tree = cel_mono.canon_tree(subset.parse("a || (b || a)"))
    assert tree == ("nary", "||", cel_canon._order([("id", "a"), ("id", "b")]))
    assert cel_mono.canon_tree(subset.parse("(x > 1) || (x > 1)")) == cel_mono.canon_tree(subset.parse("x > 1"))


# --- schema_diff ------------------------------------------------------------------------------------
@pytest.mark.parametrize("key, old, new, node, why", [
    ("maximum", 10, 20, {}, "maximum raised: larger values pass"),
    ("maximum", 10, 5, {}, None),
    ("minLength", 3, 1, {}, "minLength lowered: smaller values pass"),
    ("maximum", 10, None, {}, "maximum removed: no upper bound"),
    ("minimum", 1, None, {"type": "integer", "exclusiveMinimum": 0}, None),   # ge=1 -> gt=0 on an integer
    ("minimum", 1, None, {"type": "number", "exclusiveMinimum": 0}, "minimum removed: no lower bound"),
])
def test_bound_weakening(key: str, old: typing.Any, new: typing.Any, node: typing.Any, why: str | None) -> None:
    assert schema_diff._bound_weakening(key, old, new, node) == why


def test_enum_weakening() -> None:
    assert schema_diff._enum_weakening("", ["a", "b"], None, {"const": "a"}) is None        # narrowed to a const
    assert schema_diff._enum_weakening("", ["a"], None, {}) == "enum removed: any value passes"
    assert schema_diff._enum_weakening("added", None, ["c"], {}) == "enum widened: more values pass"
    assert schema_diff._enum_weakening("removed", ["c"], None, {}) is None                  # members dropped


@pytest.mark.parametrize("segs, node, model", [
    (["pydantic", "additionalProperties"], None, True),                                       # the root
    (["pydantic", "$defs", "M", "additionalProperties"], None, True),
    (["pydantic", "properties", "tags", "additionalProperties"], {"type": "object"}, False),   # a map
    (["pydantic", "properties", "m", "additionalProperties"], {"properties": {}}, True),
])
def test_model_object(segs: list[str], node: typing.Any, model: bool) -> None:
    assert schema_diff._model_object(segs, node) is model


def test_prefix_items_extended() -> None:
    old, new = [{"type": "string"}], [{"type": "string"}, {"type": "integer"}]
    assert schema_diff._prefix_items_extended(old, new, {}) is None                    # no items: was open
    assert schema_diff._prefix_items_extended(old, new, {"items": {"type": "integer"}}) is None   # same check
    why = schema_diff._prefix_items_extended(old, new, {"items": {"type": "string"}})
    assert why is not None and why.startswith("prefixItems extended to 2")


def test_keyword_weakening_through_constraint_weakening() -> None:
    root = {"pydantic": {"properties": {"a": {"type": "integer", "maximum": 5}}}}
    assert schema_diff.constraint_weakening("pydantic.properties.a.maximum", 5, 9, root, root) \
        == "maximum raised: larger values pass"
    assert schema_diff._keyword_weakening(["pydantic", "properties", "a", "type"], "", "number", "integer",
                                          root, root) is None                          # number -> integer
    assert schema_diff.constraint_weakening("other.properties.a.maximum", 5, 9) is None   # not judged
    # a structural keyword removed says what no longer applies (not only that the schema changed)
    assert schema_diff.constraint_weakening("pydantic.properties.a.items", {"type": "string"}, None) \
        == "items removed: what it checked no longer applies"
    assert schema_diff.constraint_weakening("pydantic.properties.a.items", {"type": "string"}, {}) \
        == "items changed: the schema may accept more input"


def test_schema_entry_changes_judges_each_leaf() -> None:
    sa = {"name": "m.M", "pydantic": {"properties": {"a": {"maximum": 5}}}}
    sb = {"name": "m.M", "pydantic": {"properties": {"a": {"maximum": 9}}}}
    diffs = contracts._json_paths(sa, sb)
    (c,) = schema_diff._schema_entry_changes("schema m.M", "used by target t", sa, sb, diffs)
    assert c.weakened and c.what == "pydantic.properties.a.maximum"
    assert c.note == "maximum raised: larger values pass; used by target t"


# --- rule_diff --------------------------------------------------------------------------------------
def test_as_list() -> None:
    assert rule_diff._as_list("py:m.f") == ["py:m.f"]
    assert rule_diff._as_list(None) == [] and rule_diff._as_list(["a", "b"]) == ["a", "b"]


def test_residual_change() -> None:
    assert rule_diff._residual_change("rule R", ["p"], None).weakened          # removed: can raise standing
    assert not rule_diff._residual_change("rule R", None, "p").weakened


def test_semantic_changes_dispatch() -> None:
    (c,) = rule_diff._semantic_changes("rule R", "falsified_by", ["t1"], [], {}, {}, True, None)
    assert not c.weakened and c.note == "a falsifier was dropped"
    assert rule_diff._semantic_changes("rule R", "applies_to", "a*", "b*", {}, {}, False, None) == []
    (c,) = rule_diff._semantic_changes("rule R", "applies_to", "a*", "b*", {}, {}, True, None)
    assert c.what == "applies_to" and not c.weakened
    (c,) = rule_diff._semantic_changes("rule R", "bind", {"a": "x"}, {"a": "y"}, {}, {}, True, None)
    assert c.weakened and c.what == "bind.a"


def test_enforcer_changes() -> None:
    old = [{"point": "decorator", "mode": "enforce", "on_error": "fail_closed"}]
    new = [{"point": "decorator", "mode": "enforce", "on_error": "fail_open_and_page"}]
    (c,) = rule_diff._enforcer_changes("rule R", old, new)
    assert c.weakened and c.note == "fail_open: an error in the rule lets the call through"
    (c,) = rule_diff._enforcer_changes("rule R", old, [])
    assert c.what == "enforcer decorator removed" and c.weakened
    (c,) = rule_diff._enforcer_field_changes("rule R", "decorator", {"mode": "enforce"}, {"mode": "detect"})
    assert c.weakened and c.what == "enforcer decorator mode"


def test_doc_changes() -> None:
    (c,) = rule_diff._doc_changes("rule R", {"why": "sha256:a"}, {"why": "sha256:b"})
    assert c.info and not c.weakened and c.what == "why text edited"
    (c,) = rule_diff._doc_changes("rule R", {}, {"mediation.chokepoint": "sha256:a"})
    assert c.weakened and c.what == "mediation.chokepoint added"
    (c,) = rule_diff._doc_changes("rule R", {"mediation.chokepoint": "sha256:a"}, {})
    assert not c.weakened


def test_with_renamed_operations() -> None:
    op = {"method": "GET", "path": "/refunds"}
    a, b = {"operations": {"listRefunds": op}}, {"operations": {"list_refunds": op}}
    renamed, (c,) = rule_diff._with_renamed_operations("contract api", a, b)
    assert renamed == {"operations": {"list_refunds": op}} and c.what == "operation listRefunds renamed to list_refunds"
    assert rule_diff._with_renamed_operations("contract api", b, b) == (b, [])


def test_pin_changes_collects_schema_refs() -> None:
    a = {"types": {"x": {"$ref": "sha256:aa"}}, "signature": "f(x)"}
    b = {"types": {"x": {"$ref": "sha256:bb"}}, "signature": "f(x)"}
    refs: list[tuple[typing.Any, ...]] = []
    assert rule_diff._pin_changes("target t", "target", "types", a, b, {}, refs) == []
    # the fifth item is the polarity of the reference's place (+1: not under not, oneOf or if)
    assert refs == [("target t types.x", "types.x", "sha256:aa", "sha256:bb", schema_diff.POS)]
    a = {"operations": {"createRefund": {"method": "POST"}}}
    (c,) = rule_diff._pin_changes("contract api", "contract", "operations", a, {"operations": {}},
                                  {"createRefund": ["R-1"]}, [])
    assert c.weakened and c.note == "operation removed while rule R-1 names it"


def test_matched_and_edited_clauses() -> None:
    a, b = [("x > 1", ""), ("y", "")], [("x>1", "new message"), ("z", "")]
    ka = [cel_canon._clause_key(w, subset.parse) for w, _ in a]
    kb = [cel_canon._clause_key(w, subset.parse) for w, _ in b]
    a_left, b_left = [0, 1], [0, 1]
    (c,) = rule_diff._matched_clauses("rule R", a, b, ka, kb, a_left, b_left)
    assert c.what == "bad message of 'x>1'" and a_left == [1] and b_left == [1]
    (c,) = rule_diff._edited_clauses("rule R", a, b, a_left, b_left, None)
    assert c.what == "bad" and c.weakened                                   # y -> z: direction unknown
    (c,) = rule_diff._edited_clauses("rule R", a, b, [1], [], None)
    assert c.what == "bad clause removed" and c.weakened


# --- pinning ----------------------------------------------------------------------------------------
def test_rule_pin_and_subject_operations() -> None:
    raw = {"id": "R-1", "status": "enforce", "subject": {"target": "py:m.f", "contract": "api", "operation": "op"},
           "enforcers": [{"point": "edge", "target": "py:m.g"}], "bad": "x", "why": "because"}
    entry = pinning._rule_pin(raw, {"R-1": ["py:m.h"]})
    assert entry["targets"] == ["py:m.f", "py:m.g", "py:m.h"] and entry["status"] == "enforce"
    assert "why" not in entry["semantic"] and set(entry["doc"]) == {"why"}
    assert pinning._subject_operations({"rules": [raw, {"id": "R-2"}]}) == {"api": ["op"]}


@dataclasses.dataclass
class _Point:
    x: int
    y: int = 0


def test_class_fields_and_generic_schema() -> None:
    assert pin_types._class_fields(_Point, {}, set()) == {"dataclass": "_Point", "fields": {"x": "int", "y": "int"},
                                                         "required": ["x"]}
    assert pin_types._type_schema(list[int], {}, set()) == {"generic": "list", "args": ["int"]}
    assert pin_types._type_schema(typing.Optional[int], {}, set()) == {"anyOf": ["int", "null"]}
    defs: dict[str, typing.Any] = {}
    ref = pin_types._schema_class_ref(_Point, defs, set(), None)
    assert ref == {"$ref": pinning._class_ref(_Point)} and set(defs) == {pinning._class_ref(_Point)}


SPEC = {"paths": {"/r/{id}": {
    "parameters": [{"$ref": "#/components/parameters/Id"}],
    "post": {"operationId": "createRefund", "summary": "prose",
             "requestBody": {"required": True, "content": {"application/json": {
                 "schema": {"$ref": "#/components/schemas/Refund"}}}},
             "responses": {"200": {"content": {}}}}}},
    "components": {"parameters": {"Id": {"name": "id", "in": "path", "schema": {"type": "string"}}},
                   "schemas": {"Refund": {"type": "object", "title": "prose",
                                          "properties": {"amount": {"type": "number", "maximum": 10}}}}}}


def test_openapi_doc_operation() -> None:
    pins = pinning.Schemas()
    doc = pin_contracts._OpenAPIDoc("api", "api.json", SPEC, pins)
    entry = doc.operation("/r/{id}", "post", SPEC["paths"]["/r/{id}"], SPEC["paths"]["/r/{id}"]["post"])
    assert entry["method"] == "POST" and entry["parameters"] == [{"name": "id", "in": "path",
                                                                  "schema": {"type": "string"}}]
    ref = entry["requestBody"]["content"]["application/json"]
    assert entry["requestBody"]["required"] is True and entry["responses"] == {"200": {}}
    assert list(pins.table.values()) == [{"name": "api.json/components/schemas/Refund", "schema": {
        "type": "object", "properties": {"amount": {"type": "number", "maximum": 10}}}}]
    assert ref == {"$ref": next(iter(pins.table))}
    cyclic = {"components": {"x": {"$ref": "#/components/x"}}}
    with pytest.raises(LoadError, match="cycle"):
        pin_contracts._OpenAPIDoc("api", "api.json", cyclic, pins).deref({"$ref": "#/components/x"})


def test_python_contract_pin() -> None:
    entry = pin_contracts._python_contract_pin("c", "json", {"type": "python", "ref": "json"}, pinning.Schemas())
    assert entry == {"type": "python", "ref": "json"}                         # a module: its targets pin it
    entry = pin_contracts._python_contract_pin("c", "json:dumps", {}, pinning.Schemas())
    assert entry["signature"].startswith("dumps(obj")


def test_pin_every_operation(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "api.json"
    f.write_text(json.dumps(SPEC))
    pins, entry = pinning.Schemas(), {}
    pin_contracts._pin_every_operation("api", f, entry, pins)
    assert set(entry["operations"]) == {"createRefund"} and len(pins.table) == 1
    f.write_text(json.dumps({"paths": {"/x": {"get": {"operationId": "x", "parameters": [{"$ref": "#/nope"}]}}}}))
    pins, entry = pinning.Schemas(), {}
    pin_contracts._pin_every_operation("api", f, entry, pins)                       # unreadable: the digest pins it
    assert entry == {} and pins.table == {}


# --- the orchestrator -------------------------------------------------------------------------------
def test_validate_names_the_bad_place() -> None:
    with pytest.raises(lock.LockError, match=r"rules\.R-1 should be an object"):
        lock.validate({"rules": {"R-1": []}}, "x.lock")
    with pytest.raises(lock.LockError, match=r"rules\.R-1\.targets should be a list"):
        lock._validate_section("x.lock", "rules", {"R-1": {"targets": {}}})
    with pytest.raises(lock.LockError, match=r"scan\.roots should be a list of strings"):
        lock.validate({"scan": {"roots": [1], "exclude": [], "skip": []}}, "x.lock")
    lock.validate({"rules": None, "scan": {"roots": ["src"], "exclude": [], "skip": []}}, "x.lock")
    assert str(lock._malformed("x.lock", "spec", "a string", 3)).startswith(
        "x.lock: malformed lock: spec should be a string, not int 3;")
