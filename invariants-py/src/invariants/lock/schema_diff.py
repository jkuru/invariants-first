"""Judging a pinned schema's change: constraint_weakening says why an edit at one path lets the schema
accept more input (a bound raised or removed, an enum widened, a field no longer required, a closed
object opened, a reference re-pointed, ...), or None when it only tightens. pin_json_schema reads a pin
taken from Python annotations as the JSON Schema it describes, so it is judged the same way. Pure."""
from __future__ import annotations

import collections
import typing
import urllib.parse
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
                        "not", "propertyNames", "dependentRequired", "dependentSchemas", "dependencies", "if",
                        "then", "else", "properties", "patternProperties", "unevaluatedProperties"})

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
_KEYED = frozenset({"properties", "patternProperties", "dependentSchemas", "dependencies", "$defs", "definitions"})

# a bound may be replaced by its exclusive or inclusive twin: removed -> (twin, is an upper bound)
_TWIN_BOUNDS = {"minimum": ("exclusiveMinimum", False), "exclusiveMinimum": ("minimum", False),
                "maximum": ("exclusiveMaximum", True), "exclusiveMaximum": ("maximum", True)}


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _node(root: Any, segs: typing.Sequence[Any]) -> Any:
    """The value at a _json_paths path (segments split on "."; "name[i]" indexes a list), or None."""
    got = _at(root, _as_keys(segs))
    return None if got is MISSING else got


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


def unevaluated_scope(root: Any, segs: typing.Sequence[Any], inherited: bool = False) -> tuple[str, Any] | None:
    """The unevaluatedProperties or unevaluatedItems keyword, false or a schema, that judges the instance
    of the schema at `segs` (a _json_paths path): on that schema or on one it is an in-place subschema
    of (allOf, anyOf, oneOf, not, if, then, else, dependentSchemas). Inside $defs, which a $ref can
    reach from anywhere, any such keyword in the whole schema counts. inherited: not the schema's own."""
    keys = _as_keys(segs)
    node = _at(root, keys[:1])
    scope: list[Any] = [node]
    i = 1
    while i < len(keys) and isinstance(node, dict):
        name = keys[i]
        step = 2 if i + 1 < len(keys) and (name in _KEYED or isinstance(keys[i + 1], int)) else 1
        if name in ("$defs", "definitions"):
            return _unevaluated_anywhere(_at(root, keys[:1]))
        if name not in _IN_PLACE:
            scope = []                                           # a property or an item: another instance
        node = _at(node, keys[i:i + step])
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


def _evaluates_more(segs: typing.Sequence[Any], what: str, old: Any, new: Any, old_root: Any,
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


# --- where a change sits, and which way it pulls ----------------------------------------------------
# A change is judged at a schema keyword (a bound, enum, required, $ref, ...) of the subschema that
# holds it, or, for a subschema replaced as a whole (allOf[0]: {...} -> true, properties.a -> true), at
# that subschema's place. Below const, enum, default and examples nothing is a schema: the value is
# compared whole. Each place has a polarity: +1 where a looser subschema lets more input through, -1
# under not (a tighter subschema there lets more through), both under oneOf, if and a contains with
# maxContains, where either direction can. A $ref passes its site's polarity on to its target, so a
# $defs entry used under not is judged the other way round, and one used both ways in both.
class _Missing:
    """An absent key (`const: null` is present, and differs from absent)."""

    def __repr__(self) -> str:
        return "(absent)"


MISSING: Any = _Missing()
POS: frozenset[int] = frozenset({1})
NEG: frozenset[int] = frozenset({-1})
BOTH: frozenset[int] = frozenset({1, -1})

_DATA_KEYWORDS = frozenset({"const", "enum", "default", "examples", "example"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})            # and a Draft 7 `items` list
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
                          "dependencies"})
_ONE_SCHEMA = frozenset({"not", "if", "then", "else", "items", "additionalItems", "contains", "propertyNames",
                         "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contentSchema"})
_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")
_ID_KEYS = frozenset({"$id", "$anchor", "$dynamicAnchor", "$recursiveAnchor"})
_DOC_HEADS = ("pydantic", "jsonschema", "schema")
_DRAFT4_DIALECTS = frozenset({"openapi-3.0", "swagger-2.0"})
# keywords that constrain nothing: a Draft 7 $ref beside only these hides no constraint
_ANNOTATIONS = frozenset({"title", "description", "$comment", "examples", "example", "default", "readOnly",
                          "writeOnly", "deprecated", "$schema", "$id", "id", "$defs", "definitions"})


class _Absolute(str):
    """A verdict that holds at any polarity: a reference that now points elsewhere or nowhere."""


def _compose(a: frozenset[int], b: frozenset[int]) -> frozenset[int]:
    return frozenset(x * y for x in a for y in b)


def _at(root: Any, keys: typing.Sequence[Any]) -> Any:
    """The value at a key path (str keys into dicts, int indexes into lists), or MISSING."""
    node = root
    for k in keys:
        if isinstance(k, int):
            if not isinstance(node, list) or k >= len(node):
                return MISSING
        elif not isinstance(node, dict) or k not in node:
            return MISSING
        node = node[k]
    return node


def _as_keys(segs: typing.Sequence[Any]) -> list[Any]:
    """Path segments as keys: "allOf[0]" (a segment of a split _json_paths path) is "allOf", 0."""
    out: list[Any] = []
    for seg in segs:
        if not isinstance(seg, str):
            out.append(seg)
            continue
        name, _, idx = seg.partition("[")
        out.append(name)
        out += [int(i) if i.isdigit() else i for i in idx.rstrip("]").split("][") if i]
    return out


