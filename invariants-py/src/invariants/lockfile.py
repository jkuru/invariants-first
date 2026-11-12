"""`inv lock`: invariants.lock pins the contract a rule file relies on, at contract granularity.

Lock version 3 pins, per entry, only what a rule's meaning depends on:

- rules: the semantic content of each rule (bad, enforcers with their modes, on_violation, on_error and
  views, bind, scope, subject, mediation sink/door/tables, model_policy with its defaults filled in,
  ...), its status, the expanded targets, and a digest per documentation field (statement, why, ...);
- targets: each target's signature (unwrapped through its decorators) as text and as resolved types,
  its declared effects and transaction, and the reply schema of a model-call target;
- providers: each provider's declared type, args, consistency, and its function's signature;
- contracts: a pydantic, dataclass or {field: type} schema, an openapi file as the parameters, request
  body and response schemas of the operations rules name, a python module as its reference only (its
  targets are pinned above), any other file by its digest;
- schemas: every resolved schema the entries above use, keyed by the sha256 of its canonical JSON, so
  a model used by a target and a contract is stored once. Prose (title, description, summary, tags,
  examples) is stripped from pinned schemas, so a docstring edit does not re-pin.

Types are resolved, not read as annotation text: an alias, and the fields of a dataclass, TypedDict or
pydantic model a signature names (recursively, bounded), are part of the pin. A body-only refactor of a
target, or an edit elsewhere in the same module, changes none of these.

`changes(old, new)` explains every difference as old -> new and marks the ones that may relax a rule as
WEAKENED; `inv lock` refuses to re-pin those without --accept-weakening. FIELD_POLICY says how each
rule-file field is judged. A field it does not classify, and any edit to a field that changes what
`bad` reads (bind, a view, provider args, the subject, model_policy, an artifact), is a possible
weakening. Lock versions 1 and 2 are migrated by `inv lock` and refused by `inv lock --check`.
The lock is JSON with sorted keys, so a change in coverage also shows up as a diff in review.
"""
from __future__ import annotations

import collections
import dataclasses
import enum
import fnmatch
import hashlib
import importlib
import inspect
import json
import math
import pathlib
import typing
from typing import Any

from .errors import LoadError
from .resolve import resolve

LOCK_NAME = "invariants.lock"
LOCK_VERSION = 3
PY_CONTRACTS = frozenset({"python", "pydantic"})
SCHEMA_CONTRACTS = frozenset({"pydantic", "dataclass", "schema"})
# rule fields that are prose, pinned by digest only; everything else is semantic
DOC_FIELDS = ("statement", "why", "owner", "class")
MEDIATION_DOC = ("chokepoint",)

STATUS_RANK = {"retired": 0, "candidate": 1, "contested": 1, "detect": 2, "enforce": 3}
MODE_RANK = {"off": 0, "detect": 1, "enforce": 2}
VIOLATION_RANK = {"route": 1, "escalate": 1, "reject": 2, "halt": 2}
ERROR_RANK = {"fail_open_and_page": 0, "fail_closed": 1}
CONSISTENCY_RANK = {"stale_ok": 0, "snapshot": 1, "same_txn": 2, "immutable": 2}
EFFECTS_RANK = {"none": 0, "external": 1, "transactional": 1}
TIMING_RANK = {"post_effect": 0, "pre_effect": 1}
COMPENSABLE_RANK = {"true": 0, "false": 1}
ENFORCER_DEFAULTS = {"on_violation": "reject", "on_error": "fail_closed"}
FEEDBACK_DEFAULT = {"format": "parser_error", "semantic": "none"}

# --- how each field is judged (R10-1) ------------------------------------------------------------
# Every property of schema/invariants.schema.json is listed here, by its place in the schema
# ("rule.bind", "enforcer.view", "modelPolicy.abstain.value", ...); tests/test_fix_round13.py walks the
# schema and fails when one is missing. A field that is not listed is treated as "weakening".
#   weakening  any change may relax the rule (it changes what `bad` reads or how a check runs):
#              refused without --accept-weakening
#   ranked     ordered values (RANKS): a lowering is WEAKENED, a raising is a strengthening
#   more_is_weaker  a number (retries, resamples): going up is WEAKENED, going down is not
#   presence   removing it is WEAKENED, adding it is a strengthening
#   analysed   a dedicated analysis decides (bad polarity, enforcer and clause matching, mediation,
#              residual paths (a removal is WEAKENED: it can raise standing), target coverage,
#              rule identity)
#   neutral    recorded as a change, never a weakening (rev, falsified_by, messages)
#   prose      pinned by digest only (statement, why, owner, class, chokepoint)
#   computed   not pinned (evidence: computed from the tests)
#   pin        a code pin taken by `inv lock` (signatures, types, schemas): a change, not a weakening
WEAKENING, RANKED, MORE_IS_WEAKER, PRESENCE, ANALYSED, NEUTRAL, PROSE, COMPUTED, PIN = (
    "weakening", "ranked", "more_is_weaker", "presence", "analysed", "neutral", "prose", "computed", "pin")
FIELD_POLICY: dict[str, str] = {
    # the file
    "file.spec": NEUTRAL, "file.contracts": ANALYSED, "file.targets": ANALYSED, "file.providers": ANALYSED,
    "file.rules": ANALYSED,
    "contract.type": WEAKENING, "contract.ref": WEAKENING,
    "contract.schema": PIN, "contract.operations": PIN, "contract.hash": PIN, "contract.signature": PIN,
    "contract.types": PIN, "contract.constraints": ANALYSED,
    # targets and providers
    "target.effects": RANKED, "target.transaction": WEAKENING,
    "target.signature": PIN, "target.types": PIN, "target.reply_schema": PIN,
    "provider.type": WEAKENING, "provider.from": WEAKENING, "provider.args": WEAKENING,
    "provider.consistency": RANKED, "provider.signature": PIN, "provider.types": PIN,
    # rules
    # the short form (guard, tests) is expanded by loader.expand_short_forms before the lock reads the file:
    # the lock judges the subject.target, enforcer and falsified_by it becomes
    "rule.guard": ANALYSED, "rule.tests": NEUTRAL,
    "rule.door": ANALYSED, "rule.sink": ANALYSED, "rule.tables": ANALYSED, "rule.allow": ANALYSED,   # short door form
    "rule.id": ANALYSED, "rule.rev": NEUTRAL, "rule.status": RANKED,
    "rule.class": PROSE, "rule.owner": PROSE, "rule.statement": PROSE, "rule.why": PROSE,
    "rule.subject": WEAKENING, "rule.subject.contract": WEAKENING, "rule.subject.target": WEAKENING,
    "rule.subject.operation": WEAKENING,
    "rule.scope": WEAKENING, "rule.bind": WEAKENING, "rule.bad": ANALYSED, "rule.compensable": RANKED,
    "rule.enforcers": ANALYSED, "rule.mediation": ANALYSED, "rule.model_policy": ANALYSED,
    "rule.compares_to": PRESENCE, "rule.falsified_by": NEUTRAL, "rule.evidence": COMPUTED,
    "rule.applies_to": ANALYSED,
    "rule.mediation.chokepoint": PROSE, "rule.mediation.residual_paths": ANALYSED,
    "rule.mediation.sink": ANALYSED, "rule.mediation.door": ANALYSED, "rule.mediation.tables": ANALYSED,
    "rule.mediation.allow": ANALYSED,
    "clause.when": ANALYSED, "clause.message": NEUTRAL,
    # enforcers (point and target identify the enforcer: a change is a removal plus an addition)
    "enforcer.point": ANALYSED, "enforcer.target": ANALYSED, "enforcer.timing": RANKED, "enforcer.mode": RANKED,
    "enforcer.on_violation": RANKED, "enforcer.on_error": RANKED, "enforcer.view": WEAKENING,
    "enforcer.artifact": WEAKENING, "enforcer.generated": WEAKENING, "enforcer.conformance_test": WEAKENING,
    # model_policy
    "modelPolicy.schema": WEAKENING, "modelPolicy.format_retries": MORE_IS_WEAKER,
    "modelPolicy.semantic_resamples": MORE_IS_WEAKER, "modelPolicy.feedback": WEAKENING,
    "modelPolicy.feedback.format": WEAKENING, "modelPolicy.feedback.semantic": WEAKENING,
    "modelPolicy.abstain": WEAKENING, "modelPolicy.abstain.field": WEAKENING,
    "modelPolicy.abstain.value": WEAKENING,
}
RANKS: dict[str, dict[str, int]] = {
    "rule.status": STATUS_RANK, "enforcer.mode": MODE_RANK, "enforcer.on_violation": VIOLATION_RANK,
    "enforcer.on_error": ERROR_RANK, "enforcer.timing": TIMING_RANK, "provider.consistency": CONSISTENCY_RANK,
    "target.effects": EFFECTS_RANK, "rule.compensable": COMPENSABLE_RANK,
}
NOTES = {
    "rule.bind": "a binding changes what bad reads",
    "rule.subject": "the rule now checks a different contract, operation or target",
    "rule.scope": "the scope changes which calls or rows bad is evaluated over",
    "enforcer.view": "a view changes what bad reads at this enforcer",
    "enforcer.artifact": "the storage artifact (trigger, constraint) that enforces the rule changed",
    "enforcer.conformance_test": "a different test now shows the artifact agrees with bad",
    "enforcer.generated": "whether the artifact is generated from bad changed",
    "enforcer.timing": "a post_effect check runs after the effect",
    "provider.args": "the provider now reads different arguments",
    "provider.from": "a different function now supplies the value bad reads",
    "provider.type": "the declared type of the value bad reads changed",
    "modelPolicy.format_retries": "more retries give the model more chances to pass",
    "modelPolicy.semantic_resamples": "more resamples give the model more chances to pass",
    "modelPolicy.schema": "the reply is parsed with a different schema",
    "modelPolicy.feedback": "what the model is told after a failure changed",
    "modelPolicy.abstain": "what counts as an abstention changed",
    "rule.compares_to": "the reply is no longer compared with held data",
    "target.transaction": "the effect now runs in a different transaction",
    "contract.type": "the contract the rule names is read differently",
    "contract.ref": "the contract the rule names points elsewhere",
}


def policy(where: str) -> str:
    """How a change at a schema location is judged; an unlisted location is a possible weakening."""
    return FIELD_POLICY.get(where, WEAKENING)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _plain(obj: Any) -> Any:
    """A JSON round trip: tuples become lists, keys become strings, so stored and fresh locks compare equal."""
    return json.loads(canonical(obj))


def same(a: Any, b: Any) -> bool:
    """Type-strict equality: 1 and true, or 1 and 1.0, differ (Python's == says they are equal)."""
    return canonical(a) == canonical(b)


def rule_hash(raw: dict[str, Any]) -> str:
    """Hash of a rule as written, without the computed `evidence` field (lock version 1's rule pin)."""
    return _sha(canonical({k: v for k, v in raw.items() if k != "evidence"}))


