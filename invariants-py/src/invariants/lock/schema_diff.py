"""Judging a pinned schema's change: constraint_weakening says why an edit at one path lets the schema
accept more input (a bound raised or removed, an enum widened, a field no longer required, a closed
object opened, a reference re-pointed, ...), or None when it only tightens. pin_json_schema reads a pin
taken from Python annotations as the JSON Schema it describes, so it is judged the same way. Pure."""
from __future__ import annotations

import collections
import typing
from typing import Any

from .contracts import MAX_DEPTH, Change, _json_paths, _short, canonical, same


# --- schema constraints: the rules a codebase already has -----------------------------------------
# A pinned pydantic schema is the model's JSON Schema, so its Field(gt=..., le=..., max_length=...),
# Literal and required constraints are pinned too; so is the JSON Schema of a dataclass or TypedDict
# contract, and the schemas of an OpenAPI contract's operations. A change that lets the schema accept
# more input is a weakening; a change that only tightens it is not. Other changes stay pin changes.
UPPER_BOUNDS = frozenset({"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties", "maxContains"})
LOWER_BOUNDS = frozenset({"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties", "minContains"})
EXACT_CONSTRAINTS = frozenset({"pattern", "format", "multipleOf", "const", "type", "uniqueItems",
                               "additionalProperties", "decimal_places", "max_digits"})
JUDGED_SCHEMAS = frozenset({"pydantic", "jsonschema", "schema", "operations"})
_NAME_SEGMENTS = frozenset({"properties", "$defs", "definitions", "patternProperties"})

# keywords that hold subschemas or field lists: removing one lets more input pass, adding one only
# tightens. What changes inside them is judged where it changes (_json_paths recurses into a dict).
STRUCTURAL = frozenset({"items", "prefixItems", "additionalItems", "unevaluatedItems", "contains", "allOf",
                        "not", "propertyNames", "dependentRequired", "dependentSchemas", "if", "then", "else",
                        "properties", "patternProperties", "unevaluatedProperties"})

# inside these a tighter subschema can accept more input (not; oneOf, where a value that now fails
# one branch may pass as matching exactly one) or the direction is unknown (if)
_FLIPPED = frozenset({"not", "if", "oneOf"})

# JSON Schema 2020-12's unevaluatedProperties / unevaluatedItems judge what no other keyword evaluated,
# looking into the in-place applicators of the same schema: a property or item evaluated by an
# applicator added there is no longer refused (or checked) by them. So while one of them is false or
# a schema, adding or changing any of these is a possible weakening, not a tightening:
# {properties: {a}, unevaluatedProperties: false} plus allOf: [{properties: {b: {}}}] accepts {"a": 1, "b": "x"}.
_UNEVALUATED = ("unevaluatedProperties", "unevaluatedItems")
APPLICATORS = frozenset({"allOf", "anyOf", "oneOf", "if", "then", "else", "dependentSchemas", "$ref",
                         "$dynamicRef", "contains", "prefixItems", "items", "properties", "patternProperties",
                         "additionalProperties", "unevaluatedProperties", "unevaluatedItems"})
_IN_PLACE = frozenset({"allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas"})
_KEYED = frozenset({"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"})

# a bound may be replaced by its exclusive or inclusive twin: removed -> (twin, is an upper bound)
_TWIN_BOUNDS = {"minimum": ("exclusiveMinimum", False), "exclusiveMinimum": ("minimum", False),
                "maximum": ("exclusiveMaximum", True), "exclusiveMaximum": ("maximum", True)}


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _node(root: Any, segs: typing.Sequence[str]) -> Any:
    """The value at a _json_paths path (segments split on "."; "name[i]" indexes a list), or None."""
    node = root
    for seg in segs:
        name, _, idx = seg.partition("[")
        if not isinstance(node, dict) or name not in node:
            return None
        node = node[name]
        for i in [i for i in idx.rstrip("]").split("][") if i]:
            if not isinstance(node, list) or not i.isdigit() or int(i) >= len(node):
                return None
            node = node[int(i)]
    return node