def _keys(ra: Any, rb: Any, path: str) -> list[Any] | None:
    """A _json_paths path followed in the old and new entries: the key that matches longest at each
    step, so a property or pattern holding "." or "[" is one key. None when the path leaves both."""
    out: list[Any] = []
    nodes, rest = [ra, rb], path
    while rest:
        names = {k for n in nodes if isinstance(n, dict) for k in n if isinstance(k, str)}
        best = max((k for k in names if rest.startswith(k) and rest[len(k):len(k) + 1] in ("", ".", "[")),
                   key=len, default=None)
        if best is None:
            return None
        out.append(best)
        nodes = [n.get(best) if isinstance(n, dict) else None for n in nodes]
        rest = rest[len(best):]
        while rest.startswith("["):
            j = rest.find("]")
            if j < 0 or not rest[1:j].isdigit():
                return None
            i = int(rest[1:j])
            out.append(i)
            nodes = [n[i] if isinstance(n, list) and i < len(n) else None for n in nodes]
            rest = rest[j + 1:]
        if rest.startswith("."):
            rest = rest[1:]
        elif rest:
            return None
    return out


def _schema_roots(entry: Any, head: str) -> list[tuple[Any, ...]]:
    """Where the schemas of an entry start: the entry's schema (pydantic, jsonschema, an OpenAPI
    component's schema), or each parameter, request body and response schema of its operations."""
    if head in _DOC_HEADS:
        return [(head,)]
    return [_media(entry, r) for r in _operation_roots(entry)]


def _media(entry: Any, root: tuple[Any, ...]) -> tuple[Any, ...]:
    """A media type's schema: the pin holds the schema itself, an unpinned document a Media Type
    Object around it ({schema: ...})."""
    node = _at(entry, root)
    if isinstance(node, dict) and "schema" in node and set(node) <= {"schema", "example", "examples", "encoding"}:
        return (*root, "schema")
    return root


def _operation_roots(entry: Any) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    ops = entry.get("operations") if isinstance(entry, dict) else None
    for op, body in (ops.items() if isinstance(ops, dict) else ()):
        if not isinstance(body, dict):
            continue
        rb = body.get("requestBody")
        content = rb.get("content") if isinstance(rb, dict) else None
        out += [("operations", op, "requestBody", "content", mt) for mt in (content if isinstance(content, dict) else ())]
        params = body.get("parameters")
        for i, prm in enumerate(params if isinstance(params, list) else []):
            if isinstance(prm, dict) and "schema" in prm:
                out.append(("operations", op, "parameters", i, "schema"))
            if isinstance(prm, dict) and isinstance(prm.get("content"), dict):
                out += [("operations", op, "parameters", i, "content", mt) for mt in prm["content"]]
        responses = body.get("responses")
        for code, media in (responses.items() if isinstance(responses, dict) else ()):
            out += [("operations", op, "responses", code, mt) for mt in (media if isinstance(media, dict) else ())]
    return out


def _dialect(entry: Any, head: str) -> str:
    """draft4 (Draft 4, OpenAPI 3.0, Swagger 2.0: exclusiveMaximum is a flag on maximum, `id`),
    draft7 (Draft 6 and 7) or 2020 (2019-09 and 2020-12, pydantic's): from the pin's `dialect`
    (an OpenAPI version), the schema's $schema, or a boolean exclusiveMaximum/Minimum."""
    if isinstance(entry, dict) and entry.get("dialect") in _DRAFT4_DIALECTS:
        return "draft4"
    doc = entry.get(head) if isinstance(entry, dict) and head in _DOC_HEADS else None
    uri = doc.get("$schema") if isinstance(doc, dict) else None
    if isinstance(uri, str) and ("draft-04" in uri or "draft-03" in uri):
        return "draft4"
    if _boolean_exclusive(entry):
        return "draft4"
    if isinstance(uri, str) and ("draft-06" in uri or "draft-07" in uri):
        return "draft7"
    return "2020"


def _boolean_exclusive(node: Any, depth: int = 0) -> bool:
    if depth > 4 * MAX_DEPTH:
        return False
    if isinstance(node, dict):
        if any(isinstance(node.get(k), bool) for k in ("exclusiveMaximum", "exclusiveMinimum")):
            return True
        return any(_boolean_exclusive(v, depth + 1) for k, v in node.items() if k not in _DATA_KEYWORDS)
    if isinstance(node, list):
        return any(_boolean_exclusive(v, depth + 1) for v in node)
    return False


def _subschemas(keys: tuple[Any, ...], node: dict[str, Any]) -> typing.Iterator[tuple[tuple[Any, ...], str]]:
    """The subschema places of a schema object: (their keys, the keyword holding them)."""
    for k, v in node.items():
        if k in _ONE_SCHEMA and isinstance(v, (dict, bool)):
            yield (*keys, k), k
        elif (k in _SCHEMA_LISTS or k == "items") and isinstance(v, list):
            yield from (((*keys, k, i), k) for i in range(len(v)))
        elif k in _SCHEMA_MAPS and isinstance(v, dict):
            yield from (((*keys, k, name), k) for name, s in v.items() if isinstance(s, (dict, bool)))


def _join(base: str, ref: str) -> str:
    return urllib.parse.urljoin(base, ref) if base else ref


