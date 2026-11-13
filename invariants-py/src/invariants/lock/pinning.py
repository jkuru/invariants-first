"""Taking the pins: `build` turns a loaded rule document into a version-3 lock. Signatures are pinned
as text and as resolved types, a parameter's Annotated constraints as JSON Schema (param_schemas),
pydantic models, dataclasses, TypedDicts and {field: type} mappings as resolved schemas or JSON Schema
(stdlib_json_schema without pydantic), and OpenAPI documents (a file or a live app) per operation. Each
schema goes to the content-addressed table (Schemas) once, without its prose. Imports target modules.

The work is staged: pin_types (resolved types, the schema table), pin_jsonschema (JSON Schema of a
dataclass or TypedDict), pin_signatures (signature pins) and pin_contracts (contract pins); this
module builds the lock from them and re-exports their names, public and private."""
from __future__ import annotations

import fnmatch
import pathlib
from typing import Any

from ..errors import LoadError
from ..resolve import resolve
from .contracts import (DOC_FIELDS, ENFORCER_DEFAULTS, FEEDBACK_DEFAULT, LOCK_VERSION, MAX_DEFS, MAX_DEPTH,
                        MEDIATION_DOC, PY_CONTRACTS, SCHEMA_CONTRACTS, _plain, _sha, canonical)
from .pin_contracts import (_PARAM_KEYS, _SHARED_SCHEMAS, _OpenAPIDoc, _openapi_app, _openapi_app_pin,
                            _openapi_file, _openapi_operations, _operation_ids, _pin_every_operation,
                            _python_contract_pin, _python_ref, contract_hash)
from .pin_jsonschema import (_ARRAYS, _BOUND_ATTRS, _JSON_TYPES, _LEN_ATTRS, _MAPS, _Defs, _annotated_bounds,
                             _def_key, _json_object, _json_type, _key_text, _local_def, _refers_to, json_schema,
                             stdlib_json_schema)
from .pin_signatures import (_annotation_schema, _callable_hints, _constrains, _has_constraints, _param_schemas,
                             _signature_pin, _signature_types, param_schemas, signature_types)
from .pin_types import (_DATA_KEYS, _KEY_QUALIFIERS, _NAME_MAPS, SCHEMA_PROSE, Schemas, _class_fields, _class_ref,
                        _class_shape, _ClassShape, _generic_schema, _is_alias, _is_pydantic, _is_schema_class,
                        _is_typeddict, _key_qualifiers, _schema_class_ref, _substitute, _type_schema, _unalias,
                        resolved_schema, strip_prose)

# the private classes of the stages keep the module name they had before the split (the package
# resets the public ones it imports, Schemas among them, to invariants.lockfile)
for _cls in (_ClassShape, _Defs, _OpenAPIDoc):
    _cls.__module__ = __name__
del _cls


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
    entry = {k: p.get(k) for k in ("type", "from", "args")}
    if "concurrent" in p:                               # pinned only when set: older locks stay fresh
        entry["concurrent"] = p["concurrent"]
    src = p.get("from")
    if isinstance(src, str) and src.startswith("py:"):
        try:
            entry.update(_signature_pin(resolve(src), f"provider {name}", pins))
        except Exception as e:
            raise LoadError(f"provider {name}: cannot pin {src} ({e})") from e
    return _plain(entry)


def scan_pin(roots: list[str], exclude: list[str]) -> dict[str, list[str]]:
    """The scan configuration a lock pins: source roots relative to the rule file, and exclude globs."""
    return {"roots": sorted(set(roots)), "exclude": sorted(set(exclude))}


def mediated(doc: dict[str, Any]) -> bool:
    return any(isinstance(r, dict) and r.get("mediation") for r in doc.get("rules") or [])


def build(doc: dict[str, Any], base_dir: pathlib.Path, scan: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """The lock for a rule document. Imports target modules; call after the file loads cleanly.
    `scan` (scan_pin of the roots and excludes `inv check` scans) is pinned when a rule has a sink."""
    expanded = expand_applies_to(doc)
    declared = doc.get("targets") or {}
    pins = Schemas()
    rules: dict[str, Any] = {}
    keys: set[str] = set(declared)
    for raw in doc.get("rules") or []:
        rules[raw["id"]] = _rule_pin(raw, expanded)
        keys.update(rules[raw["id"]]["targets"])
    ops = _subject_operations(doc)
    targets = {key: _target_pin(key, declared, pins) for key in sorted(keys)}
    providers = {n: _provider_pin(n, p, pins) for n, p in sorted((doc.get("providers") or {}).items())
                 if isinstance(p, dict)}
    contracts = {n: contract_hash(n, c, base_dir, ops.get(n), pins)
                 for n, c in sorted((doc.get("contracts") or {}).items())}
    lock = {"lock_version": LOCK_VERSION, "spec": doc.get("spec"), "rules": rules, "targets": targets,
            "providers": providers, "contracts": contracts, "schemas": pins.table}
    if scan is not None and mediated(doc):
        lock["scan"] = scan_pin(scan["roots"], scan["exclude"])
    return _plain(lock)


def _rule_pin(raw: dict[str, Any], expanded: dict[str, list[str]]) -> dict[str, Any]:
    """A rule's entry: rev, status, the targets it covers (subject, edges, applies_to), its semantic
    content and the digests of its documentation."""
    subj = raw.get("subject") or {}
    tk = subj.get("target")
    edges = sorted(e["target"] for e in raw.get("enforcers") or [] if isinstance(e, dict) and "target" in e)
    rule_targets = sorted({*([tk] if tk else []), *edges, *expanded.get(raw["id"], [])})
    return {"rev": raw.get("rev"), "status": raw.get("status"), "targets": rule_targets,
            "semantic": rule_semantics(raw), "doc": _doc_digests(raw)}


def _subject_operations(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Contract name -> the operations rule subjects name on it (an OpenAPI file pins only those)."""
    ops: dict[str, list[str]] = {}
    for raw in doc.get("rules") or []:
        subj = raw.get("subject") or {}
        if isinstance(subj.get("contract"), str) and isinstance(subj.get("operation"), str):
            ops.setdefault(subj["contract"], []).append(subj["operation"])
    return ops


def _target_pin(key: str, declared: dict[str, Any], pins: Schemas) -> dict[str, Any]:
    """A target's entry: its declared effects (external when undeclared), its signature pin, and the
    reply schema of a model-call target."""
    t = declared.get(key, {"effects": "external"})
    pin: dict[str, Any] = {"effects": t.get("effects")}
    obj = resolve(key)
    pin.update(_signature_pin(obj, f"target {key}", pins))
    schema = getattr(obj, "__inv_schema__", None)
    if schema is not None:
        label = _class_ref(schema) if isinstance(schema, type) else key
        pin["reply_schema"] = pins.add({"name": label, **resolved_schema(schema)})
    return pin