def _local_ref(root: Any, prefix: str, ref: Any) -> Any:
    """The schema a local `#/...` reference names inside the schema at `prefix`, or None."""
    if not (isinstance(ref, str) and ref.startswith("#/")):
        return None
    return _node(_node(root, [prefix]), [p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/")])


def _twin_tightens(key: str, old: Any, node: Any) -> bool:
    """A bound removed while its exclusive or inclusive twin is now at least as tight: `ge=1` to `gt=0`
    on an integer, `le=10` to `lt=10` on any number."""
    twin, upper = _TWIN_BOUNDS[key]
    new = node.get(twin) if isinstance(node, dict) else None
    if not (_number(old) and _number(new)):
        return False
    if key.startswith("exclusive"):                    # x > old became x >= new (or x < old, x <= new)
        return new < old if upper else new > old
    # an integer x > k is x >= k + 1, but only for an integral k: x >= 1.5 is x >= 2, not x > 0.5
    integral = all(isinstance(v, int) or (isinstance(v, float) and v.is_integer()) for v in (old, new))
    slack = 1 if node.get("type") == "integer" and integral else 0
    return new <= old + slack if upper else new >= old - slack


def _closes(v: Any) -> bool:
    """An unevaluated* (or additionalProperties) value that refuses or checks what it judges."""
    return v is not None and v is not True and not same(v, {})


def _own_unevaluated(n: Any) -> tuple[str, Any] | None:
    if isinstance(n, dict):
        for k in _UNEVALUATED:
            if _closes(n.get(k)):
                return k, n[k]
    return None


def unevaluated_scope(root: Any, segs: typing.Sequence[str], inherited: bool = False) -> tuple[str, Any] | None:
    """The unevaluatedProperties or unevaluatedItems keyword, false or a schema, that judges the instance
    of the schema at `segs` (a _json_paths path): on that schema or on one it is an in-place subschema
    of (allOf, anyOf, oneOf, not, if, then, else, dependentSchemas). Inside $defs, which a $ref can
    reach from anywhere, any such keyword in the whole schema counts. inherited: not the schema's own."""
    node = _node(root, segs[:1])
    scope: list[Any] = [node]
    i = 1
    while i < len(segs) and isinstance(node, dict):
        name = segs[i].partition("[")[0]
        step = 2 if name in _KEYED and i + 1 < len(segs) else 1
        if name in ("$defs", "definitions"):
            return _unevaluated_anywhere(_node(root, segs[:1]))
        if name not in _IN_PLACE:
            scope = []                                           # a property or an item: another instance
        node = _node(node, segs[i:i + step])
        scope.append(node)
        i += step
    for n in reversed(scope[:-1] if inherited else scope):
        if (got := _own_unevaluated(n)) is not None:
            return got
    return None


def _unevaluated_anywhere(node: Any, depth: int = 0) -> tuple[str, Any] | None:
    if depth > MAX_DEPTH:
        return None
    got = _own_unevaluated(node)
    for v in (node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()):
        if got is not None:
            break
        got = _unevaluated_anywhere(v, depth + 1)
    return got


def _evaluates_more(segs: typing.Sequence[str], what: str, old: Any, new: Any, old_root: Any,
                    inherited: bool = False) -> str | None:
    """An applicator (what) added or changed in the schema at `segs` while an unevaluated* keyword that
    is false or a schema judges it (unevaluated_scope in the old schema): what the applicator now
    evaluates is no longer refused or checked by that keyword. A false subschema evaluates nothing."""
    if new is None or new is False or old_root is None:
        return None
    got = unevaluated_scope(old_root, segs, inherited)
    if got is None:
        return None
    return (f"{what} {'added' if old is None else 'changed'} while {got[0]} is {_short(got[1])}: what it "
            f"evaluates is no longer refused or checked by {got[0]}")


def _types(v: Any) -> set[str] | None:
    if isinstance(v, str):
        return {v}
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return set(v)
    return None


def _anchors(p: str) -> tuple[str, bool, bool]:
    """A pattern without its ^ and $ anchors, and whether it had each (an escaped \\$ is no anchor)."""
    start = p.startswith("^")
    body = p[1:] if start else p
    escapes = len(body[:-1]) - len(body[:-1].rstrip("\\"))
    end = body.endswith("$") and escapes % 2 == 0
    return (body[:-1] if end else body), start, end


def _pattern_narrower(old: Any, new: Any) -> bool:
    """The new pattern is the old one with ^ or $ anchors added, so it matches less. Only this easy
    case is decided; a pattern with a top-level | is left alone."""
    if not (isinstance(old, str) and isinstance(new, str)) or "|" in old:
        return False
    (bo, so, eo), (bn, sn, en) = _anchors(old), _anchors(new)
    return bo == bn and sn >= so and en >= eo


def constraint_weakening(path: str, old: Any, new: Any, old_root: Any = None, new_root: Any = None) -> str | None:
    """Why a change at `path` of a pinned schema lets it accept more input, or None.

    `path` is as _json_paths reports it inside one schema-table entry ("pydantic.properties.amount.maximum",
    "pydantic.required removed") or contract entry ("operations.createRefund.requestBody..."). `old_root`
    and `new_root` are that entry, for changes judged with their neighbours (a bound and its twin, an
    enum and a const, a $ref and its target). Only JUDGED_SCHEMAS are judged: a pydantic model's JSON
    Schema, a dataclass or TypedDict contract's, and OpenAPI schemas."""
    base, _, suffix = path.partition(" ")                        # "x.enum added" / "x.required removed"
    segs = base.split(".")
    if segs[0] not in JUDGED_SCHEMAS or len(segs) < 2:
        return None
    key, parent = segs[-1], segs[-2]
    names = [x.partition("[")[0] for x in segs]
    flipped = next((n for i, n in enumerate(names[1:-1], 1) if n in _FLIPPED and names[i - 1] not in _NAME_SEGMENTS),
                   None)
    if flipped is not None:
        return f"changed inside {flipped}: there a tighter schema can accept more input"
    if parent == "dependentSchemas" and old is None and new is not None:
        return _evaluates_more(segs[:-2], f"dependentSchemas {key!r}", None, new, old_root)
    if parent in ("dependentRequired", "dependentSchemas"):
        if old is None or suffix == "added":
            return None                                          # a new dependency only tightens
        return f"{parent}.{key} {'removed' if new is None or suffix == 'removed' else 'changed'}: " \
               f"the schema may accept more input"
    if parent in _NAME_SEGMENTS:                                 # a field (or model) entry itself
        if parent == "patternProperties" and old is None and new is not None:
            return _opens_closed(_node(old_root, segs[:-2]), f"patternProperties {key!r} added") \
                or _evaluates_more(segs[:-2], f"patternProperties {key!r}", None, new, old_root, inherited=True)
        if parent == "properties" and old is None and new is not None:
            return _property_opens(_node(old_root, segs[:-2]), _node(new_root, segs[:-2]), key, new,
                                   unevaluated_scope(old_root, segs[:-2], inherited=True))
        if new is None and parent == "properties":
            return f"field {key} removed: its constraints no longer apply"
        if new is None and parent in ("$defs", "definitions"):
            return _def_removed(segs, key, old, old_root, new_root)
        return None
    return _keyword_weakening(segs, suffix, old, new, old_root, new_root)


def _keyword_weakening(segs: list[str], suffix: str, old: Any, new: Any, old_root: Any, new_root: Any) -> str | None:
    """constraint_weakening for a change to a schema keyword (segs[-1]: a bound, enum, required, type,
    $ref, ...) of the object schema at segs[:-1]."""
    why = _keyword_rules(segs, suffix, old, new, old_root, new_root)
    key = segs[-1]
    if why is not None or key not in APPLICATORS or (key == "$ref" and old is not None):
        return why                                               # a $ref to the same body: _ref_changed
    return _evaluates_more(segs[:-1], key, old, new, old_root)


def _keyword_rules(segs: list[str], suffix: str, old: Any, new: Any, old_root: Any, new_root: Any) -> str | None:
    key = segs[-1]
    new_node = _node(new_root, segs[:-1])
    old_node = _node(old_root, segs[:-1])
    if key == "$ref":
        return _ref_changed(segs[0], old, new, old_root, new_root)
    if key == "patternProperties" and old is None and new is not None:
        return _opens_closed(old_node, "patternProperties added")
    if key in ("nullable", "x-nullable"):                       # OpenAPI 3.0 / Swagger 2.0
        return f"{key} {_short(old)} -> {_short(new)}: null now passes" if new is True and old is not True else None
    if key in UPPER_BOUNDS or key in LOWER_BOUNDS:
        return _bound_weakening(key, old, new, new_node)
    if key == "enum":
        return _enum_weakening(suffix, old, new, new_node)
    if key == "required":
        return _required_weakening(suffix, old, new)
    if key == "additionalProperties":
        # false (closed) to anything else, or a schema (or pydantic's absent: ignored) opened up. A
        # schema to a schema is compared inside it (_json_paths recurses), so here it is conservative.
        if old is True or new is False or same(old, new):
            return None
        if old is None and not _model_object(segs, old_node):
            return None
        return f"additionalProperties {_short(old)} -> {_short(new)}: extra properties now pass"
    if key == "type":
        return _type_weakening(suffix, old, new)
    if key == "pattern" and _pattern_narrower(old, new):
        return None
    if key in EXACT_CONSTRAINTS:
        if old is None:
            return None                                          # a new constraint only tightens
        if key == "uniqueItems" and new is True:
            return None
        return f"{key} {'removed' if new is None else 'changed'}: the schema may accept more input"
    if key in ("anyOf", "oneOf") and old is not None:
        return f"{key} changed: the schema may accept more input"
    if key in STRUCTURAL and old is not None:
        if new is None:
            return f"{key} removed: what it checked no longer applies"
        if key == "allOf" and isinstance(old, list) and isinstance(new, list) \
                and all(any(same(x, y) for y in new) for x in old):
            return None                                          # more schemas, all of which must hold
        if key == "prefixItems" and isinstance(old, list) and isinstance(new, list) and same(old, new[:len(old)]):
            return _prefix_items_extended(old, new, old_node)
        return f"{key} changed: the schema may accept more input"
    return None


def _bound_weakening(key: str, old: Any, new: Any, new_node: Any) -> str | None:
    """A numeric, length or count bound raised (an upper one) or lowered (a lower one), or removed
    unless its exclusive or inclusive twin now holds at least as tightly."""
    upper = key in UPPER_BOUNDS
    if old is True and new is not True and key in ("exclusiveMaximum", "exclusiveMinimum"):
        # Draft 4 / OpenAPI 3.0: a boolean that makes maximum (minimum) exclusive, now inclusive
        return f"{key} true -> {_short(new)}: the bound itself now passes"
    if new is None:
        if key in _TWIN_BOUNDS and _twin_tightens(key, old, new_node):
            return None                                          # ge=1 -> gt=0 on an integer
        return f"{key} removed: no {'upper' if upper else 'lower'} bound"
    if _number(old) and _number(new) and (new > old if upper else new < old):
        return f"{key} {'raised: larger' if upper else 'lowered: smaller'} values pass"
    return None


def _enum_weakening(suffix: str, old: Any, new: Any, new_node: Any) -> str | None:
    """An enum widened or removed, unless it was narrowed to a const holding one of its values."""
    if suffix == "" and new is None and isinstance(new_node, dict) and "const" in new_node \
            and isinstance(old, list) and any(same(new_node["const"], v) for v in old):
        return None                                              # narrowed to one of its values
    if suffix == "added" or (suffix == "" and old is not None):
        return "enum widened: more values pass" if new is not None else "enum removed: any value passes"
    return None


def _model_object(segs: list[str], old_node: Any) -> bool:
    """Whether the object schema at segs[:-1] is a model's (a root, a $defs entry, or one with
    properties), where an absent additionalProperties means extras are ignored. Absent on a map (`dict`
    with no value type: an object schema with no properties) already let any value pass, so a value
    schema only tightens it (`dict` / `dict[str, Any]` -> `dict[str, int]`)."""
    return len(segs) == 2 or (len(segs) >= 3 and segs[-3] in ("$defs", "definitions")) or (
        isinstance(old_node, dict) and ("properties" in old_node or "patternProperties" in old_node))


def _prefix_items_extended(old: list[Any], new: list[Any], old_node: Any) -> str | None:
    """prefixItems made longer: the new positions were checked by items, if the old schema had one."""
    rest = old_node.get("items") if isinstance(old_node, dict) else None
    if rest is None or rest is True or same(rest, {}) or all(same(x, rest) for x in new[len(old):]):
        return None
    return (f"prefixItems extended to {len(new)}: positions {len(old)}.. were checked by items "
            f"{_short(rest)}, and are now checked only by the new entries")


def _opens_closed(node: Any, what: str) -> str | None:
    """Pattern properties added to an object schema: a tightening when other properties were
    allowed anyway, a weakening when additionalProperties (or unevaluatedProperties) closed or
    constrained them, since a property that matches the pattern is no longer checked by it."""
    if not isinstance(node, dict):
        return None
    for k in ("additionalProperties", "unevaluatedProperties"):
        v = node.get(k)
        if v is not None and v is not True and not same(v, {}):
            return f"{what} while {k} is {_short(v)}: properties matching it now pass without that check"
    return None


def _property_opens(old_node: Any, new_node: Any, key: str, schema: Any,
                    inherited: tuple[str, Any] | None = None) -> str | None:
    """A property added to an object schema whose additionalProperties (or unevaluatedProperties, its
    own or `inherited` from a schema it is an in-place subschema of: unevaluated_scope) was false or
    a schema: the property was rejected (or checked by that schema) and now passes by its own schema.
    Not a weakening when other properties were allowed anyway, when the old schema for extras was the
    same, or, for a closed schema, when the new property is required and its schema constrains its
    value (every old instance lacked it, so none of them passes now)."""
    if not isinstance(old_node, dict):
        return None
    own = [(k, old_node.get(k)) for k in ("additionalProperties", "unevaluatedProperties")]
    for k, v in own + ([inherited] if inherited is not None and inherited[0] == "unevaluatedProperties" else []):
        if v is None or v is True or same(v, {}) or same(v, schema):
            continue
        required = isinstance(new_node, dict) and isinstance(new_node.get("required"), list) \
            and key in new_node["required"]
        constrained = isinstance(schema, dict) and bool(schema) and schema is not True
        if v is False and required and constrained:
            continue
        return (f"field {key} added while {k} is {_short(v)}: inputs with {key} "
                f"{'were rejected' if v is False else 'were checked by that schema'} and now pass")
    return None


def _required_weakening(suffix: str, old: Any, new: Any) -> str | None:
    if isinstance(old, bool) or isinstance(new, bool):          # an OpenAPI parameter or request body
        return "no longer required" if old is True and new is not True else None
    if suffix == "removed" or (suffix == "" and old is not None and new is None):
        return "a field is no longer required"
    if suffix == "" and isinstance(old, list) and isinstance(new, list):
        gone = sorted({str(x) for x in old} - {str(x) for x in new})
        return f"{', '.join(gone)} no longer required" if gone else None   # reordered, deduplicated or added
    if suffix == "" and old is not None and new is not None:
        return "the required fields changed"
    return None


def _type_weakening(suffix: str, old: Any, new: Any) -> str | None:
    if suffix == "removed" or (suffix == "" and old is None):
        return None                                              # fewer types pass, or a type is added
    a, b = _types(old), _types(new)
    if suffix == "" and a is not None and b is not None and all(
            t in a or (t == "integer" and "number" in a) for t in b):
        return None                                              # number -> integer
    return f"type {_short(old)} -> {_short(new)}: the schema may accept more input"


def _ref_changed(prefix: str, old: Any, new: Any, old_root: Any, new_root: Any) -> str | None:
    """A reference removed (the value is now unconstrained, as `{}` or Any) or pointed at another schema."""
    if old is None:
        return None                                              # a reference replaces an inline schema
    if new is None:
        return f"reference {old} removed: the value is no longer checked against it"
    a, b = _local_ref(old_root, prefix, old), _local_ref(new_root, prefix, new)
    if a is not None and b is not None and same(a, b):
        return None                                              # the same schema under a new name
    return f"reference {old} -> {new}: the value is checked against a different schema"


def _def_removed(segs: typing.Sequence[str], key: str, old: Any, old_root: Any, new_root: Any) -> str | None:
    """A $defs entry removed while the old schema referred to it, and no new entry holds the same body."""
    ref = "#/" + "/".join(s.replace("~", "~0").replace("/", "~1") for s in segs[1:])
    found: list[str] = []
    _local_refs(_node(old_root, segs[:1]), found)
    if old_root is not None and ref not in found:
        return None
    defs = _node(new_root, segs[:-1])
    if isinstance(defs, dict) and any(same(old, v) for v in defs.values()):
        return None
    return f"schema {key} removed while it was referenced: what referred to it is no longer checked"


def _local_refs(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            found.append(node["$ref"])
        for v in node.values():
            _local_refs(v, found)
    elif isinstance(node, list):
        for v in node:
            _local_refs(v, found)


_PIN_SCALARS = {"null": {"type": "null"}, "int": {"type": "integer"}, "float": {"type": "number"},
                "str": {"type": "string"}, "bool": {"type": "boolean"}, "Any": {}, "object": {}}
_PIN_ARRAYS = frozenset({"list", "set", "frozenset", "tuple", "Sequence", "MutableSequence", "Set",
                         "AbstractSet", "MutableSet", "Iterable", "Collection", "deque"})
_PIN_MAPS = frozenset({"dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict"})


def pin_json_schema(entry: Any) -> Any:
    """A schema-table entry pinned from Python annotations (a {field: type} mapping, a dataclass or
    TypedDict a signature names, an Enum, a bare type) as the JSON Schema it describes, so it is judged
    like a pydantic schema. None for an entry that is already a JSON Schema (pydantic, jsonschema,
    OpenAPI) or that holds neither fields nor a type."""
    if not isinstance(entry, dict) or any(k in entry for k in JUDGED_SCHEMAS):
        return None
    defs = {_pin_def(k): _pin_type(v) for k, v in (entry.get("$defs") or {}).items()} \
        if isinstance(entry.get("$defs"), dict) else {}
    if isinstance(entry.get("fields"), dict):
        top = _pin_type({k: v for k, v in entry.items() if k in ("fields", "required")})
    elif "type" in entry:
        top = _pin_type(entry["type"])
    else:
        return None
    return {**top, "$defs": defs} if defs else top


def _pin_def(ref: str) -> str:
    return ref.replace(".", ":")                       # a path segment of _json_paths holds no "."


def _pin_type(t: Any, depth: int = 0) -> Any:
    """One `_type_schema` value as JSON Schema; what has no JSON form is an x-python-type."""
    if depth > MAX_DEPTH:
        return {"x-python-type": "truncated"}
    d = depth + 1
    if isinstance(t, str):
        return dict(_PIN_SCALARS[t]) if t in _PIN_SCALARS else {"x-python-type": t}
    if not isinstance(t, dict):
        return {"x-python-type": repr(t)}
    if isinstance(t.get("fields"), dict):
        out: dict[str, Any] = {"type": "object", "properties": {str(k): _pin_type(v, d) for k, v in t["fields"].items()}}
        if isinstance(t.get("required"), list):
            out["required"] = t["required"]
        return out
    if "literal" in t and isinstance(t["literal"], list):
        return {"enum": t["literal"]}
    if "enum" in t and isinstance(t.get("values"), list):
        return {"enum": t["values"]}
    if isinstance(t.get("anyOf"), list):
        return {"anyOf": [_pin_type(x, d) for x in t["anyOf"]]}
    if isinstance(t.get("$ref"), str) and "truncated" not in t:
        return {"$ref": f"#/$defs/{_pin_def(t['$ref'])}"}
    if isinstance(t.get("generic"), str) and isinstance(t.get("args"), list):
        name, args = t["generic"], [_pin_type(x, d) for x in t["args"] if x != "Ellipsis"]
        if name in _PIN_ARRAYS:
            if name == "tuple" and len(args) > 1:
                return {"type": "array", "prefixItems": args, "maxItems": len(args)}
            return {"type": "array", **({"items": args[0]} if args else {})}
        if name in _PIN_MAPS:
            return {"type": "object", **({"additionalProperties": args[1]} if len(args) == 2 else {})}
        return {"x-python-type": name, "x-args": args}
    return {"x-python-type": canonical(t).decode()}


def _schema_changes(refs: list[tuple[str, str, str, str]], old_tab: dict[str, Any],
                    new_tab: dict[str, Any]) -> list[Change]:
    """Each changed schema once, with the entries that use it."""
    groups: dict[tuple[str, str], list[str]] = collections.OrderedDict()
    for user, _, da, db in refs:
        groups.setdefault((da, db), []).append(user)
    out: list[Change] = []
    done: set[tuple[str, str]] = set()
    work = collections.deque(groups.items())
    while work:
        (da, db), users = work.popleft()
        if (da, db) in done:
            continue
        done.add((da, db))
        sa, sb = old_tab.get(da), new_tab.get(db)
        name = next((s.get("name") for s in (sb, sa) if isinstance(s, dict) and s.get("name")), db[:19])
        where = f"schema {name}"
        used = "used by " + ", ".join(users)
        if not isinstance(sa, dict) or not isinstance(sb, dict):
            out.append(Change(where, "schema", da, db, note=f"{used}; the stored lock does not hold the old "
                                                            f"schema, so the change cannot be shown"))
            continue
        nested: list[tuple[str, str, str]] = []
        diffs = _json_paths(sa, sb, "", nested)
        out += _schema_entry_changes(where, used, sa, sb, diffs)
        for p, xa, xb in nested:
            work.append(((xa, xb), [f"{where} {p}"]))
        if not diffs and not nested:
            out.append(Change(where, "schema", da, db, note=used))
    return out


def _schema_entry_changes(where: str, used: str, sa: dict[str, Any], sb: dict[str, Any],
                          diffs: list[tuple[str, Any, Any]]) -> list[Change]:
    """The leaf differences of one schema-table entry, each judged by constraint_weakening; a pin taken
    from Python annotations is also judged as the JSON Schema it describes."""
    out: list[Change] = []
    for i, (p, x, y) in enumerate(diffs):
        why = constraint_weakening(p, x, y, sa, sb)
        note = "; ".join(n for n in (why, used if i == 0 else "") if n)
        out.append(Change(where, p, x, y, weakened=why is not None, note=note))
    ja, jb = pin_json_schema(sa), pin_json_schema(sb)
    if diffs and ja is not None and jb is not None:
        # a {field: type} mapping, a dataclass or TypedDict a signature names, an Enum: judged as the
        # JSON Schema its pin describes, so a widened field type or a new enum member is WEAKENED
        ra, rb = {"jsonschema": ja}, {"jsonschema": jb}
        for p, x, y in _json_paths(ra, rb):
            why = constraint_weakening(p, x, y, ra, rb)
            if why is not None:
                out.append(Change(where, p, x, y, weakened=True, note=f"{why} (the pin judged as JSON Schema)"))
    return out


def _param_schema_changes(where: str, old: Any, new: Any) -> list[Change]:
    """The constraints Annotated metadata puts on a signature's parameters, judged like a pinned
    schema: a constraint removed or loosened is WEAKENED, one added or tightened is not."""
    a, b = (old if isinstance(old, dict) else {}), (new if isinstance(new, dict) else {})
    out: list[Change] = []
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name), b.get(name)
        if same(x, y):
            continue
        what = f"param_schemas.{name}"
        if y is None:
            out.append(Change(where, what, x, None, True, f"the constraints on {name} were removed"))
        elif x is None:
            out.append(Change(where, what, None, y, note=f"constraints added on {name}"))
        else:
            ra, rb = {"jsonschema": x}, {"jsonschema": y}
            for p, xv, yv in _json_paths(ra, rb):
                why = constraint_weakening(p, xv, yv, ra, rb)
                out.append(Change(where, f"param_schemas.{name}{p[len('jsonschema'):]}", xv, yv,
                                  why is not None, why or ""))
    return out