class _Doc:
    """One side of a pinned entry read as JSON Schema: its resources ($id), anchors, the references
    between its subschemas, and the polarity each subschema is evaluated at."""

    def __init__(self, entry: Any, head: str, dialect: str) -> None:
        self.entry, self.dialect = entry, dialect
        self.roots = _schema_roots(entry, head)
        self.base: dict[tuple[Any, ...], str] = {}
        self.resources: dict[str, tuple[Any, ...]] = {}
        self.anchors: dict[tuple[str, str], tuple[Any, ...]] = {}
        self.dynamic: dict[str, list[tuple[Any, ...]]] = {}
        self.recursive: list[tuple[Any, ...]] = []
        self.sites: list[tuple[tuple[Any, ...], str, str]] = []        # (keys, keyword, reference)
        for r in self.roots:
            self._index(r)
        if head in _DOC_HEADS and self.roots:
            self.resources.setdefault("", self.roots[0])
        self.targets: list[tuple[Any, ...]] = []
        self.dangling = False                       # a reference that names nothing here
        for keys, kw, ref in self.sites:
            got = self.resolve(keys, kw, ref)
            self.dangling |= got is None
            self.targets += got or []
        self.visited: dict[tuple[Any, ...], set[int]] = {}
        self.unresolved: set[int] = set()             # polarities of references that name nothing here
        self._walk()

    def _index(self, root: tuple[Any, ...]) -> None:
        work: list[tuple[tuple[Any, ...], str]] = [(root, "")]
        while work:
            keys, base = work.pop()
            node = _at(self.entry, keys)
            if not isinstance(node, dict):
                continue
            ident = node.get("id") if self.dialect == "draft4" else node.get("$id")
            if isinstance(ident, str):
                url, frag = urllib.parse.urldefrag(_join(base, ident))
                if url or not ident.startswith("#"):
                    base = url
                    self.resources[url] = keys
                if frag and not frag.startswith("/"):
                    self.anchors[(base, frag)] = keys            # Draft 6/7 "$id": "#name"
            for k in ("$anchor", "$dynamicAnchor"):
                if isinstance(node.get(k), str):
                    self.anchors[(base, node[k])] = keys
            if isinstance(node.get("$dynamicAnchor"), str):
                self.dynamic.setdefault(node["$dynamicAnchor"], []).append(keys)
            if node.get("$recursiveAnchor") is True:
                self.recursive.append(keys)
            self.base[keys] = base
            self.sites += [(keys, k, node[k]) for k in _REF_KEYS if isinstance(node.get(k), str)]
            work += [(ck, base) for ck, _ in _subschemas(keys, node)]

    def base_at(self, keys: tuple[Any, ...]) -> str:
        for n in range(len(keys), -1, -1):
            if keys[:n] in self.base:
                return self.base[keys[:n]]
        return ""

    def resolve(self, keys: tuple[Any, ...], kw: str, ref: str) -> list[tuple[Any, ...]] | None:
        """The subschemas a reference at `keys` may name ([] for another schema-table entry, judged on
        its own), or None when it names nothing in this entry."""
        if ref.startswith("sha256:"):
            return []
        url, frag = urllib.parse.urldefrag(_join(self.base_at(keys), ref))
        at = self.resources.get(url)
        if at is None:
            return None
        frag = urllib.parse.unquote(frag)
        target: tuple[Any, ...] | None = at
        if frag.startswith("/"):
            for tok in frag[1:].split("/"):
                tok = tok.replace("~1", "/").replace("~0", "~")
                node = _at(self.entry, target or ())
                if isinstance(node, list) and tok.isdigit():
                    target = (*(target or ()), int(tok))
                elif isinstance(node, dict) and tok in node:
                    target = (*(target or ()), tok)
                else:
                    return None
        elif frag:
            target = self.anchors.get((url, frag))
        if target is None:
            return None
        out = [target]
        node = _at(self.entry, target)
        if kw == "$dynamicRef" and isinstance(node, dict) and node.get("$dynamicAnchor") == frag:
            out += self.dynamic.get(frag, [])
        if kw == "$recursiveRef":
            out += self.recursive
        return out

    def _walk(self) -> None:
        work: list[tuple[tuple[Any, ...], int]] = [(r, 1) for r in self.roots]
        while work:
            keys, p = work.pop()
            seen = self.visited.setdefault(keys, set())
            if p in seen:
                continue
            seen.add(p)
            node = _at(self.entry, keys)
            if not isinstance(node, dict):
                continue
            for kw in _REF_KEYS:
                ref = node.get(kw)
                if isinstance(ref, str):
                    got = self.resolve(keys, kw, ref)
                    if got is None:
                        self.unresolved.add(p)
                    work += [(t, p) for t in got or []]
            for ck, kw in _subschemas(keys, node):
                if kw not in ("$defs", "definitions"):
                    work += [(ck, q) for q in _step(kw, node, p)]

    def polarity(self, keys: typing.Sequence[Any]) -> frozenset[int]:
        """The polarities the subschema at `keys` is evaluated at; a reference that names nothing here
        may name it too. A subschema nothing evaluates (an unused $defs entry) counts as +1."""
        got = self.visited.get(tuple(keys), set()) | self.unresolved
        return frozenset(got) or POS

    def root_of(self, keys: typing.Sequence[Any]) -> tuple[Any, ...] | None:
        return max((r for r in self.roots if tuple(keys[:len(r)]) == r), key=len, default=None)


def _step(kw: str, node: dict[str, Any], p: int) -> tuple[int, ...]:
    """The polarities a subschema under `kw` of `node` is evaluated at, inside a place at `p`."""
    if kw == "not":
        return (-p,)
    if kw in ("if", "oneOf") or (kw == "contains" and "maxContains" in node):
        return (p, -p)
    return (p,)


_DOCS: collections.OrderedDict[tuple[int, str], tuple[Any, bytes, _Doc]] = collections.OrderedDict()


def _doc(entry: Any, head: str, dialect: str) -> _Doc:
    """The _Doc of an entry, kept for the next change of the same entry (each leaf is judged apart)."""
    key, body = (id(entry), head + dialect), canonical(entry)
    got = _DOCS.get(key)
    if got is not None and got[0] is entry and got[1] == body:
        return got[2]
    doc = _Doc(entry, head, dialect)
    _DOCS[key] = (entry, body, doc)
    while len(_DOCS) > 8:
        _DOCS.popitem(last=False)
    return doc


