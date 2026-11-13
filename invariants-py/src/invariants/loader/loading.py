"""load(): the whole document (contracts, targets, providers, rules) into a Registry."""
from __future__ import annotations

import copy
import inspect
import logging
import pathlib
from typing import Any

from .. import cel
from ..errors import LoadError
from ..model import FILE_FIELDS, PROVIDER_FIELDS, PROVIDER_OPTIONAL, TARGET_FIELDS, Provider
from ..providers import provider_names
from ..registry import ABLATE_ENV, Registry, ablation_level, current, env_ablated
from ..resolve import resolve
from .reading import read_document
from .rules import _load_rule
from .schemas import _parse_type, _type_set, type_hints
from .shapes import _check_fields
from .short_forms import expand_short_forms

log = logging.getLogger("invariants")


def load(source: str | pathlib.Path | dict[str, Any], install: bool = True,
         base_dir: str | pathlib.Path | None = None, backend: str | cel.Backend | None = None) -> Registry:
    """Load and validate a rule file. With install=True the current registry takes the rules.

    A dict source is deep-copied first: later edits to the caller's dict never change live guards.
    """
    if isinstance(source, (str, pathlib.Path)):
        base_dir = pathlib.Path(source).resolve().parent
        doc = read_document(source)
    else:
        doc = _copied(source)
    base = pathlib.Path(base_dir or ".")
    eng = cel.get_backend(backend)
    _check_fields(doc, FILE_FIELDS, "file")
    reg = Registry()
    reg.backend_name = eng.name
    _load_contracts(doc, reg)
    _load_targets(doc, reg)
    providers = doc.get("providers") or {}
    if not isinstance(providers, dict):
        raise LoadError("providers: expected a mapping")
    for name, p in providers.items():
        reg.providers[name] = _load_provider(name, p)
    _load_rules(doc, reg, base, eng)
    off = sorted(env_ablated() & set(reg.rules))
    if off:
        log.log(ablation_level(), "invariants: rules %s are ablated by %s: their in-process guards are off",
                ", ".join(off), ABLATE_ENV)
    if install:
        return current().install(reg)
    return reg


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
        if isinstance(c, dict) and c.get("type") == "sql":
            raise LoadError(f"contract {name}: sql contracts were removed (the schema is below the API); pin "
                            f"the pydantic model or OpenAPI operation the API takes instead, and delete it")
    reg.contracts = dict(contracts)


def _load_targets(doc: dict[str, Any], reg: Registry) -> None:
    targets = doc.get("targets") or {}
    if not isinstance(targets, dict):
        raise LoadError("targets: expected a mapping")
    for key, t in targets.items():
        _check_fields(t, TARGET_FIELDS, f"target {key}")
        if "effects" not in t:
            raise LoadError(f"target {key}: missing effects")
        resolve(key)
        reg.targets[key] = dict(t)


def _load_rules(doc: dict[str, Any], reg: Registry, base: pathlib.Path, eng: cel.Backend) -> None:
    rules = [] if doc.get("rules") is None else doc["rules"]
    if not isinstance(rules, list):
        raise LoadError("rules: expected a list")
    for raw in rules:
        rule = _load_rule(raw, reg, base, eng)
        if rule.id in reg.rules:
            raise LoadError(f"{rule.id}: duplicate id")
        reg.rules[rule.id] = rule


def _load_provider(name: str, p: Any) -> Provider:
    _check_fields(p, PROVIDER_FIELDS | PROVIDER_OPTIONAL, f"provider {name}")
    if PROVIDER_FIELDS - set(p):
        raise LoadError(f"provider {name}: needs {sorted(PROVIDER_FIELDS - set(p))}")
    if "concurrent" in p and not isinstance(p["concurrent"], bool):
        raise LoadError(f"provider {name}: concurrent must be true or false, got {p['concurrent']!r}")
    if not isinstance(p["args"], dict):
        raise LoadError(f"provider {name}: args must be a mapping")
    fn = resolve(p["from"])
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