def expand_applies_to(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Rule id -> sorted target keys its applies_to patterns match. A pattern that matches nothing fails."""
    keys = sorted((doc.get("targets") or {}).keys())
    out: dict[str, list[str]] = {}
    for raw in doc.get("rules") or []:
        if "applies_to" not in raw:
            continue
        pats = raw["applies_to"]
        pats = [pats] if isinstance(pats, str) else pats
        if not isinstance(pats, list) or not pats or not all(isinstance(p, str) for p in pats):
            raise LoadError(f"{raw['id']}: applies_to must be a pattern or a non-empty list of patterns")
        hits: set[str] = set()
        for p in pats:
            got = fnmatch.filter(keys, p)
            if not got:
                raise LoadError(f"{raw['id']}: applies_to pattern {p!r} matches no entry in targets:")
            hits.update(got)
        out[raw["id"]] = sorted(hits)
    return out


# --- schemas: stripping, content addressing --------------------------------------------------------
SCHEMA_PROSE = frozenset({"title", "description", "summary", "tags", "examples", "example", "$comment",
                          "externalDocs"})
_NAME_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "fields",
                        "responses", "content", "schemas", "headers", "encoding"})
_DATA_KEYS = frozenset({"enum", "const", "default", "literal", "values", "required"})


def strip_prose(node: Any) -> Any:
    """A schema without its prose (title, description, summary, tags, examples) at every level. Keys of
    name maps (properties, $defs, ...) are names, not keywords, so a property called `title` stays."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in SCHEMA_PROSE:
                continue
            if k in _DATA_KEYS:
                out[k] = v
            elif k in _NAME_MAPS and isinstance(v, dict):
                out[k] = {name: strip_prose(s) for name, s in v.items()}
            else:
                out[k] = strip_prose(v)
        return out
    if isinstance(node, list):
        return [strip_prose(v) for v in node]
    return node


class Schemas:
    """The content-addressed schema table of one lock: digest -> canonical schema.

    With inline=True (direct API use, no table to write), add() returns the schema itself."""

    def __init__(self, inline: bool = False) -> None:
        self.table: dict[str, Any] = {}
        self.inline = inline

    def add(self, schema: Any) -> Any:
        body = _plain(strip_prose(schema))
        if self.inline:
            return body
        digest = _sha(canonical(body))
        self.table[digest] = body
        return {"$ref": digest}


MAX_DEFS = 64          # schema classes expanded in one closure; beyond it a $ref is marked truncated
MAX_DEPTH = 24         # nesting of one annotation (generics inside generics, aliases of aliases)


def _class_ref(t: type) -> str:
    return f"{t.__module__}.{t.__qualname__}"


def _type_schema(t: Any, defs: dict[str, Any], seen: set[int], pins: Schemas | None = None,
                 depth: int = 0) -> Any:
    """A JSON-able description of a resolved annotation. Literal values, unions, aliases and nested
    schemas are expanded. With `pins`, a schema class is pinned as its own entry in the schema table
    ({"$ref": digest}); without, it goes into `defs` by name (one closure)."""
    if depth > MAX_DEPTH:
        return {"truncated": repr(t)[:80]}
    d = depth + 1
    if t is type(None) or t is None:
        return "null"
    if type(t).__name__ == "TypeAliasType" and hasattr(t, "__value__"):    # PEP 695: type X = ...
        return _type_schema(t.__value__, defs, seen, pins, d)
    origin = typing.get_origin(t)
    if origin is typing.Literal:
        return {"literal": sorted((_plain(a) for a in typing.get_args(t)), key=repr)}
    if origin is typing.Annotated:
        return _type_schema(typing.get_args(t)[0], defs, seen, pins, d)
    if origin is not None:
        if type(origin).__name__ == "TypeAliasType" and hasattr(origin, "__value__"):
            return _type_schema(origin.__value__, defs, seen, pins, d)
        name = getattr(origin, "__name__", None) or str(origin)
        if name in ("Union", "UnionType") or str(origin) in ("typing.Union", "<class 'types.UnionType'>"):
            opts = [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)]
            return {"anyOf": sorted(opts, key=lambda o: json.dumps(o, sort_keys=True))}
        return {"generic": name, "args": [_type_schema(a, defs, seen, pins, d) for a in typing.get_args(t)]}
    if hasattr(t, "__supertype__"):                        # typing.NewType
        return _type_schema(t.__supertype__, defs, seen, pins, d)
    if isinstance(t, type):
        if issubclass(t, enum.Enum):
            return {"enum": t.__qualname__, "values": sorted((repr(m.value) for m in t), key=str)}
        if _is_schema_class(t):
            ref = _class_ref(t)
            if pins is not None:
                return pins.add({"name": ref, **resolved_schema(t)})
            if ref not in defs and id(t) not in seen:
                if len(defs) >= MAX_DEFS:
                    return {"$ref": ref, "truncated": f"more than {MAX_DEFS} nested schemas"}
                seen.add(id(t))
                defs[ref] = None                           # placeholder: cuts recursion
                defs[ref] = resolved_schema(t, defs, seen)
            return {"$ref": ref}
        return getattr(t, "__qualname__", repr(t))
    if isinstance(t, typing.TypeVar):
        return f"~{t.__name__}"
    if isinstance(t, typing.ForwardRef):
        return {"unresolved": t.__forward_arg__}
    if isinstance(t, str):
        return {"unresolved": t}
    return repr(t)


def _is_schema_class(t: Any) -> bool:
    return isinstance(t, type) and (dataclasses.is_dataclass(t) or _is_pydantic(t) or _is_typeddict(t))


def _is_pydantic(t: Any) -> bool:
    try:
        import pydantic
    except ImportError:
        return False
    return isinstance(t, type) and issubclass(t, pydantic.BaseModel)


def _is_typeddict(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, dict) and hasattr(t, "__annotations__") and hasattr(t, "__total__")


def resolved_schema(schema: Any, defs: dict[str, Any] | None = None, seen: set[int] | None = None) -> Any:
    """The resolved, canonical schema of a closed schema: pydantic model_json_schema(), or the resolved
    annotations of a dataclass, TypedDict or {field: type} mapping (aliases and nested schema classes
    are expanded into $defs). Prose (title, description, examples, ...) is stripped."""
    top = defs is None
    defs = {} if defs is None else defs
    seen = set() if seen is None else seen
    model = getattr(schema, "__inv_pydantic__", None)          # a closed() wrapper of a pydantic model
    if model is not None:
        schema = model
    if _is_pydantic(schema):
        out: Any = {"pydantic": _plain(schema.model_json_schema())}
    elif isinstance(schema, type) and (dataclasses.is_dataclass(schema) or _is_typeddict(schema)):
        try:
            hints = typing.get_type_hints(schema, include_extras=True)
        except Exception as e:                                 # an unresolvable forward reference
            raise LoadError(f"schema {schema.__qualname__}: cannot resolve its annotations ({e})") from e
        if dataclasses.is_dataclass(schema):
            names = [f.name for f in dataclasses.fields(schema)]
            kind = "dataclass"
        else:
            names = list(hints)
            kind = "typeddict"
        out = {kind: schema.__qualname__, "fields": {n: _type_schema(hints[n], defs, seen) for n in names}}
        if dataclasses.is_dataclass(schema):
            out["required"] = sorted(f.name for f in dataclasses.fields(schema)
                                     if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING)
        elif _is_typeddict(schema):
            out["required"] = sorted(getattr(schema, "__required_keys__", ()))
    elif isinstance(schema, dict):
        out = {"fields": {str(k): _type_schema(v, defs, seen) for k, v in sorted(schema.items(), key=lambda kv: str(kv[0]))}}
    else:
        out = {"type": _type_schema(schema, defs, seen)}
    if top and defs:
        out = {**out, "$defs": _plain(defs)}
    return _plain(strip_prose(out)) if top else _plain(out)


# --- signatures ---------------------------------------------------------------------------------
def signature_of(key: str) -> str:
    """The pinned signature text of a 'py:' target (unwrapped through its decorators)."""
    obj = inspect.unwrap(resolve(key))
    try:
        return f"{getattr(obj, '__qualname__', key)}{inspect.signature(obj)}"
    except (TypeError, ValueError) as e:
        raise LoadError(f"target {key}: no signature ({e})") from e


def signature_types(obj: Any, pins: Schemas) -> dict[str, Any]:
    """Resolved parameter and return types of a callable: {param: type, "return": type}.

    Read with typing.get_type_hints, so `from __future__ import annotations` text and aliases are
    resolved; a dataclass, TypedDict or pydantic model is pinned by its fields in the schema table."""
    fn = inspect.unwrap(obj)
    sig = inspect.signature(fn)
    src = fn.__init__ if inspect.isclass(fn) else fn
    try:
        hints: dict[str, Any] | None = typing.get_type_hints(src, include_extras=True)
    except Exception:                                          # an unresolvable name: keep the text
        hints = None
    out: dict[str, Any] = {}
    for name, p in sig.parameters.items():
        if p.annotation is inspect.Parameter.empty:
            continue
        out[name] = (_type_schema(hints[name], {}, set(), pins) if hints is not None and name in hints
                     else {"unresolved": str(p.annotation)})
    if sig.return_annotation is not inspect.Signature.empty and not inspect.isclass(fn):
        out["return"] = (_type_schema(hints["return"], {}, set(), pins) if hints is not None and "return" in hints
                         else {"unresolved": str(sig.return_annotation)})
    return _plain(out)


def _signature_pin(obj: Any, label: str, pins: Schemas) -> dict[str, Any]:
    fn = inspect.unwrap(obj)
    try:
        text = f"{getattr(fn, '__qualname__', label)}{inspect.signature(fn)}"
    except (TypeError, ValueError) as e:
        raise LoadError(f"{label}: no signature ({e})") from e
    pin: dict[str, Any] = {"signature": text}
    types = signature_types(obj, pins)
    if types:
        pin["types"] = types
    return pin


# --- contracts ----------------------------------------------------------------------------------
def _python_ref(name: str, ref: str) -> Any:
    mod_name, _, attr = ref.partition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as e:
        raise LoadError(f"contract {name}: cannot import {mod_name}: {e}") from e
    obj: Any = mod
    for part in [p for p in attr.split(".") if p]:
        if not hasattr(obj, part):
            raise LoadError(f"contract {name}: {mod_name} has no {attr}")
        obj = getattr(obj, part)
    return obj


_SHARED_SCHEMAS = "#/components/schemas/"
_PARAM_KEYS = ("name", "in", "required", "schema", "content", "style", "explode", "allowEmptyValue",
               "allowReserved")