def _docs(ra: Any, rb: Any, head: str) -> tuple[_Doc, _Doc]:
    dialect = "draft4" if "draft4" in (_dialect(ra, head), _dialect(rb, head)) else _dialect(rb, head)
    return _doc(ra, head, dialect), _doc(rb, head, dialect)


def ref_polarity(path: str, old_root: Any, new_root: Any) -> frozenset[int]:
    """The polarity of the schema-table reference at `path` of an entry (a pinned component a schema
    names under not, say): the entry it names is judged at that polarity."""
    head = path.partition(".")[0]
    if head not in JUDGED_SCHEMAS or old_root is None or new_root is None:
        return POS
    keys = _keys(old_root, new_root, path)
    if keys is None:
        return BOTH
    da, db = _docs(old_root, new_root, head)
    return da.polarity(keys) | db.polarity(keys)


def _locate(keys: list[Any], da: _Doc, db: _Doc) -> tuple[str, tuple[Any, ...], Any, Any] | None:
    """Where a change at `keys` is judged: ("kw", node, keyword, None) for a keyword of the subschema at
    node (a change below a value or an unknown keyword is the keyword's), ("entry", node, map, name)
    for a properties/patternProperties/$defs/dependentSchemas/dependencies entry added, removed or
    replaced by a non-object, ("item", node, list keyword, index) for an allOf/anyOf/oneOf/prefixItems
    entry replaced so, ("slot", node, keyword, None) for a not/items/contains/... subschema replaced
    so, ("root", root, None, None) for the whole schema. None outside any schema."""
    root = da.root_of(keys) or db.root_of(keys)
    if root is None:
        return None
    if len(keys) == len(root):
        return ("root", root, None, None)
    ra, rb = da.entry, db.entry
    nk, i = root, len(root)
    while True:
        k = keys[i]
        if not isinstance(k, str):
            return None
        last = i + 1 == len(keys)
        nxt = None if last else keys[i + 1]
        if k in _SCHEMA_MAPS:
            if last or not isinstance(nxt, str):
                return ("kw", nk, k, None)
            sk = (*nk, k, nxt)
            if i + 2 == len(keys) or not (isinstance(_at(ra, sk), dict) and isinstance(_at(rb, sk), dict)):
                return ("entry", nk, k, nxt)
        elif k in _SCHEMA_LISTS or (k == "items" and isinstance(nxt, int)):
            if not isinstance(nxt, int):
                return ("kw", nk, k, None)
            sk = (*nk, k, nxt)
            va, vb = _at(ra, sk), _at(rb, sk)
            if va is MISSING or vb is MISSING:
                return ("kw", nk, k, None)
            if i + 2 == len(keys) or not (isinstance(va, dict) and isinstance(vb, dict)):
                return ("item", nk, k, nxt)
        elif k in _ONE_SCHEMA:
            sk = (*nk, k)
            va, vb = _at(ra, sk), _at(rb, sk)
            if va is MISSING or vb is MISSING:
                return ("kw", nk, k, None)
            if last or not (isinstance(va, dict) and isinstance(vb, dict)):
                return ("slot", nk, k, None)
        else:
            return ("kw", nk, k, None)
        nk, i = sk, len(sk)


def _loose(v: Any) -> bool:
    return v is True or (isinstance(v, dict) and not v)


def _schema_replaced(what: str, old: Any, new: Any) -> str | None:
    """A subschema replaced as a whole (by `true` or `false`, or a schema by a list): looser unless the
    old one accepted anything or the new one accepts nothing."""
    if same(old, new) or _loose(old) or new is False:
        return None
    return f"{what} {_short(old)} -> {_short(new)}: the schema may accept more input"


def _values(node: Any) -> list[Any] | None:
    """The values const and enum together allow at a schema object, or None for any value."""
    if not isinstance(node, dict):
        return None
    out: list[Any] | None = [node["const"]] if "const" in node else None
    if isinstance(node.get("enum"), list):
        enum = node["enum"]
        out = list(enum) if out is None else [v for v in out if any(same(v, x) for x in enum)]
    return out


def _values_weakening(key: str, on: Any, nn: Any) -> str | None:
    """const or enum changed (compared whole, a structured value included): a weakening when a value
    passes that did not (a const or an enum removed counts), judged with its neighbour, so an enum
    narrowed to a const that was one of its values is a tightening."""
    a, b = _values(on), _values(nn)
    if a is None:
        return None
    if b is None:
        return f"{key} removed: any value passes"
    extra = [v for v in b if not any(same(v, x) for x in a)]
    if not extra:
        return None
    return "enum widened: more values pass" if key == "enum" else \
        f"{key} changed: {_short(extra[0])} now passes"


def _draft4_bound(key: str, on: Any, nn: Any) -> str | None:
    """Draft 4 / OpenAPI 3.0: exclusiveMaximum (exclusiveMinimum) is a flag on maximum (minimum), and
    on its own (a number included) bounds nothing. The bound and its flag are judged together."""
    upper = key in ("maximum", "exclusiveMaximum")
    mk, ek = ("maximum", "exclusiveMaximum") if upper else ("minimum", "exclusiveMinimum")

    def bound(n: Any) -> tuple[Any, bool] | None:
        m = n.get(mk) if isinstance(n, dict) else None
        return (m, bool(n.get(ek))) if _number(m) else None
    a, b = bound(on), bound(nn)
    if a is None:
        return None
    if b is None:
        return f"{mk} removed: no {'upper' if upper else 'lower'} bound (in Draft 4 and OpenAPI 3.0 " \
               f"{ek} only qualifies {mk})"
    (m0, e0), (m1, e1) = a, b
    if (m1 > m0 if upper else m1 < m0) or (m1 == m0 and e0 and not e1):
        return f"{mk} {'raised' if upper else 'lowered'} or made inclusive: more values pass"
    return None


