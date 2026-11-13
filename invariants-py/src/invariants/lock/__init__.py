"""`inv lock`: invariants.lock pins the contract a rule file relies on, at contract granularity.

Lock version 3 pins, per entry, only what a rule's meaning depends on:

- rules: the semantic content of each rule (bad, enforcers with their modes, on_violation, on_error and
  views, bind, scope, subject, mediation sink/door/allow, model_policy with its defaults filled in,
  ...), its status, the expanded targets, and a digest per documentation field (statement, why, owner,
  class: prose, never stale) and of mediation.chokepoint (judged: it decides `held`);
- targets: each target's signature (unwrapped through its decorators) as text and as resolved types,
  the JSON Schema of each parameter with an Annotated constraint, its declared effects, and the reply
  schema of a model-call target;
- providers: each provider's declared type, args, and its function's signature;
- contracts: a pydantic model's JSON Schema, a dataclass or TypedDict as a JSON Schema too, a {field:
  type} schema, an openapi file (or a live app's `app.openapi()`, `app: module:app`) as the
  parameters, request body and response schemas of its operations, a python module as its reference
  only (its targets are pinned above), any other file by its digest;
- schemas: every resolved schema the entries above use, keyed by the sha256 of its canonical JSON, so
  a model used by a target and a contract is stored once. Prose (title, description, summary, tags,
  examples) is stripped from pinned schemas, so a docstring edit does not re-pin;
- scan: when a rule has a sink, the source roots and exclude globs `inv check` scans with, so an
  exclude added or a root dropped is a weakening like any other.

Types are resolved, not read as annotation text: an alias, and the fields of a dataclass, TypedDict or
pydantic model a signature names (recursively, bounded), are part of the pin. A body-only refactor of a
target, or an edit elsewhere in the same module, changes none of these.

`changes(old, new)` explains every difference as old -> new and marks the ones that may relax a rule as
WEAKENED; `inv lock` refuses to re-pin those without --accept-weakening. FIELD_POLICY says how each
rule-file field is judged. A field it does not classify, and any edit to a field that changes what
`bad` reads (bind, a view, provider args, the subject, model_policy, an artifact), is a possible
weakening. Lock versions 1 and 2 are migrated by `inv lock` and refused by `inv lock --check`.

The package is staged: contracts (the shared tables, canonical JSON, Change), pinning (taking the
pins: build), schema_diff (is a schema change a weakening?), cel_mono (is a `bad` edit stronger or
weaker?), rule_diff (one entry's changes) and this module, which composes them (changes, diff), reads,
validates and writes the lock file, and migrates versions 1 and 2. `invariants.lockfile` re-exports
all of it under its old name.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from ..errors import LoadError
# the public names of the stages, re-exported (with the private ones this module composes)
from .cel_mono import Typer, canon_tree, clause_types, compare_bad, make_typer, new_error_sites
from .contracts import (ANALYSED, COMPENSABLE_RANK, COMPUTED, DEFAULT_SCAN_EXCLUDE, DOC_FIELDS, EFFECTS_RANK,
                        ENFORCER_DEFAULTS, ERROR_RANK, FEEDBACK_DEFAULT, FIELD_POLICY, LOCK_NAME, LOCK_VERSION,
                        MAX_DEFS, MAX_DEPTH, MEDIATION_DOC, MODE_RANK, MORE_IS_WEAKER, NEUTRAL, NOTES, PIN, PRESENCE,
                        PROSE, PY_CONTRACTS, RANKED, RANKS, REMOVED_NOTE, SCHEMA_CONTRACTS, STATUS_RANK, TIMING_RANK,
                        VIOLATION_RANK, WEAKENING, Change, _is_ref, _sha, canonical, policy, same)
from .pinning import (SCHEMA_PROSE, Schemas, build, contract_hash, expand_applies_to, json_schema, mediated,
                      normalize_semantic, param_schemas, resolved_schema, rule_hash, rule_semantics, scan_pin,
                      signature_types, stdlib_json_schema, strip_prose)
from .rule_diff import _entry_changes, _rule_changes, _scan_changes
from .schema_diff import (EXACT_CONSTRAINTS, JUDGED_SCHEMAS, LOWER_BOUNDS, STRUCTURAL, UPPER_BOUNDS, _schema_changes,
                          constraint_weakening, pin_json_schema)

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
    for sec in _ENTRY_SECTIONS:
        if sec in lock and lock[sec] is not None:
            _validate_section(path, sec, lock[sec])
    if "spec" in lock and lock["spec"] is not None and not isinstance(lock["spec"], str):
        raise _malformed(path, "spec", "a string", lock["spec"])
    scan = lock.get("scan")
    if scan is None:
        return
    if not isinstance(scan, dict):
        raise _malformed(path, "scan", "an object", scan)
    for k in ("roots", "exclude"):
        if not (isinstance(scan.get(k), list) and all(isinstance(x, str) for x in scan[k])):
            raise _malformed(path, f"scan.{k}", "a list of strings", scan.get(k))


def _validate_section(path: pathlib.Path | str, sec: str, val: Any) -> None:
    """One entry section (rules, targets, ...): an object of objects, with the fields _ENTRY_SHAPES lists."""
    if not isinstance(val, dict):
        raise _malformed(path, sec, "an object", val)
    for k, entry in val.items():
        if not isinstance(entry, dict):
            raise _malformed(path, f"{sec}.{k}", "an object", entry)
        for field, kind in _ENTRY_SHAPES.get(sec, {}).items():
            if field in entry and not isinstance(entry[field], kind):
                raise _malformed(path, f"{sec}.{k}.{field}", "a list" if kind is list else "an object", entry[field])


def _malformed(path: pathlib.Path | str, where: str, want: str, got: Any) -> LockError:
    return LockError(f"{path}: malformed lock: {where} should be {want}, not {type(got).__name__} "
                     f"{json.dumps(got, default=str)[:60]}; re-create it with `inv lock` "
                     f"(delete it first if it cannot be repaired)")


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


def changes(old: dict[str, Any], new: dict[str, Any],
            types: dict[str, Typer | None] | None = None,
            scan_default: dict[str, Any] | None = None) -> list[Change]:
    """Every semantic difference between a stored version-3 lock and a fresh one; empty when fresh.
    types (clause_types of the loaded rule file) lets the `bad` comparison use declared types.
    scan_default (a scan_pin) is what the old lock scanned when it pinned no scan: the roots and
    excludes the base's configuration gave by default."""
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
    for section, kind in (("targets", "target"), ("providers", "provider"), ("contracts", "contract")):
        sa, sb = old.get(section) or {}, new.get(section) or {}
        for name in sorted(set(sa) | set(sb)):
            where = f"{kind} {name}"
            if name not in sb and kind == "contract":
                out.append(Change(where, "removed", weakened=True, note=_contract_users(name, ra)))
            elif name not in sb:
                out.append(Change(where, "removed"))
            elif name not in sa:
                out.append(Change(where, "added"))
            else:
                out += _entry_changes(where, kind, sa[name], sb[name], refs, ra)
    out += _schema_changes(refs, old.get("schemas") or {}, new.get("schemas") or {})
    out += _scan_changes(old.get("scan"), new.get("scan"), scan_default)
    return out