def _openapi_operations(name: str, path: pathlib.Path, ops: list[str], pins: Schemas) -> dict[str, Any]:
    """The operations named by rules, keyed by operationId: method, path, and the schemas of their
    parameters, request body and responses. Shared component schemas go to the schema table once."""
    text = path.read_text()
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            spec = yaml.safe_load(text)
        except Exception as e:
            raise LoadError(f"contract {name}: {path.name} is neither JSON nor YAML ({e})") from e
    if not isinstance(spec, dict):
        raise LoadError(f"contract {name}: {path.name} is not an OpenAPI document")

    def lookup(ref: str) -> Any:
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise LoadError(f"contract {name}: unresolvable $ref {ref}")
            target = target[part]
        return target

    def schema(node: Any, stack: tuple[str, ...]) -> Any:
        """A schema object with local $refs resolved; a shared component becomes a table entry."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                if ref in stack:
                    return {"$ref": ref, "recursive": True}
                body = schema(lookup(ref), (*stack, ref))
                got = (pins.add({"name": f"{path.name}{ref[1:]}", "schema": body})
                       if ref.startswith(_SHARED_SCHEMAS) else body)
                rest = {k: schema(v, stack) for k, v in node.items() if k != "$ref" and k not in SCHEMA_PROSE}
                return {"allOf": [got], **rest} if rest else got
            return {k: (v if k in _DATA_KEYS else schema(v, stack)) for k, v in node.items()}
        if isinstance(node, list):
            return [schema(v, stack) for v in node]
        return node

    def deref(node: Any) -> Any:
        """A parameter, request body or response object behind a $ref."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
            if node["$ref"] in seen:
                raise LoadError(f"contract {name}: $ref cycle at {node['$ref']}")
            seen.add(node["$ref"])
            node = lookup(node["$ref"])
        return node

    def content(c: Any) -> dict[str, Any]:
        return {mt: schema((m or {}).get("schema"), ()) for mt, m in sorted((c or {}).items()) if isinstance(m, dict)}

    found: dict[str, Any] = {}
    for route, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not (isinstance(op, dict) and op.get("operationId") in ops):
                continue
            params: dict[tuple[str, str], Any] = {}
            for p in [*(item.get("parameters") or []), *(op.get("parameters") or [])]:
                p = deref(p)
                if isinstance(p, dict):
                    params[(str(p.get("in")), str(p.get("name")))] = {
                        k: (schema(v, ()) if k == "schema" else content(v) if k == "content" else v)
                        for k, v in p.items() if k in _PARAM_KEYS}
            entry: dict[str, Any] = {"method": method.upper(), "path": route}
            if params:
                entry["parameters"] = [params[k] for k in sorted(params)]
            if "requestBody" in op:
                rb = deref(op["requestBody"]) or {}
                entry["requestBody"] = {"required": bool(rb.get("required", False)),
                                        "content": content(rb.get("content"))}
            if op.get("responses"):
                entry["responses"] = {str(code): content((deref(r) or {}).get("content"))
                                      for code, r in sorted(op["responses"].items(), key=lambda kv: str(kv[0]))}
            found[op["operationId"]] = strip_prose(entry)
    missing = sorted(set(ops) - set(found))
    if missing:
        raise LoadError(f"contract {name}: {path.name} has no operation {', '.join(missing)}")
    return _plain(found)


def contract_hash(name: str, c: dict[str, Any], base_dir: pathlib.Path,
                  operations: list[str] | None = None, pins: Schemas | None = None) -> dict[str, Any]:
    """Pin one contract at contract granularity (see the module docstring). Without `pins`, schemas are
    inlined in the entry instead of going to a schema table."""
    pins = Schemas(inline=True) if pins is None else pins
    ref = c.get("ref")
    if not isinstance(ref, str) or not ref:
        raise LoadError(f"contract {name}: needs a string ref")
    ctype = c.get("type")
    entry: dict[str, Any] = {"type": ctype, "ref": ref}
    if ctype in PY_CONTRACTS or (ctype in SCHEMA_CONTRACTS and ":" in ref):
        obj = _python_ref(name, ref)
        if inspect.ismodule(obj):
            return entry                                # its targets and providers carry the pins
        if _is_schema_class(obj) or isinstance(obj, dict) or getattr(obj, "__inv_pydantic__", None) is not None:
            label = _class_ref(obj) if isinstance(obj, type) else ref
            entry["schema"] = pins.add({"name": label, **resolved_schema(obj)})
        elif isinstance(obj, type) and issubclass(obj, enum.Enum):
            entry["schema"] = pins.add({"name": _class_ref(obj), **_plain({"type": _type_schema(obj, {}, set())})})
        elif callable(obj):
            entry.update(_signature_pin(obj, f"contract {name}", pins))
        else:
            try:
                entry["hash"] = _sha(inspect.getsource(obj).encode())
            except (OSError, TypeError) as e:
                raise LoadError(f"contract {name}: cannot pin {ref} ({e})") from e
        return entry
    f = base_dir / ref
    if not f.is_file():
        raise LoadError(f"contract {name}: {ref} not found next to the rule file")
    if ctype == "openapi" and operations:
        entry["operations"] = _openapi_operations(name, f, sorted(set(operations)), pins)
    elif ctype == "sql":
        from .sql_constraints import constraints
        try:
            entry["constraints"] = constraints(f.read_text())
        except UnicodeDecodeError as e:
            raise LoadError(f"contract {name}: {ref} is not text ({e})") from e
    else:
        entry["hash"] = _sha(f.read_bytes())
    return entry


# --- building -----------------------------------------------------------------------------------
def normalize_semantic(sem: dict[str, Any], compares_to: Any = None) -> dict[str, Any]:
    """Fill the defaults the loader applies, so writing a default out (or leaving it out) is no change:
    enforcer on_violation/on_error, model_policy format_retries, semantic_resamples and feedback."""
    sem = dict(sem)
    sem.pop("status", None)                            # the entry holds it once
    if isinstance(sem.get("mediation"), dict):
        sem["mediation"] = {k: v for k, v in sem["mediation"].items() if k not in MEDIATION_DOC}
    if isinstance(sem.get("enforcers"), list):
        sem["enforcers"] = [({**ENFORCER_DEFAULTS, **e} if isinstance(e, dict) and e.get("mode") else e)
                            for e in sem["enforcers"]]
    pol = sem.get("model_policy")
    if isinstance(pol, dict):
        held = (compares_to if compares_to is not None else sem.get("compares_to")) == "held_data"
        pol = {"format_retries": 2, "semantic_resamples": 0 if held else 1, **pol}
        fb = pol.get("feedback")
        pol["feedback"] = {**FEEDBACK_DEFAULT, **fb} if isinstance(fb, dict) else (fb if "feedback" in pol
                                                                                 else dict(FEEDBACK_DEFAULT))
        sem["model_policy"] = pol
    return _plain(sem)


def rule_semantics(raw: dict[str, Any]) -> dict[str, Any]:
    """The part of a rule its meaning depends on; prose fields, id, rev and status are left out."""
    sem = {k: v for k, v in raw.items() if k not in (*DOC_FIELDS, "id", "rev", "evidence")}
    return normalize_semantic(sem)


def _doc_digests(raw: dict[str, Any]) -> dict[str, str]:
    out = {k: _sha(canonical(raw[k])) for k in DOC_FIELDS if k in raw}
    med = raw.get("mediation")
    if isinstance(med, dict):
        out.update({f"mediation.{k}": _sha(canonical(med[k])) for k in MEDIATION_DOC if k in med})
    return out