def _contains_bound(old: Any, new: Any) -> str | None:
    """minContains defaults to 1: adding minContains: 0 lets an array with no match through."""
    a = 1 if old is None else old
    b = 1 if new is None else new
    if not (_number(a) and _number(b)):
        return "minContains changed: the schema may accept more input"
    if b >= a:
        return None
    return "minContains lowered: fewer matches pass" if new is not None else "minContains removed: no lower bound"


def _required_list_weakening(old: Any, new: Any) -> str | None:
    if isinstance(old, bool) or isinstance(new, bool):          # an OpenAPI parameter or request body
        return "no longer required" if old is True and new is not True else None
    if old is None:
        return None
    if new is None:
        return "a field is no longer required"
    if isinstance(old, list) and isinstance(new, list):
        gone = sorted({str(x) for x in old} - {str(x) for x in new})
        return f"{', '.join(gone)} no longer required" if gone else None
    return "the required fields changed"


def _dependent_required(key: str, old: Any, new: Any) -> str | None:
    """dependentRequired (or a Draft 7 `dependencies` entry that lists names): a name no longer
    required with its key is a weakening."""
    a = old if isinstance(old, dict) else {}
    b = new if isinstance(new, dict) else {}
    for name, need in a.items():
        got = b.get(name)
        if not (isinstance(need, list) and isinstance(got, list) and {canonical(x) for x in need} <= {canonical(x) for x in got}):
            return f"{key}.{name} {'removed' if got is None else 'changed'}: the schema may accept more input"
    return None


def _repointed(da: _Doc, db: _Doc) -> str | None:
    """A reference whose target moved ($id, $anchor or $dynamicAnchor edited, added or removed): the
    old and new targets are compared whole."""
    new_sites = {(keys, kw): ref for keys, kw, ref in db.sites}
    for keys, kw, ref in da.sites:
        if new_sites.get((keys, kw)) != ref:
            continue                                             # the reference itself changed: judged there
        ta, tb = da.resolve(keys, kw, ref), db.resolve(keys, kw, ref)
        if ta == tb or (ta is not None and tb is not None and len(ta) == len(tb)
                        and all(same(_at(da.entry, x), _at(db.entry, y)) for x, y in zip(ta, tb))):
            continue
        return _Absolute(f"{kw} {ref} now resolves to {'nothing' if tb is None else 'a different schema'}: the "
                         f"value is checked against something else")
    return None


def _has_ids(v: Any, depth: int = 0) -> bool:
    if depth > 4 * MAX_DEPTH:
        return True
    if isinstance(v, dict):
        return any(k in _ID_KEYS or k == "id" for k in v) or any(_has_ids(x, depth + 1) for x in v.values())
    if isinstance(v, list):
        return any(_has_ids(x, depth + 1) for x in v)
    return False


def _verdict(loc: tuple[str, tuple[Any, ...], Any, Any], da: _Doc, db: _Doc) -> str | None:
    """Why the change at `loc` (_locate) lets the subschema there accept more input, or None; from the
    old (da) to the new (db) side. Called with the sides swapped for a place at polarity -1."""
    kind, nk, k, sub = loc
    ra, rb = da.entry, db.entry
    if kind == "root":
        ov, nv = _at(ra, nk), _at(rb, nk)
        return None if ov is MISSING or nv is MISSING else _schema_replaced("the schema", ov, nv)
    if kind == "item":
        ov, nv = _at(ra, (*nk, k, sub)), _at(rb, (*nk, k, sub))
        return _schema_replaced(f"{k}[{sub}]", ov, nv) or _evaluates_more(list(nk), k, ov, nv, ra)
    if kind == "slot":
        ov, nv = _at(ra, (*nk, k)), _at(rb, (*nk, k))
        if k == "additionalProperties" or k in _UNEVALUATED:
            return _schema_replaced(k, ov, nv) or _evaluates_more(list(nk), k, ov, nv, ra)
        return _schema_replaced(k, ov, nv) or (_evaluates_more(list(nk), k, ov, nv, ra) if k in APPLICATORS else None)
    if kind == "entry":
        return _entry_verdict(nk, k, sub, da, db)
    return _kw_verdict(nk, k, da, db)


def _entry_verdict(nk: tuple[Any, ...], k: str, name: str, da: _Doc, db: _Doc) -> str | None:
    ra, rb = da.entry, db.entry
    ov, nv = _at(ra, (*nk, k, name)), _at(rb, (*nk, k, name))
    on, nn = _at(ra, nk), _at(rb, nk)
    if k in ("$defs", "definitions"):
        if nv is MISSING:
            dk = (*nk, k, name)
            if not da.dangling and not any(t[:len(dk)] == dk for t in da.targets):
                return None                                      # nothing referred to it
            defs = _at(rb, (*nk, k))
            if isinstance(defs, dict) and any(same(ov, v) for v in defs.values()):
                return None
            return _Absolute(f"schema {name} removed while it was referenced: what referred to it is no "
                             f"longer checked")
        return None if ov is MISSING else _schema_replaced(f"{k}.{name}", ov, nv)
    if ov is MISSING:
        if nv is MISSING:
            return None
        if k == "properties":
            return _property_opens(on, nn, name, nv, unevaluated_scope(ra, list(nk), inherited=True))
        if k == "patternProperties":
            return _opens_closed(on, f"patternProperties {name!r} added") \
                or _evaluates_more(list(nk), f"patternProperties {name!r}", None, nv, ra, inherited=True)
        if isinstance(nv, list):
            return None                                          # a Draft 7 dependency: names required
        return _evaluates_more(list(nk), f"{k} {name!r}", None, nv, ra)
    if nv is MISSING:
        if k == "properties":
            return f"field {name} removed: its constraints no longer apply"
        if k == "patternProperties":
            return f"patternProperties {name!r} removed: properties matching it are no longer checked by it"
        return f"{k}.{name} removed: the schema may accept more input"
    if isinstance(ov, list) or isinstance(nv, list):          # a Draft 7 dependency that lists names
        return _dependent_required(k, {name: ov}, {name: nv})
    return _schema_replaced(f"{k}.{name}", ov, nv)