def _contract_users(name: str, rules: dict[str, Any]) -> str:
    """Why removing a contract is a weakening. Always one: a rule may name it, and a contract no rule
    names (the pydantic models `inv init` pins) is itself the pin of the constraints it holds."""
    users = sorted(rid for rid, e in rules.items() if isinstance(e, dict)
                   and ((e.get("semantic") or {}).get("subject") or {}).get("contract") == name)
    if users:
        return f"rule {', '.join(users)} named it in the stored lock"
    return "its pinned schema or constraints are no longer checked"


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


_V3_ONLY = ("types", "reply_schema", "schema", "operations", "param_schemas")


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
        out[sec] = {k: {f: v for f, v in e.items() if f not in _V3_ONLY}
                    for k, e in (lock.get(sec) or {}).items()}
    return out


def migrate_v2(old: dict[str, Any], fresh: dict[str, Any],
               types: dict[str, Typer | None] | None = None) -> list[Change]:
    """What a version-2 lock tells about the fresh version-3 one. Rule semantics (so the weakening gate
    holds across the migration), effects, transactions, signature text, provider fields and contract
    references are compared. Version 3 pins resolved types and stores schemas once without prose, so
    those pins are re-taken rather than compared."""
    return changes(_comparable(old, True), _comparable(fresh, False), types)

# Classes keep the module name they had before the split: a pinned signature (`invariants.lock`) names
# an annotation's class by its module, and pickles name it too, so a moved __module__ would make an
# upgrade alone re-pin the lock and make new pickles unreadable by older versions.
for _obj in list(globals().values()):
    if isinstance(_obj, type) and (_obj.__module__ == __name__ or _obj.__module__.startswith(__name__ + ".")):
        _obj.__module__ = 'invariants.lockfile'
del _obj