def _provider_pin(name: str, p: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    entry = {k: p.get(k) for k in ("type", "from", "args", "consistency")}
    src = p.get("from")
    if isinstance(src, str) and src.startswith("py:"):
        try:
            entry.update(_signature_pin(resolve(src), f"provider {name}", pins))
        except Exception as e:
            raise LoadError(f"provider {name}: cannot pin {src} ({e})") from e
    return _plain(entry)


def build(doc: dict[str, Any], base_dir: pathlib.Path) -> dict[str, Any]:
    """The lock for a rule document. Imports target modules; call after the file loads cleanly."""
    expanded = expand_applies_to(doc)
    declared = doc.get("targets") or {}
    pins = Schemas()
    rules: dict[str, Any] = {}
    keys: set[str] = set(declared)
    ops: dict[str, list[str]] = {}
    for raw in doc.get("rules") or []:
        subj = raw.get("subject") or {}
        tk = subj.get("target")
        if isinstance(subj.get("contract"), str) and isinstance(subj.get("operation"), str):
            ops.setdefault(subj["contract"], []).append(subj["operation"])
        edges = sorted(e["target"] for e in raw.get("enforcers") or [] if isinstance(e, dict) and "target" in e)
        rule_targets = sorted({*([tk] if tk else []), *edges, *expanded.get(raw["id"], [])})
        keys.update(rule_targets)
        rules[raw["id"]] = {"rev": raw.get("rev"), "status": raw.get("status"), "targets": rule_targets,
                            "semantic": rule_semantics(raw), "doc": _doc_digests(raw)}
    targets = {}
    for key in sorted(keys):
        t = declared.get(key, {"effects": "external"})
        pin: dict[str, Any] = {"effects": t.get("effects")}
        if t.get("transaction") is not None:
            pin["transaction"] = t["transaction"]
        obj = resolve(key)
        pin.update(_signature_pin(obj, f"target {key}", pins))
        schema = getattr(obj, "__inv_schema__", None)
        if schema is not None:
            label = _class_ref(schema) if isinstance(schema, type) else key
            pin["reply_schema"] = pins.add({"name": label, **resolved_schema(schema)})
        targets[key] = pin
    providers = {n: _provider_pin(n, p, pins) for n, p in sorted((doc.get("providers") or {}).items())
                 if isinstance(p, dict)}
    contracts = {n: contract_hash(n, c, base_dir, ops.get(n), pins)
                 for n, c in sorted((doc.get("contracts") or {}).items())}
    return _plain({"lock_version": LOCK_VERSION, "spec": doc.get("spec"), "rules": rules, "targets": targets,
                   "providers": providers, "contracts": contracts, "schemas": pins.table})


DUMP_WIDTH = 128


def dumps(lock: dict[str, Any]) -> str:
    """The lock as text: sorted keys, two-space indent, and any value that fits in DUMP_WIDTH columns
    on one line (so an enforcer or a clause is one line in review). Deterministic."""
    return _dump(lock, 0, "") + "\n"


def _dump(v: Any, indent: int, prefix: str) -> str:
    flat = json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    if not isinstance(v, (dict, list)) or not v or indent + len(prefix) + len(flat) <= DUMP_WIDTH:
        return flat
    pad = " " * (indent + 2)
    if isinstance(v, dict):
        items = []
        for k in sorted(v):
            key = json.dumps(k, ensure_ascii=False) + ": "
            items.append(pad + key + _dump(v[k], indent + 2, key))
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    return "[\n" + ",\n".join(pad + _dump(x, indent + 2, "") for x in v) + "\n" + " " * indent + "]"


# --- reading ------------------------------------------------------------------------------------
class LockError(LoadError):
    """The stored lock is missing, unreadable or malformed; the message names the file and the place."""


class LockVersionError(LockError):
    """The stored lock has a version this library cannot compare."""


def read(path: pathlib.Path) -> dict[str, Any]:
    """Read a lockfile; LockError when it is missing, not JSON, or not shaped like a lock."""
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise LockError(f"{path}: no lockfile ({e.strerror}); run `inv lock`") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LockError(f"{path}: not a valid lockfile: {e}") from e
    if not isinstance(data, dict):
        raise LockError(f"{path}: not a valid lockfile (expected a JSON object, got {type(data).__name__})")
    check_version(data, path)
    validate(data, path)
    return data


def check_version(lock: dict[str, Any], path: pathlib.Path | str = LOCK_NAME) -> int:
    """The lock's version; LockVersionError for a version newer than this library or one that is not an int."""
    v = lock.get("lock_version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise LockVersionError(f"{path}: lock_version {json.dumps(v)} is not a valid lock version")
    if v > LOCK_VERSION:
        raise LockVersionError(f"{path}: lock_version {v} was written by a newer invariants "
                               f"(this one reads up to {LOCK_VERSION}); upgrade invariants")
    return v


_ENTRY_SECTIONS = ("rules", "targets", "providers", "contracts", "schemas")
_ENTRY_SHAPES: dict[str, dict[str, Any]] = {"rules": {"semantic": dict, "doc": dict, "targets": list},
                 "targets": {"types": dict}, "providers": {"types": dict, "args": (dict, type(None))}}


def validate(lock: dict[str, Any], path: pathlib.Path | str = LOCK_NAME) -> None:
    """Check the shape a lock must have before it is compared; LockError naming the bad place."""
    def bad(where: str, want: str, got: Any) -> LockError:
        return LockError(f"{path}: malformed lock: {where} should be {want}, not {type(got).__name__} "
                         f"{json.dumps(got, default=str)[:60]}; re-create it with `inv lock` "
                         f"(delete it first if it cannot be repaired)")
    for sec in _ENTRY_SECTIONS:
        if sec not in lock or lock[sec] is None:
            continue
        val = lock[sec]
        if not isinstance(val, dict):
            raise bad(sec, "an object", val)
        for k, entry in val.items():
            if not isinstance(entry, dict):
                raise bad(f"{sec}.{k}", "an object", entry)
            for field, kind in _ENTRY_SHAPES.get(sec, {}).items():
                if field in entry and not isinstance(entry[field], kind):
                    raise bad(f"{sec}.{k}.{field}", "a list" if kind is list else "an object", entry[field])
    if "spec" in lock and lock["spec"] is not None and not isinstance(lock["spec"], str):
        raise bad("spec", "a string", lock["spec"])


def _refs_in(node: Any, found: list[str]) -> None:
    if _is_ref(node):
        found.append(node["$ref"])
    elif isinstance(node, dict):
        for v in node.values():
            _refs_in(v, found)
    elif isinstance(node, list):
        for v in node:
            _refs_in(v, found)


def schema_problems(lock: dict[str, Any]) -> list[str]:
    """What is wrong with a version-3 lock's schemas table: an entry whose key is not the sha256 of
    its body (hand-edited, or a bad merge), a `$ref` that names no entry, an entry nothing refers to.
    Empty when the table is sound (or the lock is older than version 3)."""
    if lock.get("lock_version") != LOCK_VERSION or not isinstance(lock.get("schemas", {}), dict):
        return []
    table: dict[str, Any] = lock.get("schemas") or {}
    out = [f"schemas.{k}: the key is not the sha256 of its body (edited by hand, or a bad merge)"
           for k, body in sorted(table.items()) if _sha(canonical(body)) != k]
    roots: list[str] = []
    for sec in ("rules", "targets", "providers", "contracts"):
        _refs_in(lock.get(sec) or {}, roots)
    reached: set[str] = set()
    work = list(roots)
    while work:
        r = work.pop()
        if r in reached:
            continue
        reached.add(r)
        if r not in table:
            out.append(f"$ref {r} names no entry in the schemas table")
            continue
        _refs_in(table[r], work)
    out += [f"schemas.{k}: no entry refers to it (an orphan)" for k in sorted(set(table) - reached)]
    return out


def without_damaged_schemas(lock: dict[str, Any]) -> dict[str, Any]:
    """The lock with schema entries whose key does not match their body dropped (a diff then says the
    old schema cannot be shown, rather than showing a hand-edited body as the old side)."""
    tab = lock.get("schemas") or {}
    return {**lock, "schemas": {k: v for k, v in tab.items() if _sha(canonical(v)) == k}}


# --- explaining changes -------------------------------------------------------------------------
@dataclasses.dataclass
class Change:
    """One difference between a stored lock and a fresh one."""

    where: str                     # e.g. "rule R-22", "target py:m.f", "contract api", "schema m.Reply"
    what: str                      # e.g. "bad", "enforcer decorator mode", "signature"
    old: Any = None
    new: Any = None
    weakened: bool = False
    note: str = ""

    def __str__(self) -> str:
        if self.what in ("added", "removed") or (self.old is None and self.new is None):
            body = self.what
        else:
            body = f"{self.what}: {_short(self.old)} -> {_short(self.new)}"
        tag = "  WEAKENED" if self.weakened else ""
        note = f" ({self.note})" if self.note else ""
        return f"{self.where}: {body}{tag}{note}"


def _short(v: Any, limit: int = 240) -> str:
    if v is None:
        return "(none)"
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)
    s = repr(s) if isinstance(v, str) else s
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _is_ref(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and isinstance(v.get("$ref"), str) and v["$ref"].startswith("sha256:")


def _json_paths(a: Any, b: Any, path: str = "",
                refs: list[tuple[str, str, str]] | None = None) -> list[tuple[str, Any, Any]]:
    """Leaf-level differences between two JSON values, as (path, old, new), compared type-strictly
    (1 and true differ). Lists of scalars diff as sets. With `refs`, two different schema-table
    references are collected there as (path, old digest, new digest) instead of being reported."""
    out: list[tuple[str, Any, Any]] = []
    if refs is not None and _is_ref(a) and _is_ref(b):
        if a["$ref"] != b["$ref"]:
            refs.append((path, a["$ref"], b["$ref"]))
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append((p, None, b[k]))
            elif k not in b:
                out.append((p, a[k], None))
            else:
                out += _json_paths(a[k], b[k], p, refs)
        return out
    if isinstance(a, list) and isinstance(b, list) and not same(a, b) and all(
            not isinstance(x, (dict, list)) for x in [*a, *b]):
        ka, kb = [canonical(x) for x in a], [canonical(x) for x in b]
        sa = [x for x, k in zip(a, ka) if k not in kb]
        sb = [x for x, k in zip(b, kb) if k not in ka]
        if sa and sb:                                  # ["int"] -> ["float"]: one replacement
            out.append((path, sa, sb))
        elif sb:
            out.append((f"{path} added", None, sb))
        elif sa:
            out.append((f"{path} removed", sa, None))
        return out or [(path, a, b)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            out += _json_paths(x, y, f"{path}[{i}]", refs)
        return out
    return [] if same(a, b) else [(path, a, b)]


# --- bad: canonical trees and polarity -----------------------------------------------------------
def _clauses(bad: Any) -> list[tuple[str, str]]:
    if isinstance(bad, str):
        return [(bad, "")]
    if not isinstance(bad, list):                  # a malformed stored lock: compared as text
        return [] if bad is None else [(json.dumps(bad), "")]
    out = []
    for c in bad:
        if isinstance(c, dict):
            out.append((str(c.get("when", "")), str(c.get("message", ""))))
        else:
            out.append((str(c), ""))
    return out


def _lit(v: Any) -> tuple[Any, ...]:
    """A literal tagged with its CEL type, so true, 1 and 1.0 (equal to Python) never compare equal,
    and -0.0 stays apart from 0.0."""
    if isinstance(v, bool):
        return ("lit", "bool", v)
    if isinstance(v, int):
        return ("lit", "int", v)
    if isinstance(v, float):
        return ("lit", "double", v, "-0") if v == 0 and math.copysign(1.0, v) < 0 else ("lit", "double", v)
    if v is None:
        return ("lit", "null", None)
    return ("lit", type(v).__name__, v)


def _numeric(n: Any) -> bool:
    """A node that can only be a number (so `+` on it is addition, not string or list concatenation)."""
    if not isinstance(n, tuple) or not n:
        return False
    if n[0] == "lit":
        return n[1] in ("int", "double")
    if n[0] == "bin" and n[1] in ("*", "/", "%", "-"):
        return True
    if n[0] == "nary" and n[1] == "*":
        return True
    if n[0] == "un" and n[1] == "-":
        return True
    return n[0] == "call" and n[1] in ("size", "int", "double", "uint", "inv.sum") and n[2] is None


def _mask(n: Any) -> str:
    """A sort key that ignores numeric literal values, so an edited threshold keeps its place."""
    if isinstance(n, tuple):
        if _num(n):
            return "#" + n[1]
        return "(" + ",".join(_mask(x) for x in n) + ")"
    if isinstance(n, list):
        return "[" + ",".join(_mask(x) for x in n) + "]"
    return repr(n)


def _order(xs: list[Any]) -> tuple[Any, ...]:
    return tuple(sorted(xs, key=lambda x: (_mask(x), repr(x))))


def _unique(xs: list[Any]) -> list[Any]:
    out: list[Any] = []
    for x in xs:
        if x not in out:                               # tagged literals: Python == is type-strict here
            out.append(x)
    return out


Typer = typing.Callable[[Any], "frozenset[str] | None"]
_INT64 = (-(2 ** 63), 2 ** 63 - 1)
_INTEGRAL = frozenset({"int", "uint"})
_MIRROR = {"<": ">", "<=": ">="}
_NEGATE = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}
_BOOL_OPS = frozenset({"&&", "||", "==", "!=", "<", "<=", ">", ">=", "in"})
_BOOL_CALLS = frozenset({"has", "exists", "all", "exists_one", "startsWith", "endsWith", "contains", "matches"})
_MACRO_CALLS = frozenset({"all", "exists", "exists_one", "map", "filter", "inv.unique_by"})


def make_typer(envs: Any) -> Typer | None:
    """The static CEL kinds of a subset expression in every one of a rule's type environments (one
    per enforcer, from the loader), or None when any environment cannot say (dyn, an error)."""
    if not envs:
        return None
    from . import typecheck as tc

    def typer(raw: Any) -> frozenset[str] | None:
        kinds: set[str] = set()
        for env in envs:
            try:
                t = tc.infer(raw, env)
            except Exception:
                return None
            if t.kinds is None or t.refused is not None:
                return None
            kinds |= t.kinds
        return frozenset(kinds)
    return typer


def clause_types(reg: Any) -> dict[str, Typer | None]:
    """Rule id -> a typer for its clauses, from a loaded registry's static type environments."""
    from .loader import _type_env
    out: dict[str, Typer | None] = {}
    for rid, rule in reg.rules.items():
        try:
            envs = [_type_env(rule, reg, i, names) for i, names in rule.names_for.items()]
        except Exception:
            envs = []
        out[rid] = make_typer(envs)
    return out


class _Canon:
    """canon_tree's walk. Rewrites that hold for every value CEL can give (De Morgan, `a < b` as
    `b > a`, `!(a == b)` as `a != b`, duplicate disjuncts, macro variable names) always apply; those
    that hold only for some types (`!(a < b)` as `a >= b`, which NaN breaks, and `a > 5` as
    `a >= 6`, which needs integers) apply only when both operands are provably of those types."""

    def __init__(self, typer: Typer | None = None) -> None:
        self.typer = typer

    def kinds(self, raw: Any, bound: frozenset[str]) -> frozenset[str] | None:
        if self.typer is None:
            return None
        from .cel import subset
        try:
            if any(r[0] in bound for r in subset.references(raw)):
                return None                            # reads a macro variable: no static type here
        except Exception:
            return None
        return self.typer(raw)

    def integral(self, raw: Any, bound: frozenset[str]) -> bool:
        """Provably an int or uint (or an error): an int literal, size(), int(), uint(), or arithmetic
        on those. A path the rule's declared types make int does not count (see below)."""
        if not isinstance(raw, tuple) or not raw:
            return False
        k = raw[0]
        if k == "lit":
            return type(raw[1]) is int
        if k == "un" and raw[1] == "-":
            return self.integral(raw[2], bound)
        if k == "call" and (raw[1] == "size" or (raw[1] in ("int", "uint") and raw[2] is None)):
            return True
        if k == "bin" and raw[1] in ("+", "-", "*", "/", "%"):
            return self.integral(raw[2], bound) and self.integral(raw[3], bound)
        # Declared annotations are deliberately NOT trusted here: Python does not enforce them at
        # run time, so a caller can pass 10000.5 to an `int` parameter and `> 10000` / `>= 10001`
        # would then differ. A weakening gate must not rest on an unchecked assumption.
        return False

    def nan_free(self, raw: Any, bound: frozenset[str]) -> bool:
        if isinstance(raw, tuple) and raw[:1] == ("un",) and raw[1] == "-":
            return self.nan_free(raw[2], bound)
        if isinstance(raw, tuple) and raw[:1] == ("lit",) and type(raw[1]) in (int, float):
            return True
        return self.integral(raw, bound)

    def boolean(self, c: Any) -> bool:
        """A canonical node that is provably bool (or an error)."""
        if not isinstance(c, tuple) or not c:
            return False
        if c[0] == "lit":
            return c[1] == "bool"
        if c[0] in ("bin", "nary"):
            return c[1] in _BOOL_OPS
        if c[0] == "un":
            return c[1] == "!"
        if c[0] == "call" and c[1] in _BOOL_CALLS:
            return True
        if c[0] in ("id", "sel"):
            kinds = self.kinds(c, frozenset())
            return kinds == frozenset({"bool"})
        return False

    def canon(self, n: Any, ren: dict[str, str]) -> Any:
        if isinstance(n, list):
            return [self.canon(x, ren) for x in n]
        if not isinstance(n, tuple) or not n:
            return n
        k = n[0]
        if k == "lit":
            return _lit(n[1])
        if k == "id":
            return ("id", ren.get(n[1], n[1]))
        if k == "un" and n[1] == "!":
            return self.negate(n[2], ren)
        if k == "un" and n[1] == "-":
            x = n[2]
            if x[0] == "lit" and type(x[1]) in (int, float):
                return _lit(-x[1])
            return ("un", "-", self.canon(x, ren))
        if k == "bin":
            return self.binary(n, ren)
        if k == "call" and n[1] in _MACRO_CALLS:
            return self.macro(n, ren)
        if k == "cond":
            return ("cond", *(self.canon(x, ren) for x in n[1:]))
        return tuple(self.canon(x, ren) if isinstance(x, (tuple, list)) else x for x in n)

    def binary(self, n: Any, ren: dict[str, str]) -> Any:
        op = n[1]
        bound = frozenset(ren)
        if op in _MIRROR:                              # a < b is b > a, for every value
            return self.binary(("bin", _MIRROR[op], n[3], n[2]), ren)
        if op in ("+", "*"):                           # commuted, never regrouped: (a + b) + c and
            pair = [self.canon(n[2], ren), self.canon(n[3], ren)]   # a + (b + c) differ in rounding
            if op == "+" and not any(_numeric(x) for x in pair):    # and in int overflow
                return ("bin", op, *pair)              # may be string or list concatenation: keep order
            return ("nary", op, _order(pair))
        if op in ("||", "&&"):
            flat: list[Any] = []
            stack = [n]
            while stack:
                x = stack.pop()
                if isinstance(x, tuple) and x[:2] == ("bin", op):
                    stack += [x[3], x[2]]
                    continue
                c = self.canon(x, ren)
                flat += list(c[2]) if isinstance(c, tuple) and c[:2] == ("nary", op) else [c]
            flat = _unique(flat)                           # a || a is a
            if len(flat) == 1 and self.boolean(flat[0]):
                return flat[0]
            return ("nary", op, _order(flat))
        left, right = self.canon(n[2], ren), self.canon(n[3], ren)
        if op == ">":                                  # integers: a > 5 is a >= 6
            if _int_lit(right) and self.integral(n[2], bound) and right[2] < _INT64[1]:
                return ("bin", ">=", left, _lit(right[2] + 1))
            if _int_lit(left) and self.integral(n[3], bound) and left[2] > _INT64[0]:
                return ("bin", ">=", _lit(left[2] - 1), right)
        if op in ("==", "!="):
            left, right = _order([left, right])
        if op == "in" and isinstance(right, tuple) and right[:1] == ("list",):
            right = ("list", list(_order(_unique(right[1]))))   # membership ignores order and repeats
        return ("bin", op, left, right)

    def negate(self, x: Any, ren: dict[str, str]) -> Any:
        """The canonical form of !x."""
        bound = frozenset(ren)
        if isinstance(x, tuple) and x:
            if x[0] == "un" and x[1] == "!":
                inner = self.canon(x[2], ren)
                if self.boolean(inner):
                    return inner                       # !!e is e for a bool e
                return ("un", "!", self.negate(x[2], ren))
            if x[0] == "bin" and x[1] in ("&&", "||"):     # De Morgan
                dual = "||" if x[1] == "&&" else "&&"
                return self.binary(("bin", dual, ("un", "!", x[2]), ("un", "!", x[3])), ren)
            if x[0] == "bin" and x[1] in ("==", "!="):
                return self.binary(("bin", "!=" if x[1] == "==" else "==", x[2], x[3]), ren)
            if x[0] == "bin" and x[1] in _NEGATE and self.nan_free(x[2], bound) and self.nan_free(x[3], bound):
                return self.binary(("bin", _NEGATE[x[1]], x[2], x[3]), ren)
            if x[0] == "lit" and isinstance(x[1], bool):
                return _lit(not x[1])
        return ("un", "!", self.canon(x, ren))

    def macro(self, n: Any, ren: dict[str, str]) -> Any:
        """A comprehension with its variable renamed by nesting depth, so exists(x, ...) and
        exists(y, ...) with the same body are the same tree."""
        receiver = n[2] is not None
        args = n[3]
        tgt, var, body = (n[2], args[0], args[1:]) if receiver else (args[0], args[1], args[2:])
        if not (isinstance(var, tuple) and var[:1] == ("id",)):
            return tuple(self.canon(x, ren) if isinstance(x, (tuple, list)) else x for x in n)
        name = f"${1 + max((int(v[1:]) for v in ren.values()), default=-1)}"
        inner = {**ren, var[1]: name}
        ct = self.canon(tgt, ren)
        cb = [self.canon(b, inner) for b in body]
        if receiver:
            return ("call", n[1], ct, [("id", name), *cb])
        return ("call", n[1], None, [ct, ("id", name), *cb])


def _int_lit(c: Any) -> bool:
    return isinstance(c, tuple) and len(c) == 3 and c[:2] == ("lit", "int")


def canon_tree(n: Any, typer: Typer | None = None) -> Any:
    """The canonical form of a CEL subset parse tree. Equal canonical trees are equivalent.

    Literals are tagged with their type. Chains of || && are flattened into sorted ("nary", op,
    operands), with repeated operands dropped; the two operands of a numeric + or * are sorted (a
    commuted pair) but never regrouped, since floating-point rounding and int overflow depend on the
    grouping; == != and `in` lists are sorted;
    `a < b` is written `b > a`; ! is pushed inward (De Morgan, !! dropped on a bool, !(a == b) is
    a != b); macro variables are named by depth. With a typer (the rule's declared types), a
    comparison of provable integers or NaN-free numbers is also normalised: `a > 5` is `a >= 6`,
    and `!(a < b)` is `a >= b`."""
    return _Canon(typer).canon(n, {})


def _clause_key(text: str, parse: Any, typer: Typer | None = None) -> Any:
    try:
        return canon_tree(parse(text), typer)
    except Exception:
        return ("text", " ".join(text.split()))


def _disjuncts(tree: Any) -> list[Any]:
    if isinstance(tree, tuple) and tree[:2] == ("nary", "||"):
        return list(tree[2])
    return [tree]


def _bad_changes(where: str, old: Any, new: Any, typer: Typer | None = None) -> list[Change]:
    """Clause by clause, on canonical trees. Whitespace, commuted operands, a disjunction split into
    clauses (or merged from them) and a repeated clause dropped are no change. A removed clause, or
    an edit that makes the rule match less, or one whose direction cannot be decided, is WEAKENED."""
    from .cel import subset
    a, b = _clauses(old), _clauses(new)
    ka = [_clause_key(w, subset.parse, typer) for w, _ in a]
    kb = [_clause_key(w, subset.parse, typer) for w, _ in b]
    out: list[Change] = []
    a_left = list(range(len(a)))
    b_left = list(range(len(b)))
    for i in list(a_left):                             # the same condition, perhaps a new message
        j = next((j for j in b_left if kb[j] == ka[i]), None)
        if j is None:
            continue
        a_left.remove(i)
        b_left.remove(j)
        if a[i][1] != b[j][1]:
            out.append(Change(where, f"bad message of {b[j][0]!r}", a[i][1], b[j][1]))
        elif a[i][0] != b[j][0]:
            out.append(Change(where, "bad (equivalent rewrite)", a[i][0], b[j][0], False,
                              "the same condition after normalisation"))
    for i in list(a_left):                             # a repeat of a clause that is still there
        if ka[i] in kb:
            a_left.remove(i)
            out.append(Change(where, "bad repeated clause removed", _label(a[i]), None, False,
                              "the same condition is still a clause; no change in what is flagged"))
    for j in list(b_left):
        if kb[j] in ka:
            b_left.remove(j)
            out.append(Change(where, "bad repeated clause added", None, _label(b[j]), False,
                              "the same condition is already a clause"))
    if not a_left and not b_left:
        return out
    if a_left and b_left:                              # regrouped: the same disjuncts in other clauses
        da = sorted(_unique([d for i in a_left for d in _disjuncts(ka[i])]), key=repr)
        db = sorted(_unique([d for j in b_left for d in _disjuncts(kb[j])]), key=repr)
        if da == db:
            out.append(Change(where, "bad clauses regrouped", [_label(a[i]) for i in a_left],
                              [_label(b[j]) for j in b_left], False,
                              "the same conditions, split into clauses or merged; no change in what is flagged"))
            return out
    pairs: list[tuple[int, int]] = []
    for i in list(a_left):                             # pair by message, then by position
        j = next((j for j in b_left if a[i][1] and b[j][1] == a[i][1]), None)
        if j is not None:
            pairs.append((i, j))
            a_left.remove(i)
            b_left.remove(j)
    if len(a_left) == len(b_left):
        pairs += list(zip(a_left, b_left))
        a_left, b_left = [], []
    for i, j in pairs:
        (w, m), (w2, _) = a[i], b[j]
        out.append(_direction_change(where, f"bad{' [' + m + ']' if m else ''}", w, w2, typer=typer))
    if a_left and b_left:                              # unequal counts: compare the leftovers as one disjunction
        w = " || ".join(f"({a[i][0]})" for i in a_left)
        w2 = " || ".join(f"({b[j][0]})" for j in b_left)
        out.append(_direction_change(where, "bad clauses", [_label(a[i]) for i in a_left],
                                     [_label(b[j]) for j in b_left], w, w2, typer))
        return out
    for i in a_left:
        out.append(Change(where, "bad clause removed", _label(a[i]), None, True))
    for j in b_left:
        out.append(Change(where, "bad clause added", None, _label(b[j])))
    return out


def _label(c: tuple[str, str]) -> str:
    return c[0] + (f" [{c[1]}]" if c[1] else "")


def _direction_change(where: str, label: str, old: Any, new: Any, w: str | None = None,
                      w2: str | None = None, typer: Typer | None = None) -> Change:
    from .cel import subset
    direction = compare_bad(old if w is None else w, new if w2 is None else w2, subset.parse, typer)
    if direction == "same":
        return Change(where, label, old, new, False, "the same condition after normalisation")
    if direction == "weaker":
        return Change(where, label, old, new, True, "the condition now matches fewer cases")
    if direction == "stronger":
        return Change(where, label, old, new, False, "the condition now matches more cases")
    sites = _new_sites_text(old if w is None else w, new if w2 is None else w2, typer)
    if sites:
        return Change(where, label, old, new, True,
                      f"the new condition can raise an error the old one could not ({', '.join(sites)}); "
                      "with on_error: fail_open_and_page an error lets the call through, so this is "
                      "treated as a possible weakening")
    return Change(where, label, old, new, True, "the direction of the change cannot be decided; "
                                                "treated as a possible weakening")


def _new_sites_text(old: str, new: str, typer: Typer | None) -> list[str]:
    """new_error_sites() for an edit the polarity walk would call stronger, else []."""
    from .cel import subset
    try:
        ta, tb = canon_tree(subset.parse(old), typer), canon_tree(subset.parse(new), typer)
    except Exception:
        return []
    effects: list[int] = []
    added: list[Any] = []
    if not _walk(ta, tb, 1, effects, typer, added) or not effects or not all(e > 0 for e in effects):
        return []
    return new_error_sites(ta, tb, added)


# polarity analysis over canonical CEL subset trees ------------------------------------------------
_CMP = {">": (1, -1), ">=": (1, -1), "<": (-1, 1), "<=": (-1, 1)}
_OP_STRENGTH = {(">", ">="): 1, (">=", ">"): -1, ("<", "<="): 1, ("<=", "<"): -1}
_FALSE = ("lit", "bool", False)


def compare_bad(old: str, new: str, parse: Any, typer: Typer | None = None) -> str:
    """'same' | 'stronger' | 'weaker' | 'unknown': does `new` flag the same cases as `old`, more, fewer,
    or cannot it be decided?

    Both are compared as canonical trees (canon_tree, with the rule's declared types when a typer is
    given), so whitespace, commuted operands, mirrored comparisons, De Morgan, `!!`, repeated
    disjuncts, renamed macro variables and regrouped || && chains are the same. Otherwise decided only when
    the trees have the same shape and differ in numeric literals of one type, list literal elements,
    strictness of a comparison, or in a disjunct or conjunct added or removed. Polarity: && || keep
    it, ! flips it, the left of > flips relative to the right, + keeps it, the right of - flips it.
    `*` and `/` keep or flip it only when the other operand is provably non-negative (a non-negative
    literal, size(), uint); otherwise the sign is unknown and so is the direction. The list on the
    right of `in` keeps it, exists/all keep it.

    An edit that would be 'stronger' but adds a sub-expression that can raise where the old tree
    could not (a new field read, divisor, arithmetic, index or call; see new_error_sites) is
    'unknown': under on_error: fail_open_and_page the error would let through a call the old rule
    rejected. A disjunct or conjunct added to an existing || or && is exempt.
    """
    if old.strip() == new.strip():
        return "same"
    try:
        ta, tb = canon_tree(parse(old), typer), canon_tree(parse(new), typer)
    except Exception:
        if new.strip() == "false":
            return "weaker"
        if old.strip() == "false":
            return "stronger"
        return "unknown"
    if ta == tb:
        return "same"
    if tb == _FALSE:
        return "weaker"
    if ta == _FALSE:
        return "stronger"
    effects: list[int] = []
    added: list[Any] = []
    if not _walk(ta, tb, 1, effects, typer, added) or not effects:
        return "unknown"
    if all(e > 0 for e in effects):
        return "unknown" if new_error_sites(ta, tb, added) else "stronger"
    if all(e < 0 for e in effects):
        return "weaker"
    return "unknown"


_TOTAL_CALLS = frozenset({"has"})


def _error_sites(n: Any, ctx: tuple[Any, ...], out: list[tuple[Any, ...]], skip: set[int]) -> None:
    """Every sub-expression of a canonical tree that can raise by itself: a field or variable read
    (a missing key, a read on null), an index, a division or modulus (by zero, int64 min / -1),
    arithmetic (int overflow) and a function call (a bad argument). Each is keyed by the macro
    targets it sits under, so `x.f` in one comprehension is not `x.f` in another. Nodes whose id()
    is in `skip` are left out with all they contain."""
    if not isinstance(n, tuple) or not n or id(n) in skip:
        if isinstance(n, list):
            for x in n:
                _error_sites(x, ctx, out, skip)
        return
    k = n[0]
    if not isinstance(k, str):                         # a map item: (key, value)
        for x in n:
            _error_sites(x, ctx, out, skip)
        return
    if k == "lit":
        return
    if k == "id":
        if not str(n[1]).startswith("$"):              # macro variables are always bound
            out.append((ctx, n))
        return
    if k == "call" and n[1] in _MACRO_CALLS:
        receiver = n[2] is not None
        args = n[3]
        tgt, var, body = (n[2], args[0], args[1:]) if receiver else (args[0], args[1], args[2:])
        if isinstance(var, tuple) and var[:1] == ("id",):
            _error_sites(tgt, ctx, out, skip)
            for b in body:
                _error_sites(b, (*ctx, tgt), out, skip)
            return
    if k == "call" and n[1] == "has" and n[2] is None and n[3] and n[3][0][:1] == ("sel",):
        _error_sites(n[3][0][1], ctx, out, skip)       # has(a.b) reads a, and tests b
        return
    own = k in ("sel", "idx") or (k == "call" and n[1] not in _TOTAL_CALLS) \
        or (k == "un" and n[1] == "-") \
        or (k in ("bin", "nary") and n[1] in ("+", "-", "*"))
    if own and k in ("bin", "nary") and n[1] in ("+", "-", "*"):
        b = _magnitude(n)                              # literals and size(): provably in int64
        own = b is None or b >= 2 ** 63
    if k == "bin" and n[1] in ("/", "%"):
        d = n[3]                                       # a non-zero literal divisor other than -1
        own = not (_num(d) and d[2] not in (0, -1))    # cannot fail; anything else can
    if own:
        out.append((ctx, n))
    for x in n[1:]:
        if isinstance(x, (tuple, list)):
            _error_sites(x, ctx, out, skip)


def _magnitude(n: Any) -> int | None:
    """An upper bound on |n| for arithmetic over int literals and size() (at most 2**32: no list,
    map or string in a call's arguments is longer), or None when there is none."""
    if _num(n) and n[1] == "int" and len(n) == 3:
        return abs(n[2])
    if isinstance(n, tuple) and n[:2] == ("call", "size") and n[2] is None:
        return 2 ** 32
    if isinstance(n, tuple) and n[0] in ("bin", "nary") and n[1] in ("+", "-", "*"):
        parts = [_magnitude(x) for x in (n[2] if n[0] == "nary" else n[2:4])]
        if any(x is None for x in parts):
            return None
        total = 1 if n[1] == "*" else 0
        for x in parts:
            total = total * x if n[1] == "*" else total + x   # type: ignore[operator]
        return total
    return None


def _describe_site(n: Any) -> str:
    path = []
    x = n
    while isinstance(x, tuple) and x[:1] == ("sel",):
        path.append(x[2])
        x = x[1]
    if isinstance(x, tuple) and x[:1] == ("id",) and path:
        return "a read of " + ".".join([str(x[1]), *reversed(path)])
    k = n[0]
    if k == "id":
        return f"a read of {n[1]}"
    if k == "sel":
        return f"a read of .{n[2]}"
    if k == "idx":
        return "an index"
    if k == "call":
        return f"a call of {n[1]}()"
    if k == "bin" and n[1] in ("/", "%"):
        return "a division" if n[1] == "/" else "a modulus"
    return f"arithmetic ({n[1]})"


def new_error_sites(old: Any, new: Any, exempt: typing.Sequence[Any] = ()) -> list[str]:
    """The sub-expressions of canonical tree `new` that can raise an error and have no equal in
    `old`, described for a reviewer. Operands in `exempt` (a disjunct or conjunct added to an
    existing || or &&) are left out: wherever the old condition held, the operands it already had
    decide the || or && and absorb an error in the new one, as CEL's commutative || and && do.

    Under on_error: fail_open_and_page a rule that errors lets the call through, so an edit that
    flags more cases but can now raise where it used to fire is not a strengthening."""
    so: list[tuple[Any, ...]] = []
    sn: list[tuple[Any, ...]] = []
    _error_sites(old, (), so, set())
    _error_sites(new, (), sn, {id(x) for x in exempt})
    out: list[str] = []
    for site in sn:
        if site not in so:
            d = _describe_site(site[1])
            if d not in out:
                out.append(d)
    return [d for d in out if not any(e.startswith(d + ".") for e in out)]   # a.b.c covers a.b


def _num(n: Any) -> bool:
    """A tagged int or double literal (canonical form)."""
    return isinstance(n, tuple) and len(n) >= 3 and n[0] == "lit" and n[1] in ("int", "double")


def _nonneg(n: Any, typer: Typer | None) -> bool:
    """A canonical node that is provably >= 0 (or an error): a non-negative literal, size(), uint(),
    a uint-typed path, or a product, sum or quotient of those."""
    if not isinstance(n, tuple) or not n:
        return False
    if _num(n):
        return n[2] >= 0 and len(n) == 3
    if n[0] == "call":
        return n[1] == "size" or (n[1] == "uint" and n[2] is None)
    if n[0] == "nary" and n[1] in ("*", "+"):
        return all(_nonneg(x, typer) for x in n[2])
    if n[0] == "bin" and n[1] == "/":
        return _nonneg(n[2], typer) and _nonneg(n[3], typer)
    if n[0] in ("id", "sel") and typer is not None:
        kinds = typer(n)
        return bool(kinds) and kinds <= {"uint"}      # type: ignore[operator]
    return False


def _walk(a: Any, b: Any, pol: int, out: list[int], typer: Typer | None = None,
          added: list[Any] | None = None) -> bool:
    """Collect the effect (+1 stronger, -1 weaker) of each difference; False when undecidable.
    pol 0: the direction at this position is unknown, so only an unchanged subtree is decidable."""
    if a == b:
        return True
    if _num(a) and _num(b):
        if pol == 0 or a[1] != b[1] or len(a) != 3 or len(b) != 3 or a[2] == b[2]:
            return False                               # 1 vs 1.0 (or 0.0 vs -0.0): a type change
        out.append(pol if b[2] > a[2] else -pol)
        return True
    if not (isinstance(a, tuple) and isinstance(b, tuple)) or not a or not b:
        return False
    if a[0] == "nary" and b[0] != "nary" and a[1] in ("||", "&&"):     # `x && y` -> `x`: one operand left
        b = ("nary", a[1], (b,))
    elif b[0] == "nary" and a[0] != "nary" and b[1] in ("||", "&&"):
        a = ("nary", b[1], (a,))
    if a[0] != b[0]:
        return False
    kind = a[0]
    if kind == "nary":
        if a[1] != b[1] or pol == 0:
            return False
        xa, xb = list(a[2]), list(b[2])
        common = []
        for x in list(xa):                             # operands present on both sides
            if x in xb:
                xa.remove(x)
                xb.remove(x)
                common.append(x)
        sub = pol
        if a[1] == "*":                                # monotone in a factor only if the rest is >= 0
            others = common if len(xa) == 1 else [*common, *xa, *xb]
            if not all(_nonneg(x, typer) for x in others):
                sub = 0
        if len(xa) == len(xb):
            return all(_walk(x, y, sub, out, typer, added) for x, y in zip(xa, xb))
        if a[1] in ("||", "&&") and (not xa or not xb):
            more = 1 if xb else -1                     # operands added (1) or removed (-1)
            if added is not None:                      # where the old tree fired, the old operands
                added += xb                            # decide || / && and absorb an error in these
            out.append(more * pol if a[1] == "||" else -more * pol)
            return True
        return False
    if kind == "bin":
        op, op2 = a[1], b[1]
        if op != op2:
            step = _OP_STRENGTH.get((op, op2))
            if step is None or pol == 0:
                return False
            out.append(step * pol)                     # > to >= flags the boundary too
            lp, rp = _CMP[op]
            return (_walk(a[2], b[2], pol * lp, out, typer, added)
                    and _walk(a[3], b[3], pol * rp, out, typer, added))
        if op in ("&&", "||", "+"):
            return _walk(a[2], b[2], pol, out, typer, added) and _walk(a[3], b[3], pol, out, typer, added)
        if op == "-":
            return _walk(a[2], b[2], pol, out, typer, added) and _walk(a[3], b[3], -pol, out, typer, added)
        if op == "/":                                  # x / k: up in x if k >= 0, down in k if x >= 0
            lp = pol if _nonneg(a[3], typer) and _nonneg(b[3], typer) else 0
            rp = -pol if _nonneg(a[2], typer) and _nonneg(b[2], typer) and _nonneg(a[3], typer) \
                and _nonneg(b[3], typer) else 0
            return _walk(a[2], b[2], lp, out, typer, added) and _walk(a[3], b[3], rp, out, typer, added)
        if op in _CMP:
            lp, rp = _CMP[op]
            return (_walk(a[2], b[2], pol * lp, out, typer, added)
                    and _walk(a[3], b[3], pol * rp, out, typer, added))
        if op == "in":
            if a[2] != b[2]:
                return False
            la, lb = a[3], b[3]
            if la[0] == lb[0] == "list":
                xa, xb = list(la[1]), list(lb[1])
                grown, shrunk = [x for x in xb if x not in xa], [x for x in xa if x not in xb]
                if pol == 0 or (grown and shrunk):
                    return False
                if grown:
                    out.append(pol)
                if shrunk:
                    out.append(-pol)
                return True
            return _walk(la, lb, pol, out, typer, added)
        return False                                  # == != % : not monotone
    if kind == "un":
        if a[1] != b[1]:
            return False
        return _walk(a[2], b[2], -pol, out, typer, added)    # ! and unary - both flip
    if kind == "call":
        if a[1] != b[1] or a[2] != b[2] or len(a[3]) != len(b[3]):
            return False
        if a[1] in ("exists", "all") and a[2] is not None and a[3][0] == b[3][0]:
            return _walk(a[3][1], b[3][1], pol, out, typer, added)
        return False
    if kind == "cond":
        return (a[1] == b[1] and _walk(a[2], b[2], pol, out, typer, added)
                and _walk(a[3], b[3], pol, out, typer, added))
    return False


# --- per-field changes --------------------------------------------------------------------------
def _field_change(where: str, label: str, loc: str, old: Any, new: Any) -> list[Change]:
    """One field's change, judged by FIELD_POLICY[loc]. Dict values are shown per changed key."""
    pol = policy(loc)
    note = next((NOTES[k] for k in (loc, loc.rsplit(".", 1)[0]) if k in NOTES), "")
    if pol in (NEUTRAL, PIN, PROSE, COMPUTED, ANALYSED):
        return [Change(where, f"{label}{'.' + p if p else ''}", x, y)
                for p, x, y in _json_paths(old, new)] if isinstance(old, dict) and isinstance(new, dict) \
            else [Change(where, label, old, new)]
    if pol == RANKED:
        rank = RANKS.get(loc, {})
        ko, kn = (v if isinstance(v, str) else json.dumps(v) for v in (old, new))
        if ko in rank and kn in rank:
            weak = rank[kn] < rank[ko]
            return [Change(where, label, old, new, weak, note if weak else "")]
        return [Change(where, label, old, new, True, "an unranked value; treated as a possible weakening")]
    if pol == MORE_IS_WEAKER:
        if isinstance(old, int) and isinstance(new, int) and not isinstance(old, bool) and not isinstance(new, bool):
            weak = new > old
            return [Change(where, label, old, new, weak, note if weak else "")]
        return [Change(where, label, old, new, True, note or "treated as a possible weakening")]
    if pol == PRESENCE:
        weak = new is None or (old is not None)        # removed, or changed to another value
        return [Change(where, label, old, new, weak, note if weak else "")]
    # WEAKENING (and anything unlisted)
    why = note or "an unclassified field; treated as a possible weakening"
    if isinstance(old, dict) and isinstance(new, dict):
        return [Change(where, f"{label}.{p}", x, y, True, why) for p, x, y in _json_paths(old, new)]
    return [Change(where, label, old, new, True, why)]


def _enforcer_key(e: Any, i: int) -> str:
    if not isinstance(e, dict):
        return f"#{i}"
    return f"{e.get('point')}" + (f" {e['target']}" if e.get("target") else "")


def _keyed(enfs: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, e in enumerate(enfs or []):
        k = _enforcer_key(e, i)
        while k in out:
            k += "'"
        out[k] = e if isinstance(e, dict) else {"value": e}
    return out


def _mediation_changes(where: str, old: Any, new: Any, subject_old: Any = None,
                       subject_new: Any = None) -> list[Change]:
    from .scan import table_specs, ScanConfigError
    a, b = old if isinstance(old, dict) else {}, new if isinstance(new, dict) else {}
    out = []

    def as_list(v: Any) -> list[str]:
        return [v] if isinstance(v, str) else list(v or [])
    sinks_a, sinks_b = as_list(a.get("sink")), as_list(b.get("sink"))
    # the doors the scan uses: mediation.door, else subject.target
    doors_a = as_list(a.get("door")) or ([subject_old] if isinstance(subject_old, str) else [])
    doors_b = as_list(b.get("door")) or ([subject_new] if isinstance(subject_new, str) else [])
    if sinks_a != sinks_b:
        gone = [x for x in sinks_a if x not in sinks_b]
        out.append(Change(where, "mediation.sink", sinks_a or None, sinks_b or None, bool(gone),
                          "a protected sink is no longer checked" if gone else ""))
    if as_list(a.get("door")) != as_list(b.get("door")) and (doors_a != doors_b or not sinks_a):
        came = [x for x in doors_b if x not in doors_a]
        weak = bool(sinks_a) and bool(came)           # a new door on an existing scan lets more paths through
        out.append(Change(where, "mediation.door", as_list(a.get("door")) or None, as_list(b.get("door")) or None,
                          weak, "more doors may reach the sink" if weak else ""))
    if not same(a.get("tables"), b.get("tables")):
        try:
            ta = table_specs(a["tables"]) if a.get("tables") else {}
            tb = table_specs(b["tables"]) if b.get("tables") else {}
        except ScanConfigError:
            ta, tb = {"?": frozenset({"?"})}, {}             # unparseable: treat as a possible weakening
        weak = any(t not in tb or not (ops <= tb[t]) for t, ops in ta.items())
        out.append(Change(where, "mediation.tables", a.get("tables"), b.get("tables"), weak,
                          "raw SQL to a table, or a statement kind, is no longer forbidden" if weak else ""))
    allow_a, allow_b = as_list(a.get("allow")), as_list(b.get("allow"))
    if sorted(allow_a) != sorted(allow_b):
        came = [x for x in allow_b if x not in allow_a]
        out.append(Change(where, "mediation.allow", allow_a or None, allow_b or None, bool(came),
                          f"a back door is now allowed: {', '.join(came)}" if came else ""))
    ra, rb = a.get("residual_paths"), b.get("residual_paths")
    if not same(ra, rb):
        la = ra if isinstance(ra, list) else ([] if ra is None else [ra])
        lb = rb if isinstance(rb, list) else ([] if rb is None else [rb])
        gone = [x for x in la if not any(same(x, y) for y in lb)]
        out.append(Change(where, "mediation.residual_paths", ra, rb, bool(gone),
                          "a documented residual path was removed; `inv status` withholds `held` while "
                          "one is listed, so this can raise the rule's standing" if gone else ""))
    for k in sorted((set(a) | set(b)) - {"sink", "door", "tables", "residual_paths", "allow"}):
        if not same(a.get(k), b.get(k)):
            out += _field_change(where, f"mediation.{k}", f"rule.mediation.{k}", a.get(k), b.get(k))
    return out


def _model_policy_changes(where: str, old: Any, new: Any) -> list[Change]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return _field_change(where, "model_policy", "rule.model_policy.whole", old, new)
    out: list[Change] = []
    for k in sorted(set(old) | set(new)):
        x, y = old.get(k), new.get(k)
        if same(x, y):
            continue
        loc = f"modelPolicy.{k}"
        if isinstance(x, dict) and isinstance(y, dict):
            for p, xv, yv in _json_paths(x, y):
                sub = f"{loc}.{p.split('.')[0].split(' ')[0]}"
                out += _field_change(where, f"model_policy.{k}.{p}", sub if sub in FIELD_POLICY else loc, xv, yv)
        else:
            out += _field_change(where, f"model_policy.{k}", loc, x, y)
    return out


def _rule_changes(rid: str, a: dict[str, Any], b: dict[str, Any], typer: Typer | None = None) -> list[Change]:
    where = f"rule {rid}"
    out: list[Change] = []
    if not same(a.get("status"), b.get("status")):
        out += _field_change(where, "status", "rule.status", a.get("status"), b.get("status"))
    if not same(a.get("rev"), b.get("rev")):
        out.append(Change(where, "rev", a.get("rev"), b.get("rev")))
    ta, tb = a.get("targets") or [], b.get("targets") or []
    if not same(ta, tb):
        gone = [t for t in ta if t not in tb]
        out.append(Change(where, "targets", ta, tb, bool(gone), "a target is no longer covered" if gone else ""))
    sa, sb = a.get("semantic") or {}, b.get("semantic") or {}
    for field in sorted(set(sa) | set(sb)):
        va, vb = sa.get(field), sb.get(field)
        if same(va, vb) or field == "status":
            continue
        if field == "bad":
            out += _bad_changes(where, va, vb, typer)
        elif field == "enforcers":
            ka, kb = _keyed(va), _keyed(vb)
            for k in sorted(set(ka) | set(kb)):
                ea, eb = ka.get(k), kb.get(k)
                if eb is None:
                    out.append(Change(where, f"enforcer {k} removed", ea, None, True))
                elif ea is None:
                    out.append(Change(where, f"enforcer {k} added", None, eb))
                else:
                    for f in sorted(set(ea) | set(eb)):
                        if same(ea.get(f), eb.get(f)):
                            continue
                        cs = _field_change(where, f"enforcer {k} {f}", f"enforcer.{f}", ea.get(f), eb.get(f))
                        if f == "on_error" and any(c.weakened for c in cs):
                            for c in cs:
                                c.note = "fail_open: an error in the rule lets the call through"
                        out += cs
        elif field == "mediation":
            out += _mediation_changes(where, va, vb, (sa.get("subject") or {}).get("target"),
                                      (sb.get("subject") or {}).get("target"))
        elif field == "model_policy":
            out += _model_policy_changes(where, va, vb)
        elif field == "falsified_by":
            gone = [x for x in (va or []) if x not in (vb or [])]
            out.append(Change(where, "falsified_by", va, vb, False, "a falsifier was dropped" if gone else ""))
        elif field == "applies_to":
            if same(ta, tb):                           # the targets list shows coverage changes
                out.append(Change(where, "applies_to", va, vb))
        else:
            out += _field_change(where, field, f"rule.{field}", va, vb)
    da, db = a.get("doc") or {}, b.get("doc") or {}
    for f in sorted(set(da) | set(db)):
        if da.get(f) != db.get(f):
            verb = "added" if f not in da else "removed" if f not in db else "edited"
            out.append(Change(where, f"{f} text {verb}", note="prose only; the rule file diff shows it"))
    return out


def _entry_changes(where: str, kind: str, a: dict[str, Any], b: dict[str, Any],
                   refs: list[tuple[str, str, str, str]]) -> list[Change]:
    """A target, provider or contract entry, field by field, judged by FIELD_POLICY[kind.field]."""
    out: list[Change] = []
    for f in sorted(set(a) | set(b)):
        x, y = a.get(f), b.get(f)
        if same(x, y):
            continue
        loc = f"{kind}.{f}"
        if loc == "contract.constraints":                  # a sql contract: losing a constraint weakens
            for p, xv, yv in _json_paths(x if isinstance(x, list) else [], y if isinstance(y, list) else [], f):
                weak = not p.endswith(" added")
                out.append(Change(where, p, xv, yv, weak,
                                  "the database now accepts rows it refused" if weak else ""))
            continue
        if policy(loc) == PIN:
            found: list[tuple[str, str, str]] = []
            for p, xv, yv in _json_paths(x, y, f, found):
                out.append(Change(where, p, xv, yv))
            refs += [(f"{where} {p}", p, da, db) for p, da, db in found]
            continue
        cs = _field_change(where, f, loc, x, y)
        if loc == "target.effects":
            for c in cs:
                if c.weakened:
                    c.note = "declared pure, so the scan no longer needs a sink for it"
        if loc == "provider.consistency":
            for c in cs:
                if c.weakened:
                    c.note = "the provider may now read staler data"
        out += cs
    return out


# --- pydantic constraints: the rules a codebase already has ---------------------------------------
# A pinned pydantic schema is the model's JSON Schema, so its Field(gt=..., le=..., max_length=...),
# Literal and required constraints are pinned too. A change that lets the model accept more input is
# a weakening; a change that only tightens it is not. Other schema changes stay plain pin changes.
UPPER_BOUNDS = frozenset({"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties", "maxContains"})
LOWER_BOUNDS = frozenset({"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties", "minContains"})
EXACT_CONSTRAINTS = frozenset({"pattern", "format", "multipleOf", "const", "type", "uniqueItems",
                               "additionalProperties", "decimal_places", "max_digits"})
_NAME_SEGMENTS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def constraint_weakening(path: str, old: Any, new: Any) -> str | None:
    """Why a change at `path` of a pinned pydantic schema lets the model accept more input, or None.

    `path` is as _json_paths reports it inside one schema-table entry ("pydantic.properties.amount.maximum",
    "pydantic.required removed"). Only schemas pinned from a pydantic model are judged."""
    if not path.startswith("pydantic."):
        return None
    base, _, suffix = path.partition(" ")                        # "x.enum added" / "x.required removed"
    segs = base.split(".")
    key, parent = segs[-1], (segs[-2] if len(segs) > 1 else "")
    if parent in _NAME_SEGMENTS:                                 # a field (or model) entry itself
        if new is None and parent == "properties":
            return f"field {key} removed: its constraints no longer apply"
        return None
    if key in UPPER_BOUNDS:
        if new is None:
            return f"{key} removed: no upper bound"
        if old is not None and _number(old) and _number(new) and new > old:
            return f"{key} raised: larger values pass"
        return None
    if key in LOWER_BOUNDS:
        if new is None:
            return f"{key} removed: no lower bound"
        if old is not None and _number(old) and _number(new) and new < old:
            return f"{key} lowered: smaller values pass"
        return None
    if key == "enum":
        if suffix == "added" or (suffix == "" and old is not None):
            return "enum widened: more values pass" if new is not None else "enum removed: any value passes"
        return None
    if key == "required":
        if suffix == "removed" or (suffix == "" and old is not None and new is None):
            return "a field is no longer required"
        if suffix == "" and old is not None and new is not None:
            return "the required fields changed"
        return None
    if key in EXACT_CONSTRAINTS:
        if old is None:
            return None                                          # a new constraint only tightens
        if key == "additionalProperties" and new is False:
            return None
        if key == "uniqueItems" and new is True:
            return None
        return f"{key} {'removed' if new is None else 'changed'}: the model may accept more input"
    if key in ("anyOf", "oneOf") and old is not None:
        return f"{key} changed: the model may accept more input"
    return None


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
        for i, (p, x, y) in enumerate(diffs):
            why = constraint_weakening(p, x, y)
            note = "; ".join(n for n in (why, used if i == 0 else "") if n)
            out.append(Change(where, p, x, y, weakened=why is not None, note=note))
        for p, xa, xb in nested:
            work.append(((xa, xb), [f"{where} {p}"]))
        if not diffs and not nested:
            out.append(Change(where, "schema", da, db, note=used))
    return out


def changes(old: dict[str, Any], new: dict[str, Any],
            types: dict[str, Typer | None] | None = None) -> list[Change]:
    """Every semantic difference between a stored version-3 lock and a fresh one; empty when fresh.
    types (clause_types of the loaded rule file) lets the `bad` comparison use declared types."""
    out: list[Change] = []
    for k in ("lock_version", "spec"):
        if not same(old.get(k), new.get(k)):
            out.append(Change("lock", k, old.get(k), new.get(k)))
    ra, rb = old.get("rules") or {}, new.get("rules") or {}
    for rid in sorted(set(ra) | set(rb)):
        if rid not in rb:
            out.append(Change(f"rule {rid}", "removed", weakened=True, note="the rule no longer exists"))
        elif rid not in ra:
            out.append(Change(f"rule {rid}", "added"))
        else:
            out += _rule_changes(rid, ra[rid], rb[rid], (types or {}).get(rid))
    refs: list[tuple[str, str, str, str]] = []
    for section, kind, removed_weak in (("targets", "target", False), ("providers", "provider", False),
                                        ("contracts", "contract", False)):
        sa, sb = old.get(section) or {}, new.get(section) or {}
        for name in sorted(set(sa) | set(sb)):
            where = f"{kind} {name}"
            if name not in sb:
                out.append(Change(where, "removed", weakened=removed_weak))
            elif name not in sa:
                out.append(Change(where, "added"))
            else:
                out += _entry_changes(where, kind, sa[name], sb[name], refs)
    out += _schema_changes(refs, old.get("schemas") or {}, new.get("schemas") or {})
    return out


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Readable differences between a stored lock and a fresh one; empty when fresh."""
    return [str(c) for c in changes(old, new)]


# --- migration ----------------------------------------------------------------------------------
def migrate_v1(old: dict[str, Any], doc: dict[str, Any]) -> list[Change]:
    """What a version-1 lock can still tell about the current rule file: which rules changed since it
    was pinned (v1 kept only a whole-rule hash, so what changed cannot be shown) and which went away.
    Every such rule is a possible weakening. Signature changes are reported but are not weakenings."""
    out: list[Change] = []
    raws = {r["id"]: r for r in doc.get("rules") or [] if isinstance(r, dict) and "id" in r}
    for rid, entry in sorted((old.get("rules") or {}).items()):
        if rid not in raws:
            out.append(Change(f"rule {rid}", "removed", weakened=True, note="the rule no longer exists"))
        elif isinstance(entry, dict) and entry.get("hash") != rule_hash(raws[rid]):
            out.append(Change(f"rule {rid}", "changed since the version-1 pin", weakened=True,
                              note="lock version 1 kept only a whole-rule hash, so the change cannot be shown; "
                                   "review the rule file history"))
    for rid in sorted(set(raws) - set(old.get("rules") or {})):
        out.append(Change(f"rule {rid}", "added"))
    return out


_V3_ONLY = ("types", "reply_schema", "schema", "operations")


def _comparable(lock: dict[str, Any], v2: bool) -> dict[str, Any]:
    """A lock reduced to what versions 2 and 3 both pin the same way: rule semantics, status, targets,
    effects, transaction, signature text, provider fields and contract references."""
    out: dict[str, Any] = {"lock_version": None, "spec": lock.get("spec"), "rules": {}}
    for rid, e in (lock.get("rules") or {}).items():
        e = dict(e)
        e.pop("applies_to", None)
        if v2:
            sem = dict(e.get("semantic") or {})
            e["semantic"] = normalize_semantic(sem, sem.get("compares_to"))
        out["rules"][rid] = e
    for sec in ("targets", "providers", "contracts"):
        out[sec] = {k: {f: v for f, v in e.items() if f not in _V3_ONLY and not (f == "transaction" and v is None)}
                    for k, e in (lock.get(sec) or {}).items()}
    return out


def migrate_v2(old: dict[str, Any], fresh: dict[str, Any],
               types: dict[str, Typer | None] | None = None) -> list[Change]:
    """What a version-2 lock tells about the fresh version-3 one. Rule semantics (so the weakening gate
    holds across the migration), effects, transactions, signature text, provider fields and contract
    references are compared. Version 3 pins resolved types and stores schemas once without prose, so
    those pins are re-taken rather than compared."""
    return changes(_comparable(old, True), _comparable(fresh, False), types)