def _kw_verdict(nk: tuple[Any, ...], k: str, da: _Doc, db: _Doc) -> str | None:
    ra, rb = da.entry, db.entry
    ov, nv = _at(ra, (*nk, k)), _at(rb, (*nk, k))
    on, nn = _at(ra, nk), _at(rb, nk)
    o = None if ov is MISSING else ov
    n = None if nv is MISSING else nv
    if k in ("const", "enum"):
        return _values_weakening(k, on, nn)
    if k in ("default", "examples", "example"):
        return None
    if k == "$schema":
        return None if same(o, n) else _Absolute("$schema changed: the schema's keywords may mean other things")
    if k in _ID_KEYS or (k == "id" and da.dialect == "draft4"):
        return _repointed(da, db)
    if k in _REF_KEYS:
        why = _ref_verdict(nk, k, o, n, da, db)
    elif k in _SCHEMA_MAPS:
        why = _map_verdict(nk, k, o, n, da, db)
    elif k in _SCHEMA_LISTS or (k == "items" and (isinstance(o, list) or isinstance(n, list))):
        why = _list_verdict(nk, k, o, n, on)
    elif k == "dependentRequired":
        why = _dependent_required(k, o, n)
    elif k == "required":
        why = _required_list_weakening(o, n)
    elif k == "type":
        why = None if o is None else _type_weakening("", o, n)
    elif k == "minContains":
        why = _contains_bound(o, n)
    elif da.dialect == "draft4" and k in _TWIN_BOUNDS:
        why = _draft4_bound(k, on, nn)
    else:
        why = _keyword_rules([*nk, k], "", o, n, ra, rb)
    if why is not None or k not in APPLICATORS or (k in _REF_KEYS and o is not None):
        return why
    return _evaluates_more(list(nk), k, o, n, ra)


def _ref_verdict(nk: tuple[Any, ...], k: str, o: Any, n: Any, da: _Doc, db: _Doc) -> str | None:
    """A reference added (a tightening, except beside other keywords in Draft 7 and earlier, which then
    ignore them), removed (the value is no longer checked against it) or pointed elsewhere (compared by
    the bodies the old and new references name)."""
    if o is None:
        node = _at(db.entry, nk)
        others = [x for x in node if x != k and x not in _ANNOTATIONS] if isinstance(node, dict) else []
        if n is not None and da.dialect in ("draft4", "draft7") and others:
            return f"{k} added beside {', '.join(sorted(others))}: before 2019-09 they are ignored next to it"
        return None
    if n is None:
        return f"reference {o} removed: the value is no longer checked against it"
    ta = da.resolve(nk, k, o) if isinstance(o, str) else None
    tb = db.resolve(nk, k, n) if isinstance(n, str) else None
    if ta and tb and len(ta) == len(tb) == 1 and same(_at(da.entry, ta[0]), _at(db.entry, tb[0])):
        return None                                              # the same schema under a new name
    return f"reference {o} -> {n}: the value is checked against a different schema"


def _map_verdict(nk: tuple[Any, ...], k: str, o: Any, n: Any, da: _Doc, db: _Doc) -> str | None:
    """A properties/patternProperties/$defs/... map added, removed or replaced whole: each entry."""
    if o is not None and not isinstance(o, dict) or n is not None and not isinstance(n, dict):
        return None if o is None else f"{k} changed: the schema may accept more input"
    for name in sorted(set(o or {}) | set(n or {}), key=str):
        a, b = (o or {}).get(name, MISSING), (n or {}).get(name, MISSING)
        if a is MISSING or b is MISSING or not same(a, b):
            why = _entry_verdict(nk, k, name, da, db)
            if why is not None:
                return why
    return None


def _list_verdict(nk: tuple[Any, ...], k: str, o: Any, n: Any, on: Any) -> str | None:
    """allOf/anyOf/oneOf/prefixItems (or a Draft 7 items list) added, removed, or of another length."""
    if o is None:
        return None
    if n is None:
        return f"{k} removed: what it checked no longer applies"
    if not (isinstance(o, list) and isinstance(n, list)):
        return f"{k} changed: the schema may accept more input"
    if k == "allOf" and all(any(same(x, y) for y in n) for x in o):
        return None                                              # more schemas, all of which must hold
    if k == "anyOf" and all(any(same(x, y) for y in o) for x in n):
        return None                                              # branches dropped
    if k in ("prefixItems", "items") and same(o, n[:len(o)]):
        rest = on.get("items" if k == "prefixItems" else "additionalItems") if isinstance(on, dict) else None
        if rest is None or _loose(rest) or all(same(x, rest) for x in n[len(o):]):
            return None
        return (f"{k} extended to {len(n)}: positions {len(o)}.. were checked by "
                f"{'items' if k == 'prefixItems' else 'additionalItems'} {_short(rest)}, and are now checked "
                f"only by the new entries")
    return f"{k} changed: the schema may accept more input"


def _flip_note(path: str) -> str:
    names = [x.partition("[")[0] for x in path.split(" ")[0].split(".")]
    flip = next((n for i, n in enumerate(names[1:], 1) if n in ("not", "if", "oneOf", "contains")
                 and names[i - 1] not in _NAME_SEGMENTS), None)
    where = flip if flip is not None else "a schema a $ref uses under not, oneOf, if or contains"
    return f"changed inside {where}: there a tighter schema can accept more input"


