"""load(): the whole document (contracts, targets, providers, rules) into a Registry."""
from __future__ import annotations

import contextlib
import copy
import inspect
import logging
import pathlib
from typing import Any, ContextManager

import invariants_cel as cel

from ..errors import LoadError
from ..model import FILE_FIELDS, PROVIDER_FIELDS, PROVIDER_OPTIONAL, SPECS, TARGET_FIELDS, Provider
from ..providers import provider_names
from ..registry import ABLATE_ENV, Registry, ablation_level, current, env_ablated
from ..resolve import resolve
from .reading import Where, read_located
from .rules import _load_rule
from .schemas import _parse_type, _type_set, type_hints
from .shapes import _check_fields
from .short_forms import expand_short_forms

log = logging.getLogger("invariants")

# contract types whose ref names a Python object as module:attribute
_OBJECT_CONTRACTS = frozenset({"pydantic", "dataclass"})


def load(source: str | pathlib.Path | dict[str, Any], install: bool = True,
         base_dir: str | pathlib.Path | None = None, backend: str | cel.Backend | None = None) -> Registry:
    """Load and validate a rule file. With install=True the current registry takes the rules.

    A dict source is deep-copied first: later edits to the caller's dict never change live guards.
    An error in one rule, provider or target of a file names the file and, for YAML, the line
    (`invariants.yaml:19: REFUND: ...`).
    """
    where: Where | None = None
    if isinstance(source, (str, pathlib.Path)):
        base_dir = pathlib.Path(source).resolve().parent
        doc, where = read_located(source)
    else:
        doc = _copied(source)
    base = pathlib.Path(base_dir or ".")
    eng = cel.get_backend(backend)
    _check_fields(doc, FILE_FIELDS, "file")
    _check_spec(doc)
    reg = Registry()
    reg.backend_name = eng.name
    _load_contracts(doc, reg)
    _load_targets(doc, reg, where)
    providers = doc.get("providers") or {}
    if not isinstance(providers, dict):
        raise LoadError("providers: expected a mapping")
    for name, p in providers.items():
        with _at(where, where.providers.get(str(name)) if where else None):
            reg.providers[name] = _load_provider(name, p)
    _load_rules(doc, reg, base, eng, where)
    off = sorted(env_ablated() & set(reg.rules))
    if off:
        log.log(ablation_level(), "invariants: rules %s are ablated by %s: their in-process guards are off",
                ", ".join(off), ABLATE_ENV)
    if install:
        return current().install(reg)
    return reg


def _at(where: Where | None, line: int | None) -> ContextManager[None]:
    """Prefix a LoadError raised in the block with the file and line, for a file source."""
    return where.located(line) if where is not None else contextlib.nullcontext()


def _check_spec(doc: dict[str, Any]) -> None:
    """spec, when given, names a version of the rule-file format this library reads."""
    if "spec" in doc and doc["spec"] not in SPECS:
        raise LoadError(f"spec: {doc['spec']!r} is not a rule-file version this library reads "
                        f"(it reads {', '.join(SPECS)})")


def _copied(source: Any) -> Any:
    """A deep copy of a dict source, its short forms expanded."""
    try:
        doc = copy.deepcopy(source)
    except Exception as e:
        raise LoadError(f"file: the rule document could not be copied ({type(e).__name__}); "
                        f"pass plain data (dicts, lists, strings, numbers)") from None
    if isinstance(doc, dict):
        expand_short_forms(doc)
    return doc


def _load_contracts(doc: dict[str, Any], reg: Registry) -> None:
    contracts = doc.get("contracts") or {}
    if not isinstance(contracts, dict):
        raise LoadError("contracts: expected a mapping")
    for name, c in contracts.items():
        ref = c.get("ref") if isinstance(c, dict) else None
        if isinstance(c, dict) and c.get("type") in _OBJECT_CONTRACTS and isinstance(ref, str) \
                and ":" not in ref and "." in ref and all(p.isidentifier() for p in ref.split(".")):
            mod, _, attr = ref.rpartition(".")
            raise LoadError(f"contract {name}: ref {ref!r} names a Python object: write it as module:attribute "
                            f"({mod}:{attr})")
    reg.contracts = dict(contracts)


def _load_targets(doc: dict[str, Any], reg: Registry, where: Where | None = None) -> None:
    targets = doc.get("targets") or {}
    if not isinstance(targets, dict):
        raise LoadError("targets: expected a mapping")
    for key, t in targets.items():
        with _at(where, where.targets.get(str(key)) if where else None):
            _check_fields(t, TARGET_FIELDS, f"target {key}")
            if "effects" not in t:
                raise LoadError(f"target {key}: missing effects")
            resolve(key)
            reg.targets[key] = dict(t)


def _load_rules(doc: dict[str, Any], reg: Registry, base: pathlib.Path, eng: cel.Backend,
                where: Where | None = None) -> None:
    rules = [] if doc.get("rules") is None else doc["rules"]
    if not isinstance(rules, list):
        raise LoadError("rules: expected a list")
    first: dict[str, int] = {}
    for i, raw in enumerate(rules):
        with _at(where, where.rules.get(i) if where else None):
            rule = _load_rule(raw, reg, base, eng)
            if rule.id in reg.rules:
                raise LoadError(f"{rule.id}: duplicate id: {_nth(first[rule.id], where)} and {_nth(i, where)} "
                                f"both use it (rule ids are unique)")
        first[rule.id] = i
        reg.rules[rule.id] = rule


def _nth(i: int, where: Where | None) -> str:
    """'rule 1 (line 12)': the i-th rule, counted from 1, and its line when known."""
    line = where.rules.get(i) if where else None
    return f"rule {i + 1}" + (f" (line {line})" if line else "")


def _load_provider(name: str, p: Any) -> Provider:
    _check_fields(p, PROVIDER_FIELDS | PROVIDER_OPTIONAL, f"provider {name}")
    if PROVIDER_FIELDS - set(p):
        raise LoadError(f"provider {name}: needs {sorted(PROVIDER_FIELDS - set(p))}")
    if "concurrent" in p and not isinstance(p["concurrent"], bool):
        raise LoadError(f"provider {name}: concurrent must be true or false, got {p['concurrent']!r}")
    if not isinstance(p["args"], dict):
        raise LoadError(f"provider {name}: args must be a mapping")
    for a, path in p["args"].items():
        if not isinstance(path, str) or not path:
            raise LoadError(f"provider {name}: argument {a!r} must be a path string (e.g. args.account), "
                            f"not {type(path).__name__}")
    fn = resolve(p["from"], f"provider {name}: from")
    got = provider_names(fn)
    if name not in got:
        raise LoadError(f"provider {name}: {p['from']} is not registered with @inv.provider({name!r})"
                        + (f"; it is registered as {', '.join(map(repr, got))}" if got else ""))
    params = list(inspect.signature(fn).parameters)
    if set(params) != set(p["args"]):
        raise LoadError(f"provider {name}: signature {params} does not match args {sorted(p['args'])}")
    typ, nullable = _parse_type(p["type"])
    ann = type_hints(fn, f"provider {name}: {p['from']}", LoadError).get("return")
    if _type_set(ann) != frozenset({typ} | ({type(None)} if nullable else set())):
        raise LoadError(f"provider {name}: {p['from']} returns {ann}, but the record declares {p['type']!r}")
    return Provider(name, p, fn, typ, nullable)
