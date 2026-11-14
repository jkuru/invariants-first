"""Taking the pins: `build` turns a loaded rule document into a version-3 lock. Signatures are pinned
as text and as resolved types, a parameter's Annotated constraints as JSON Schema (param_schemas),
pydantic models, dataclasses, TypedDicts and {field: type} mappings as resolved schemas or JSON Schema
(stdlib_json_schema without pydantic), and OpenAPI documents (a file or a live app) per operation. Each
schema goes to the content-addressed table (Schemas) once, without its prose. Imports target modules.

The work is staged: pin_types (resolved types, the schema table), pin_jsonschema (JSON Schema of a
dataclass or TypedDict), pin_signatures (signature pins) and pin_contracts (contract pins); this
module builds the lock from them."""
from __future__ import annotations

import fnmatch
import pathlib
from typing import Any

from ..errors import LoadError
from ..resolve import resolve
from .contracts import (DOC_FIELDS, ENFORCER_DEFAULTS, FEEDBACK_DEFAULT, LOCK_VERSION, MEDIATION_DOC, PY_CONTRACTS,
                        SCHEMA_CONTRACTS, _is_ref, _plain, _sha, canonical)
from .pin_contracts import contract_hash
from .pin_signatures import _signature_pin
from .pin_types import Schemas, _class_ref, resolved_schema


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
def normalize_semantic(sem: dict[str, Any]) -> dict[str, Any]:
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
        held = sem.get("compares_to") == "held_data"
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
    if "concurrent" in p:                               # pinned only when set
        entry["concurrent"] = p["concurrent"]
    src = p.get("from")
    if isinstance(src, str) and src.startswith("py:"):
        try:
            entry.update(_signature_pin(resolve(src), f"provider {name}", pins))
        except LoadError as e:
            raise LoadError(f"provider {name}: cannot pin {src} ({e})") from e
        except Exception as e:                       # its text may carry an object's repr: the type only
            raise LoadError(f"provider {name}: cannot pin {src} ({type(e).__name__})") from e
    return _plain(entry)


def scan_skip() -> list[str]:
    """The directories the scan always skips, as the lock pins them: each name in SKIP_DIRS, and `.*`
    for every dot-directory. The one place the pin reads the scan's built-in skip list from, so a
    library upgrade or change that skips more shows up in the lock (an added entry is WEAKENED)."""
    from ..scan.sources import SKIPPED          # the scan's own value: one source of truth
    return sorted(SKIPPED)


def scan_pin(roots: list[str], exclude: list[str], skip: list[str] | None = None) -> dict[str, list[str]]:
    """The scan configuration a lock pins: source roots relative to the rule file, exclude globs, and
    the directories the scan always skips (scan_skip() unless given). Roots are pinned as a set: the
    scan names a file from the deepest root that holds it, whatever the order the roots come in."""
    return {"roots": sorted(set(roots)), "exclude": sorted(set(exclude)),
            "skip": sorted(set(scan_skip() if skip is None else skip))}


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
        lock["scan"] = scan_pin(scan["roots"], scan["exclude"], scan.get("skip"))
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


# --- the pins that need no import -----------------------------------------------------------------
# `inv lock` and `inv check` take the pins that import the app (signatures, model schemas, a live
# app's OpenAPI document) in a child process (cli/worker.py) and the rest here, in the process that
# compares them, from the rule document it read before the child ran: the child's word is taken for
# what only the imported code can say, never for the rules, the scan, a target's declared effects, a
# provider's declared fields or a contract read from a file.
def imports_code(c: Any) -> bool:
    """The contract is pinned by importing a module (a python, pydantic, dataclass or schema class
    `module:attr`, or an openapi `app:`); a file contract is read, never imported."""
    if not isinstance(c, dict):
        return True
    ref = c.get("ref")
    return ((c.get("type") == "openapi" and "app" in c) or c.get("type") in PY_CONTRACTS
            or (c.get("type") in SCHEMA_CONTRACTS and isinstance(ref, str) and ":" in ref))