def constraint_weakening(path: str, old: Any, new: Any, old_root: Any = None, new_root: Any = None,
                         polarity: frozenset[int] = POS) -> str | None:
    """Why a change at `path` of a pinned schema lets it accept more input, or None.

    `path` is as _json_paths reports it inside one schema-table entry ("pydantic.properties.amount.maximum",
    "pydantic.required removed") or contract entry ("operations.createRefund.requestBody..."). `old_root`
    and `new_root` are that entry: the change is judged where it sits (_locate) with its neighbours (a
    bound and its twin, an enum and a const, a $ref and its target), at the polarity of that place,
    composed with `polarity`, the polarity the entry itself is used at (ref_polarity). Only
    JUDGED_SCHEMAS are judged: a pydantic model's JSON Schema, a dataclass or TypedDict contract's, and
    OpenAPI schemas."""
    head = path.partition(".")[0].partition(" ")[0]
    if head not in JUDGED_SCHEMAS:
        return None
    base, _, suffix = path.partition(" ")
    keys = _keys(old_root, new_root, base) if old_root is not None and new_root is not None else None
    if keys is not None and not suffix and not all(
            (got is MISSING and v is None) or (got is not MISSING and same(got, v))
            for got, v in ((_at(old_root, keys), old), (_at(new_root, keys), new))):
        keys = None                              # the values are not the entries': judged from the path
    if keys is not None:
        da, db = _docs(old_root, new_root, head)
        loc = _locate(keys, da, db)
        if loc is not None:
            kind, nk, k, sub = loc
            here = nk
            if kind == "slot":
                here = (*nk, k)
            elif kind in ("item", "entry") and MISSING not in (_at(old_root, (*nk, k, sub)), _at(new_root, (*nk, k, sub))):
                here = (*nk, k, sub)             # a subschema replaced: judged at its own place
            pol = _compose(polarity, da.polarity(here) | db.polarity(here))
            fwd = _verdict(loc, da, db)
            if isinstance(fwd, _Absolute) or (fwd is not None and 1 in pol):
                return str(fwd)
            if -1 in pol and _verdict(loc, db, da) is not None:
                return _flip_note(path)
            if _has_ids(_at(old_root, keys)) or _has_ids(_at(new_root, keys)):
                moved = _repointed(da, db)       # an $id or $anchor added or removed with its subschema
                return None if moved is None else str(moved)
            return None
    swapped = {"added": "removed", "removed": "added"}
    back = f"{base} {swapped.get(suffix, suffix)}".rstrip()
    fwd = _path_weakening(path, old, new, old_root, new_root)
    if fwd is not None and 1 in polarity:
        return fwd
    if -1 in polarity and _path_weakening(back, new, old, new_root, old_root) is not None:
        return _flip_note(path)
    return None


def _path_weakening(path: str, old: Any, new: Any, old_root: Any = None, new_root: Any = None) -> str | None:
    """constraint_weakening read from the path alone, for a change whose entries are not given (or whose
    path cannot be followed in them): a change inside not, if or oneOf is a possible weakening, and a
    schema replaced by `true` or `false` is judged as a whole."""
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
    if parent in ("dependentRequired", "dependentSchemas", "dependencies"):
        if old is None or suffix == "added":
            return None                                          # a new dependency only tightens
        return f"{parent}.{key} {'removed' if new is None or suffix == 'removed' else 'changed'}: " \
               f"the schema may accept more input"
    data = next((i for i, n in enumerate(names[1:], 1) if n in _DATA_KEYWORDS and names[i - 1] not in _NAME_SEGMENTS),
                None)
    if data is not None and (data < len(names) - 1 or "[" in segs[data]):
        # inside a value (const: {"a": 1}, enum: [[1]]): the value changed as a whole
        return None if names[data] in ("default", "examples", "example") else f"{names[data]} changed: another value may pass"
    if "[" in key and names[-1] in _SCHEMA_LISTS and not (isinstance(old, dict) and isinstance(new, dict)):
        return _schema_replaced(key, old, new)                   # allOf[0]: {...} -> true
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
        if new is None and parent == "patternProperties":
            return f"patternProperties {key!r} removed: properties matching it are no longer checked by it"
        if old is not None and new is not None and not (isinstance(old, dict) and isinstance(new, dict)):
            return _schema_replaced(f"{parent}.{key}", old, new)
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


def _schema_changes(refs: typing.Sequence[tuple[Any, ...]], old_tab: dict[str, Any],
                    new_tab: dict[str, Any]) -> list[Change]:
    """Each changed schema once, with the entries that use it, judged at the polarity it is used at:
    a component named under `not` (directly, or through other components) is judged the other way
    round, one used both ways is judged both ways. `refs` holds (user, path, old digest, new digest)
    and, from _pin_changes, the polarity of the path as a fifth item."""
    users: dict[tuple[str, str], list[str]] = collections.OrderedDict()
    pols: dict[tuple[str, str], frozenset[int]] = {}
    work: collections.deque[tuple[tuple[str, str], frozenset[int]]] = collections.deque()

    def use(pair: tuple[str, str], pol: frozenset[int], user: str, first: bool) -> None:
        if pair not in users or not first:
            users.setdefault(pair, []).append(user)
        more = pol - pols.get(pair, frozenset())
        if more:
            pols[pair] = pols.get(pair, frozenset()) | more
            work.append((pair, more))
    for r in refs:
        use((r[2], r[3]), r[4] if len(r) > 4 else POS, r[0], False)
    nested_of: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    while work:
        pair, pol = work.popleft()
        sa, sb = old_tab.get(pair[0]), new_tab.get(pair[1])
        if not isinstance(sa, dict) or not isinstance(sb, dict):
            continue
        if pair not in nested_of:
            nested_of[pair] = []
            _json_paths(sa, sb, "", nested_of[pair])
        for p, xa, xb in nested_of[pair]:
            use((xa, xb), _compose(pol, ref_polarity(p, sa, sb)), f"{_schema_where(sa, sb, pair[1])} {p}", True)
    out: list[Change] = []
    for (da, db), who in users.items():
        sa, sb = old_tab.get(da), new_tab.get(db)
        where = _schema_where(sa, sb, db)
        used = "used by " + ", ".join(who)
        if not isinstance(sa, dict) or not isinstance(sb, dict):
            out.append(Change(where, "schema", da, db, note=f"{used}; the stored lock does not hold the old "
                                                            f"schema, so the change cannot be shown"))
            continue
        nested: list[tuple[str, str, str]] = []
        diffs = _json_paths(sa, sb, "", nested)
        out += _schema_entry_changes(where, used, sa, sb, diffs, pols.get((da, db), POS))
        if not diffs and not nested:
            out.append(Change(where, "schema", da, db, note=used))
    return out


