"""Contract pins (contract_hash): a Python object by its schema or signature, an OpenAPI document (a
file or a live app's app.openapi()) per operation, with shared component schemas sent to the
schema table once, and any other file by its digest. Imports the modules contracts name."""
from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import pathlib
from typing import Any

from ..errors import LoadError
from .contracts import PY_CONTRACTS, SCHEMA_CONTRACTS, _plain, _sha
from .pin_jsonschema import json_schema
from .pin_signatures import _signature_pin
from .pin_types import (_DATA_KEYS, SCHEMA_PROSE, Schemas, _class_ref, _is_schema_class, _is_typeddict,
                        _type_schema, resolved_schema, strip_prose)


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


def _openapi_file(name: str, path: pathlib.Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise LoadError(f"contract {name}: {path.name} cannot be read as UTF-8 text ({type(e).__name__})") from e
    except OSError as e:
        raise LoadError(f"contract {name}: {path.name} cannot be read ({type(e).__name__})") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
            return yaml.safe_load(text)
        except Exception as e:
            raise LoadError(f"contract {name}: {path.name} is neither JSON nor YAML ({e})") from e


def _openapi_app(name: str, ref: str) -> Any:
    """The OpenAPI document of a live app (`module:app`, for example a FastAPI app): app.openapi()."""
    app = _python_ref(name, ref)
    make = getattr(app, "openapi", None)
    if not callable(make):
        raise LoadError(f"contract {name}: {ref} has no openapi() method (a FastAPI app has one)")
    try:
        return make()
    except Exception as e:
        raise LoadError(f"contract {name}: {ref}.openapi() failed ({type(e).__name__}: {e})") from e


def _operation_ids(spec: Any) -> list[str]:
    return sorted({op["operationId"] for item in (spec.get("paths") or {}).values() if isinstance(item, dict)
                   for op in item.values() if isinstance(op, dict) and isinstance(op.get("operationId"), str)})


class _OpenAPIDoc:
    """One OpenAPI document being pinned: local $refs resolved, shared component schemas sent to the
    schema table (`pins`) once, labelled with `label`; errors name the contract."""

    def __init__(self, name: str, label: str, spec: dict[str, Any], pins: Schemas) -> None:
        self.name, self.label, self.spec, self.pins = name, label, spec, pins

    def lookup(self, ref: str) -> Any:
        target: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise LoadError(f"contract {self.name}: unresolvable $ref {ref}")
            target = target[part]
        return target

    def schema(self, node: Any, stack: tuple[str, ...]) -> Any:
        """A schema object with local $refs resolved; a shared component becomes a table entry."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                if ref in stack:
                    return {"$ref": ref, "recursive": True}
                body = self.schema(self.lookup(ref), (*stack, ref))
                got = (self.pins.add({"name": f"{self.label}{ref[1:]}", "schema": body})
                       if ref.startswith(_SHARED_SCHEMAS) else body)
                rest = {k: self.schema(v, stack) for k, v in node.items() if k != "$ref" and k not in SCHEMA_PROSE}
                return {"allOf": [got], **rest} if rest else got
            return {k: (v if k in _DATA_KEYS else self.schema(v, stack)) for k, v in node.items()}
        if isinstance(node, list):
            return [self.schema(v, stack) for v in node]
        return node

    def deref(self, node: Any) -> Any:
        """A parameter, request body or response object behind a $ref."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
            if node["$ref"] in seen:
                raise LoadError(f"contract {self.name}: $ref cycle at {node['$ref']}")
            seen.add(node["$ref"])
            node = self.lookup(node["$ref"])
        return node

    def content(self, c: Any) -> dict[str, Any]:
        return {mt: self.schema((m or {}).get("schema"), ()) for mt, m in sorted((c or {}).items())
                if isinstance(m, dict)}

    def parameters(self, item: dict[str, Any], op: dict[str, Any]) -> dict[tuple[str, str], Any]:
        """The path item's and the operation's parameters, keyed by (in, name): the operation's win."""
        params: dict[tuple[str, str], Any] = {}
        for p in [*(item.get("parameters") or []), *(op.get("parameters") or [])]:
            p = self.deref(p)
            if isinstance(p, dict):
                params[(str(p.get("in")), str(p.get("name")))] = {
                    k: (self.schema(v, ()) if k == "schema" else self.content(v) if k == "content" else v)
                    for k, v in p.items() if k in _PARAM_KEYS}
        return params

    def operation(self, route: str, method: str, item: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
        """One operation: method, path, and the schemas of its parameters, request body and responses."""
        params = self.parameters(item, op)
        entry: dict[str, Any] = {"method": method.upper(), "path": route}
        if params:
            entry["parameters"] = [params[k] for k in sorted(params)]
        if "requestBody" in op:
            rb = self.deref(op["requestBody"]) or {}
            entry["requestBody"] = {"required": bool(rb.get("required", False)),
                                    "content": self.content(rb.get("content"))}
        if op.get("responses"):
            entry["responses"] = {str(code): self.content((self.deref(r) or {}).get("content"))
                                  for code, r in sorted(op["responses"].items(), key=lambda kv: str(kv[0]))}
        return entry


def _openapi_operations(name: str, label: str, spec: Any, ops: list[str], pins: Schemas) -> dict[str, Any]:
    """The operations named by rules, keyed by operationId: method, path, and the schemas of their
    parameters, request body and responses. Shared component schemas go to the schema table once."""
    if not isinstance(spec, dict):
        raise LoadError(f"contract {name}: {label} is not an OpenAPI document")
    doc = _OpenAPIDoc(name, label, spec, pins)
    found: dict[str, Any] = {}
    for route, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if isinstance(op, dict) and op.get("operationId") in ops:
                found[op["operationId"]] = strip_prose(doc.operation(route, method, item, op))
    missing = sorted(set(ops) - set(found))
    if missing:
        raise LoadError(f"contract {name}: {label} has no operation {', '.join(missing)}")
    return _plain(found)


def contract_hash(name: str, c: dict[str, Any], base_dir: pathlib.Path,
                  operations: list[str] | None = None, pins: Schemas | None = None) -> dict[str, Any]:
    """Pin one contract at contract granularity (see the module docstring). Without `pins`, schemas are
    inlined in the entry instead of going to a schema table."""
    pins = Schemas(inline=True) if pins is None else pins
    ctype = c.get("type")
    if ctype == "openapi" and "app" in c:
        return _openapi_app_pin(name, c, operations, pins)
    ref = c.get("ref")
    if not isinstance(ref, str) or not ref:
        raise LoadError(f"contract {name}: needs a string ref")
    entry: dict[str, Any] = {"type": ctype, "ref": ref}
    if ctype in PY_CONTRACTS or (ctype in SCHEMA_CONTRACTS and ":" in ref):
        return _python_contract_pin(name, ref, entry, pins)
    f = base_dir / ref
    if not f.is_file():
        raise LoadError(f"contract {name}: {ref} not found next to the rule file")
    if ctype == "openapi" and operations:
        entry["operations"] = _openapi_operations(name, f.name, _openapi_file(name, f), sorted(set(operations)), pins)
        return entry
    try:
        entry["hash"] = _sha(f.read_bytes())
    except OSError as e:
        raise LoadError(f"contract {name}: {f.name} cannot be read ({type(e).__name__})") from e
    if ctype == "openapi":
        _pin_every_operation(name, f, entry, pins)
    return entry


def _python_contract_pin(name: str, ref: str, entry: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    """A contract that is a Python object (`module:attr`): a schema class, mapping or Enum as its
    schema, a callable as its signature, any other object by the digest of its source."""
    obj = _python_ref(name, ref)
    if inspect.ismodule(obj):
        return entry                                # its targets and providers carry the pins
    if (isinstance(obj, type) and dataclasses.is_dataclass(obj)) or _is_typeddict(obj):
        entry["schema"] = pins.add({"name": _class_ref(obj), "jsonschema": json_schema(obj)})
    elif _is_schema_class(obj) or isinstance(obj, dict) or getattr(obj, "__inv_pydantic__", None) is not None:
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


def _pin_every_operation(name: str, f: pathlib.Path, entry: dict[str, Any], pins: Schemas) -> None:
    """A whole-file OpenAPI contract (no rule names an operation of it): any edit re-pins the digest,
    and every operation is pinned too, so a looser schema in one of them is judged a weakening."""
    try:
        spec = _openapi_file(name, f)
        ids = _operation_ids(spec) if isinstance(spec, dict) else []
        if ids:
            trial = Schemas(inline=pins.inline)            # merged only when every operation reads
            entry["operations"] = _openapi_operations(name, f.name, spec, ids, trial)
            pins.table.update(trial.table)
    except LoadError:
        pass                                        # not a document it can read: the digest pins it


def _openapi_app_pin(name: str, c: dict[str, Any], operations: list[str] | None, pins: Schemas) -> dict[str, Any]:
    """An `openapi` contract read from a live app (`app: module:app`): the operations rules (or the
    contract's `operation`) name, or every operation when none does: the document is generated, so
    there is no file to digest."""
    app = c.get("app")
    if not isinstance(app, str) or ":" not in app:
        raise LoadError(f"contract {name}: app must be `module:attribute` (a FastAPI app, for example)")
    if "ref" in c:
        raise LoadError(f"contract {name}: give ref (an OpenAPI file) or app (a live app), not both")
    spec = _openapi_app(name, app)
    named = [*(operations or []), *([c["operation"]] if isinstance(c.get("operation"), str) else [])]
    ops = sorted(set(named)) if named else _operation_ids(spec if isinstance(spec, dict) else {})
    return {"type": "openapi", "app": app, "operations": _openapi_operations(name, app, spec, ops, pins)}