def static_pins(doc: dict[str, Any], base_dir: pathlib.Path,
                scan: dict[str, list[str]] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """The parts of build() that need no import (a contract that does is None), and the message of
    each file contract that cannot be pinned."""
    expanded = expand_applies_to(doc)
    declared = doc.get("targets") or {}
    rules: dict[str, Any] = {}
    keys: set[str] = set(declared)
    for raw in doc.get("rules") or []:
        rules[raw["id"]] = _rule_pin(raw, expanded)
        keys.update(rules[raw["id"]]["targets"])
    targets = {k: {"effects": declared.get(k, {"effects": "external"}).get("effects")} for k in sorted(keys)}
    providers = {n: _provider_fields(p) for n, p in sorted((doc.get("providers") or {}).items())
                 if isinstance(p, dict)}
    ops = _subject_operations(doc)
    pins = Schemas()
    contracts: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for n, c in sorted((doc.get("contracts") or {}).items()):
        if imports_code(c):
            contracts[n] = None
            continue
        try:
            contracts[n] = contract_hash(n, c, base_dir, ops.get(n), pins)
        except LoadError as e:
            contracts[n] = None
            errors[n] = str(e)
    lock = {"lock_version": LOCK_VERSION, "spec": doc.get("spec"), "rules": rules, "targets": targets,
            "providers": providers, "contracts": contracts, "schemas": pins.table}
    if scan is not None and mediated(doc):
        lock["scan"] = scan_pin(scan["roots"], scan["exclude"], scan.get("skip"))
    return _plain(lock), errors


def _provider_fields(p: dict[str, Any]) -> dict[str, Any]:
    """A provider's declared fields, as _provider_pin pins them (concurrent only when set)."""
    entry = {k: p.get(k) for k in ("type", "from", "args")}
    if "concurrent" in p:
        entry["concurrent"] = p["concurrent"]
    return _plain(entry)


def with_static(imported: dict[str, Any], static: dict[str, Any]) -> dict[str, Any]:
    """The fresh lock: the imported pins (a lock build() made in the child) with every part that
    needs no import taken from static_pins. LoadError when the child pinned other entries than the
    rule file names."""
    for sec in ("targets", "providers", "contracts"):
        got = imported.get(sec)
        if not isinstance(got, dict) or sorted(got) != sorted(static[sec]):
            raise LoadError(f"the process that imports the app pinned other {sec} than the rule file names")
    out = {k: static[k] for k in ("lock_version", "spec", "rules")}
    out["targets"] = {k: {**_obj(imported["targets"][k]), **v} for k, v in static["targets"].items()}
    out["providers"] = {n: {**_obj(imported["providers"][n]), **v} for n, v in static["providers"].items()}
    out["contracts"] = {n: (imported["contracts"][n] if v is None else v) for n, v in static["contracts"].items()}
    if "scan" in static:
        out["scan"] = static["scan"]
    table = imported.get("schemas")
    out["schemas"] = _reachable(out, {**(table if isinstance(table, dict) else {}), **static["schemas"]})
    return _plain(out)


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _reachable(lock: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    """The schema entries the lock's entries reach: one the child pinned for a file contract that
    this process pinned itself is dropped, so the table holds no orphan."""
    def refs(node: Any, out: list[str]) -> None:
        if isinstance(node, dict):
            if _is_ref(node):
                out.append(node["$ref"])
            for v in node.values():
                refs(v, out)
        elif isinstance(node, list):
            for v in node:
                refs(v, out)
    work: list[str] = []
    for sec in ("rules", "targets", "providers", "contracts"):
        refs(lock.get(sec) or {}, work)
    kept: dict[str, Any] = {}
    while work:
        r = work.pop()
        if r in kept or r not in table:
            continue
        kept[r] = table[r]
        refs(table[r], work)
    return kept