def _schema_where(sa: Any, sb: Any, db: str) -> str:
    name = next((s.get("name") for s in (sb, sa) if isinstance(s, dict) and s.get("name")), db[:19])
    return f"schema {name}"


def _schema_entry_changes(where: str, used: str, sa: dict[str, Any], sb: dict[str, Any],
                          diffs: list[tuple[str, Any, Any]], polarity: frozenset[int] = POS) -> list[Change]:
    """The leaf differences of one schema-table entry, each judged by constraint_weakening at the
    polarity the entry is used at; a pin taken from Python annotations is also judged as the JSON
    Schema it describes."""
    out: list[Change] = []
    for i, (p, x, y) in enumerate(diffs):
        why = constraint_weakening(p, x, y, sa, sb, polarity)
        note = "; ".join(n for n in (why, used if i == 0 else "") if n)
        out.append(Change(where, p, x, y, weakened=why is not None, note=note))
    ja, jb = pin_json_schema(sa), pin_json_schema(sb)
    if diffs and ja is not None and jb is not None:
        # a {field: type} mapping, a dataclass or TypedDict a signature names, an Enum: judged as the
        # JSON Schema its pin describes, so a widened field type or a new enum member is WEAKENED
        ra, rb = {"jsonschema": ja}, {"jsonschema": jb}
        for p, x, y in _json_paths(ra, rb):
            why = constraint_weakening(p, x, y, ra, rb, polarity)
            if why is not None:
                out.append(Change(where, p, x, y, weakened=True, note=f"{why} (the pin judged as JSON Schema)"))
    return out


def _param_key(p: Any) -> tuple[str, str] | None:
    return (str(p.get("in")), str(p.get("name"))) if isinstance(p, dict) else None


def _param_required(p: dict[str, Any]) -> bool:
    return p.get("required") is True or p.get("in") == "path"          # a path parameter always is


def aligned_parameters(where: str, a: dict[str, Any], b: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any], list[Change], dict[str, str]]:
    """The old and new contract entries with each operation's parameters matched by (in, name), not by
    position: a parameter both have sits at the same index in both lists, so its schema, required
    and allowEmptyValue are judged against its old self. A parameter only one side has is taken out
    and judged here: removing a required one is WEAKENED (a request without it now passes), and so is
    removing an optional one (the value it is sent is no longer checked: a request its schema refused
    now passes); an added one only tightens. Also the display name of each aligned parameter path
    ("operations.op.parameters[0]" -> "operations.op.parameters[query limit]")."""
    oa, ob = a.get("operations"), b.get("operations")
    if not isinstance(oa, dict) or not isinstance(ob, dict):
        return a, b, [], {}
    na, nb = dict(oa), dict(ob)
    out: list[Change] = []
    names: dict[str, str] = {}
    for op in sorted(set(oa) & set(ob), key=str):
        x, y = oa[op], ob[op]
        if not (isinstance(x, dict) and isinstance(y, dict)):
            continue
        px, py = x.get("parameters", []), y.get("parameters", [])
        if not (isinstance(px, list) and isinstance(py, list)):
            continue
        kx = {k: p for p in px if (k := _param_key(p)) is not None}
        ky = {k: p for p in py if (k := _param_key(p)) is not None}
        if len(kx) != len(px) or len(ky) != len(py):
            continue                                   # a malformed pin: compared by position
        both = sorted(set(kx) & set(ky))
        for i, k in enumerate(both):
            names[f"operations.{op}.parameters[{i}]"] = f"operations.{op}.parameters[{k[0]} {k[1]}]"
        for k in sorted(set(kx) - set(ky)):
            p = kx[k]
            why = (f"required {k[0]} parameter {k[1]} removed: a request without it now passes"
                   if _param_required(p) else
                   f"{k[0]} parameter {k[1]} removed: its schema no longer checks the value a request sends")
            out.append(Change(where, f"operations.{op}.parameters[{k[0]} {k[1]}] removed", p, None, True, why))
        for k in sorted(set(ky) - set(kx)):
            p = ky[k]
            out.append(Change(where, f"operations.{op}.parameters[{k[0]} {k[1]}] added", None, p,
                              note=("a new required parameter: a request without it is now refused"
                                    if _param_required(p) else "a new optional parameter, checked when sent")))
        na[op] = {**x, "parameters": [kx[k] for k in both]}
        nb[op] = {**y, "parameters": [ky[k] for k in both]}
        for side, d in ((na, x), (nb, y)):
            if not side[op]["parameters"] and "parameters" not in d:
                del side[op]["parameters"]
    return {**a, "operations": na}, {**b, "operations": nb}, out, names


def parameter_flag_weakening(path: str, old: Any, new: Any) -> str | None:
    """allowEmptyValue turned on for an aligned OpenAPI parameter (required is judged by
    constraint_weakening)."""
    head, _, key = path.rpartition(".")
    if not (head.startswith("operations.") and ".parameters[" in head and head.endswith("]")):
        return None
    if key == "allowEmptyValue" and new is True and old is not True:
        return "allowEmptyValue now true: an empty value passes"
    return None


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
