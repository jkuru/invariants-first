"""Load and validate a rule file (JSON or YAML, a path or a dict). Every defect is a LoadError."""
from __future__ import annotations

import copy
import dataclasses
import enum
import functools
import hashlib
import inspect
import json
import logging
import pathlib
import sys
import types
import typing
from typing import Any, Iterator

from . import cel
from .errors import LoadError
from .model import (DEFAULT_NAMES, ENFORCER_FIELDS, ENUMS, EVALUATED, VIEW_POINTS, FILE_FIELDS, MEDIATION_FIELDS, PATH_ROOTS,
                    POLICY_FIELDS,
                    PROVIDER_FIELDS, PROVIDER_OPTIONAL, RULE_FIELDS, STATUS_RANK, TARGET_FIELDS, Provider, Rule)
from .providers import provider_names
from .registry import ABLATE_ENV, Registry, ablation_level, current, env_ablated
from .resolve import resolve
from . import typecheck as tc

log = logging.getLogger("invariants")

YAML_SUFFIXES = (".yaml", ".yml")
_BUILTIN_TYPES: dict[str, type] = {"int": int, "str": str, "bool": bool, "float": float, "list": list, "dict": dict}


# Below-the-API fields that 0.x accepted and this version removed, with what to do instead.
REMOVED = {
    "consistency": "provider consistency levels were removed: a provider is a plain read at the enforcer's "
                   "timing; delete the field",
    "transaction": "transactional targets were removed: rules live on API boundaries; check the service "
                   "method's arguments (pre_effect) and delete the field",
    "tables": "tables (raw SQL write checks) were removed: a door rule checks which API calls the "
              "repository; delete the field",
}
REMOVED_VALUES = {
    ("effects", "transactional"): "effects: transactional was removed (rules stay on the API boundary); "
                                  "use effects: external, or none for a pure function",
    ("point", "storage"): "storage enforcers were removed: the database's own constraints are the "
                          "implementation's business; delete this enforcer or make it a pre_effect "
                          "decorator check",
}


def _check_fields(obj: Any, allowed: frozenset[str] | set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise LoadError(f"{where}: expected a mapping")
    unknown = set(obj) - allowed
    for k in sorted(unknown & set(REMOVED), key=str):
        raise LoadError(f"{where}: {k}: {REMOVED[k]}")
    for k, v in obj.items():
        if isinstance(v, str) and (k, v) in REMOVED_VALUES:
            raise LoadError(f"{where}: {REMOVED_VALUES[(k, v)]}")
    if unknown:
        raise LoadError(f"{where}: unknown field(s) {sorted(unknown, key=str)}")
    for k, v in obj.items():
        if k in ENUMS and (not isinstance(v, str) or v not in ENUMS[k]):
            hint = " (YAML reads a bare off/on as a boolean; quote it)" if isinstance(v, bool) else ""
            raise LoadError(f"{where}: {k}={v!r} not in {sorted(ENUMS[k])}{hint}")


def read_document(source: str | pathlib.Path) -> dict[str, Any]:
    """Read a rule file from disk: YAML for .yaml/.yml, JSON otherwise."""
    path = pathlib.Path(source)
    try:
        text = path.read_text()
    except OSError as e:
        raise LoadError(f"{source}: {e}") from e
    if path.suffix.lower() in YAML_SUFFIXES:
        import yaml
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise LoadError(f"{source}: {e}") from e
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise LoadError(f"{source}: {e}") from e
    if not isinstance(doc, dict):
        raise LoadError(f"{source}: the rule file must be a mapping")
    return expand_short_forms(doc)


SHORT_FIELDS = frozenset({"guard", "tests", "door", "sink", "allow"})
SHORT_ENFORCER = {"point": "decorator", "timing": "pre_effect", "mode": "enforce"}
DOOR_FIELDS = ("door", "sink", "allow")
STRUCTURE_ENFORCER = {"point": "structure", "timing": "pre_effect", "mode": "enforce"}


def _py_list(rid: str, key: str, v: Any) -> list[str]:
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.startswith("py:") for x in items):
        raise LoadError(f"{rid}: {key} must be a 'py:module.function' path or a non-empty list of them")
    return list(items)


def _expand_door(raw: dict[str, Any], rid: str) -> None:
    """door/sink/allow -> mediation; without guard or bad, a structure rule checked by the scan."""
    if "sink" not in raw:
        raise LoadError(f"{rid}: a door rule names the sink it protects: add sink: py:module.function "
                        f"(the function that writes)")
    med = raw.get("mediation")
    if med is not None and not isinstance(med, dict):
        raise LoadError(f"{rid}: mediation must be a mapping")
    clash = sorted({"sink", "door", "allow"} & set(med or {}))
    if clash:
        raise LoadError(f"{rid}: give {', '.join(clash)} at the top of the rule or under mediation, not both")
    sinks = _py_list(rid, "sink", raw.pop("sink"))
    doors = _py_list(rid, "door", raw.pop("door")) if "door" in raw else []
    allow_raw = raw.pop("allow", None)
    allow = _py_list(rid, "allow", allow_raw) if allow_raw not in (None, []) else []   # emptied: none left
    new = {**(med or {}), "sink": sinks if len(sinks) > 1 else sinks[0]}
    if doors:
        new["door"] = doors if len(doors) > 1 else doors[0]
    if allow:
        new["allow"] = allow
    if not doors and "guard" not in raw:
        raise LoadError(f"{rid}: a door rule names its door: add door: py:module.function (the function every "
                        f"write must go through), or guard: to make the guarded function the door")
    raw["mediation"] = new
    if "guard" in raw or "bad" in raw or "subject" in raw:
        if "guard" not in raw and "bad" in raw:
            raise LoadError(f"{rid}: a door rule is checked by the scan and has no bad; to also check values at "
                            f"run time, add guard: (the function whose arguments bad reads)")
        return
    if "enforcers" in raw:
        raise LoadError(f"{rid}: a door rule adds its structure enforcer; drop enforcers or use the long form")
    raw["bad"] = "false"                          # nothing to evaluate at run time: the scan is the check
    raw["enforcers"] = [dict(STRUCTURE_ENFORCER)]
    raw.setdefault("status", "enforce")


def expand_short_forms(doc: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the short rule form into the full one, in place, before anything else reads the file.

    A short rule names the function it guards and, optionally, the tests that prove the guard works:

        - id: PAY-1
          guard: py:payments.refund
          bad: 'args.amount > args.charged - args.refunded'
          tests: [tests/test_payments.py::test_over_refund_is_rejected]

    becomes subject.target, one decorator enforcer (pre_effect, mode enforce) and falsified_by. With
    no `status`, it is `enforce` when the rule names tests and `detect` (log, do not block) when it does
    not, so the loader's evidence check never refuses a short rule. The lock pins the full form, so
    switching between the two forms is not a change, and deleting `tests:` lowers the status
    (a weakening).

    A door rule names the function every write must go through and the function that writes:

        - id: LEDGER-DOOR
          door: py:billing.service.apply_transfer
          sink: py:billing.store.write_balance
          allow: [py:billing.batch_refund.refund_all]     # known back doors, frozen

    becomes mediation {sink, door, allow}, `bad: 'false'` and one structure enforcer: nothing runs in
    production, and `inv scan` (inside `inv check`) fails any call path to the sink that skips the
    door. `allow` lists known back doors (mediation.allow): the scan
    lets them through, so today's code passes and a new back door fails; the lock reports an added
    entry as a weakening. With guard:
    as well, the guarded function is the door and the rule also checks its arguments."""
    rules = doc.get("rules")
    for raw in rules if isinstance(rules, list) else []:
        if not isinstance(raw, dict) or not SHORT_FIELDS & set(raw):
            continue
        rid = raw.get("id", "?")
        if any(k in raw for k in DOOR_FIELDS):
            _expand_door(raw, rid)
        if "tests" in raw:
            if "falsified_by" in raw:
                raise LoadError(f"{rid}: give tests or falsified_by, not both (tests is the short name)")
            tests = raw.pop("tests")
            if isinstance(tests, str):
                tests = [tests]
            if not isinstance(tests, list) or not all(isinstance(t, str) and t for t in tests):
                raise LoadError(f"{rid}: tests must be a pytest node id or a list of them")
            raw["falsified_by"] = tests
        if "guard" in raw:
            target = raw.pop("guard")
            if not isinstance(target, str) or not target.startswith("py:"):
                raise LoadError(f"{rid}: guard must be the 'py:module.function' the rule protects, got {target!r}")
            subject = raw.get("subject")
            if subject is not None and not isinstance(subject, dict):
                raise LoadError(f"{rid}: subject must be a mapping")
            if subject and "target" in subject:
                raise LoadError(f"{rid}: give guard or subject.target, not both (guard is the short form)")
            if "enforcers" in raw:
                raise LoadError(f"{rid}: guard adds the decorator enforcer; drop enforcers or use subject.target "
                                f"with your own enforcers")
            raw["subject"] = {**(subject or {}), "target": target}
            raw["enforcers"] = [dict(SHORT_ENFORCER)]
            raw.setdefault("status", "enforce" if raw.get("falsified_by") else "detect")
    return doc


def _parse_type(s: str) -> tuple[type, bool]:
    """Provider type: 'int' | 'py:mod.Class', optionally suffixed ' | null'."""
    if not isinstance(s, str):
        raise LoadError(f"provider type {s!r} must be a string")
    if s.replace(" ", "").endswith("|null"):
        base, nullable = s.strip()[: -len("null")].rstrip().rstrip("|").strip(), True
    else:
        base, nullable = s.strip(), False
    t = _BUILTIN_TYPES.get(base) or (resolve(base) if base.startswith("py:") else None)
    if not isinstance(t, type):
        raise LoadError(f"provider type {s!r} does not name a type")
    return t, nullable


def _type_set(ann: Any) -> frozenset[Any]:
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        return frozenset(typing.get_args(ann))
    return frozenset({ann})


def _params(obj: Any) -> set[str]:
    fn = inspect.unwrap(obj)
    return set(inspect.signature(fn).parameters) if callable(fn) else set()


def _check_path(rid: str, what: str, path: Any, reg: Registry, roots: Any = PATH_ROOTS) -> None:
    if not isinstance(path, str):
        raise LoadError(f"{rid}: {what}: path must be a string, got {path!r}")
    head, *rest = path.split(".")
    if head not in roots:
        raise LoadError(f"{rid}: {what}: bad root in {path!r}")
    if head == "provider" and (len(rest) != 1 or rest[0] not in reg.providers):
        raise LoadError(f"{rid}: {what}: {path!r} names no declared provider")


def closed_schema(schema: Any) -> Any:
    """A pydantic model becomes its closed dataclass (integrations.pydantic.closed); others are unchanged."""
    mod = sys.modules.get("pydantic")
    if mod is not None and isinstance(schema, type) and issubclass(schema, mod.BaseModel):
        from .integrations.pydantic import closed
        return closed(schema)
    return schema


def type_hints(obj: Any, who: str, error: type[Exception] = TypeError,
               localns: dict[str, Any] | None = None) -> dict[str, Any]:
    """typing.get_type_hints(obj), or `error` naming the annotation that does not resolve (never a raw
    NameError). localns: names to try when the module's globals do not hold one (a class defined in
    the function that applies the decorator, under `from __future__ import annotations`)."""
    try:
        return typing.get_type_hints(obj)
    except NameError as e:
        first = e
    except Exception as e:
        raise error(f"{who}: its annotations do not resolve ({type(e).__name__}: {e})") from None
    if localns:
        try:
            return typing.get_type_hints(obj, localns=localns)
        except Exception:
            pass
    name = getattr(first, "name", None) or str(first)
    raise error(f"{who}: the annotation name {name!r} does not resolve (with `from __future__ import "
                f"annotations`, annotations are read by name: define {name} at module level or import it "
                f"there)") from None


@functools.lru_cache(maxsize=512)
def _dataclass_fields(schema: type) -> dict[str, Any]:
    return type_hints(schema, f"schema {getattr(schema, '__qualname__', schema)!r}")


def schema_fields(schema: Any) -> dict[str, Any]:
    """Field name -> type of a closed schema (dataclass, {field: type} or pydantic model). Cached per class."""
    schema = closed_schema(schema)
    if isinstance(schema, type) and dataclasses.is_dataclass(schema):
        return dict(_dataclass_fields(schema))
    return (type_hints(schema, f"schema {getattr(schema, '__qualname__', schema)!r}")
            if dataclasses.is_dataclass(schema) else dict(schema))


def literal_eq(v: Any, a: Any) -> bool:
    """v equals the Literal member a (an Enum member stands for its value), with the same plain type:
    True is not 1, and no user-defined __eq__ ever runs."""
    if isinstance(a, enum.Enum):
        a = a.value
    return type(v) is type(a) and type(a) in (str, int, float, bool, bytes, type(None)) and v == a


def schema_name(schema: Any) -> str:
    """A closed schema's name for messages: the class name, or 'the {field: type} schema'."""
    n = getattr(schema, "__name__", None)
    return n if isinstance(n, str) else "the {field: type} schema"


def _restates_enum(prog: Any, fields: dict[str, Any], abstain_value: Any) -> str | None:
    """A clause `!(reply.F in [...])` whose list covers F's closed enum (minus the abstain value)."""
    probe = getattr(prog, "negated_membership", None)
    got = probe() if probe else None
    if not got:
        return None
    path, values = got
    if len(path) != 2 or path[0] != "reply":
        return None
    t = fields.get(path[1])
    if typing.get_origin(t) is not typing.Literal:
        return None
    return path[1] if set(typing.get_args(t)) - {abstain_value} <= set(values) else None


def load(source: str | pathlib.Path | dict[str, Any], install: bool = True,
         base_dir: str | pathlib.Path | None = None, backend: str | cel.Backend | None = None) -> Registry:
    """Load and validate a rule file. With install=True the current registry takes the rules.

    A dict source is deep-copied first: later edits to the caller's dict never change live guards.
    """
    if isinstance(source, (str, pathlib.Path)):
        base_dir = pathlib.Path(source).resolve().parent
        doc = read_document(source)
    else:
        try:
            doc = copy.deepcopy(source)
        except Exception as e:
            raise LoadError(f"file: the rule document could not be copied ({type(e).__name__}); "
                            f"pass plain data (dicts, lists, strings, numbers)") from None
        if isinstance(doc, dict):
            expand_short_forms(doc)
    base = pathlib.Path(base_dir or ".")
    eng = cel.get_backend(backend)
    _check_fields(doc, FILE_FIELDS, "file")
    reg = Registry()
    reg.backend_name = eng.name
    contracts = doc.get("contracts") or {}
    if not isinstance(contracts, dict):
        raise LoadError("contracts: expected a mapping")
    for name, c in contracts.items():
        if isinstance(c, dict) and c.get("type") == "sql":
            raise LoadError(f"contract {name}: sql contracts were removed (the schema is below the API); pin "
                            f"the pydantic model or OpenAPI operation the API takes instead, and delete it")
    reg.contracts = dict(contracts)
    targets = doc.get("targets") or {}
    if not isinstance(targets, dict):
        raise LoadError("targets: expected a mapping")
    for key, t in targets.items():
        _check_fields(t, TARGET_FIELDS, f"target {key}")
        if "effects" not in t:
            raise LoadError(f"target {key}: missing effects")
        resolve(key)
        reg.targets[key] = dict(t)
    providers = doc.get("providers") or {}
    if not isinstance(providers, dict):
        raise LoadError("providers: expected a mapping")
    for name, p in providers.items():
        reg.providers[name] = _load_provider(name, p)
    rules = [] if doc.get("rules") is None else doc["rules"]
    if not isinstance(rules, list):
        raise LoadError("rules: expected a list")
    for raw in rules:
        rule = _load_rule(raw, reg, base, eng)
        if rule.id in reg.rules:
            raise LoadError(f"{rule.id}: duplicate id")
        reg.rules[rule.id] = rule
    off = sorted(env_ablated() & set(reg.rules))
    if off:
        log.log(ablation_level(), "invariants: rules %s are ablated by %s: their in-process guards are off",
                ", ".join(off), ABLATE_ENV)
    if install:
        return current().install(reg)
    return reg


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


def _strings(v: Any, prefix: str = "") -> bool:
    """A string, or a non-empty list of strings, each starting with prefix."""
    items = [v] if isinstance(v, str) else v if isinstance(v, list) and v else None
    return items is not None and all(isinstance(x, str) and x.startswith(prefix) for x in items)


def _check_mediation(rid: str, raw: dict[str, Any]) -> None:
    """Shapes of mediation (chokepoint, residual_paths, and the scan's sink, door, allow) and applies_to."""
    if "applies_to" in raw and not _strings(raw["applies_to"]):
        raise LoadError(f"{rid}: applies_to must be a pattern or a non-empty list of patterns")
    med = raw.get("mediation")
    if med is None:
        return
    _check_fields(med, MEDIATION_FIELDS, f"{rid}.mediation")
    if "chokepoint" in med and not isinstance(med["chokepoint"], str):
        raise LoadError(f"{rid}: mediation.chokepoint must be a string")
    rp = med.get("residual_paths", [])
    if not isinstance(rp, list) or not all(isinstance(x, str) for x in rp):
        raise LoadError(f"{rid}: mediation.residual_paths must be a list of strings")
    for k in ("sink", "door", "allow"):
        if k in med and not _strings(med[k], "py:"):
            raise LoadError(f"{rid}: mediation.{k} must be a 'py:' path or a non-empty list of them")


def _calls(n: Any) -> Iterator[tuple[str, bool, int]]:
    """(function name, called on a receiver, argument count) for every call in a subset tree."""
    if n[0] == "call":
        yield n[1], n[2] is not None, len(n[3])
    for c in cel.subset.kids(n):
        yield from _calls(c)


def _check_functions(rid: str, source: str, eng: cel.Backend) -> None:
    """Refuse a clause that calls a function the backend does not have (it would error on every call).

    A backend declares its names in `functions`; one that does not (a registered custom backend)
    is not checked here.
    """
    known = getattr(eng, "functions", None)
    if known is None:
        return
    try:
        tree = cel.subset.parse(source)
    except cel.CelSyntaxError:
        return                                   # the backend compiled it; nothing more to check here
    for name, method, nargs in _calls(tree):
        if name not in known:
            form = f"x.{name}()" if method else f"{name}()"
            hint = (" (the subset backend has no uint, bytes or type; use the cel-expr "
                    "backend)" if eng.name == "subset" and name in cel.ENGINE_FUNCTIONS else "")
            raise LoadError(f"{rid}: bad calls {form}, which the {eng.name} CEL backend does not have, so every "
                            f"evaluation would be a rule_error{hint}. Functions on {eng.name}: "
                            f"{', '.join(sorted(known))}")
        if name in cel.subset.CONVERSIONS and method:
            raise LoadError(f"{rid}: bad calls x.{name}(); CEL conversions are global: write {name}(x)")
        why = cel.subset.arity_error(name, method, nargs)
        if why is not None:
            raise LoadError(f"{rid}: bad calls {why}, so every evaluation would be a rule_error")


def _load_rule(raw: Any, reg: Registry, base_dir: pathlib.Path, eng: cel.Backend) -> Rule:
    rid = raw.get("id", "?") if isinstance(raw, dict) else "?"
    if isinstance(raw, dict) and "effects" in raw:
        raise LoadError(f"{rid}: effects belongs to the target; declare it once under targets:")
    _check_fields(raw, RULE_FIELDS, rid)
    _check_mediation(rid, raw)
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        raise LoadError(f"{rid}: missing id")
    subject = raw.get("subject")
    if subject is not None and not isinstance(subject, dict):
        raise LoadError(f"{rid}: subject must be a mapping (subject: {{target: 'py:module.function'}}), "
                        f"got {subject!r}")
    if subject is not None and "target" in subject and not (isinstance(subject["target"], str)
                                                            and subject["target"].startswith("py:")):
        raise LoadError(f"{rid}: subject.target must be a 'py:module.function' string, got {subject['target']!r}")
    if "status" not in raw:
        raise LoadError(f"{rid}: missing status (set only by human review)")
    if "bad" not in raw:
        raise LoadError(f"{rid}: missing bad")
    bind = raw.get("bind", {})
    if not isinstance(bind, dict):
        raise LoadError(f"{rid}: bind must be a mapping")
    for name, path in bind.items():
        _check_path(rid, f"bind {name}", path, reg)
    if isinstance(raw["bad"], str):
        bads = [{"when": raw["bad"], "message": raw.get("statement", rid)}]
    elif isinstance(raw["bad"], list) and raw["bad"]:
        bads = raw["bad"]
    else:
        raise LoadError(f"{rid}: bad must be a CEL string or a non-empty list of clauses")
    clauses, refs, reads = [], set(), set()
    for c in bads:
        _check_fields(c, {"when", "message"}, f"{rid}.bad")
        if not isinstance(c.get("when"), str):
            raise LoadError(f"{rid}.bad: each clause needs a string when")
        try:
            prog = eng.compile(c["when"])
        except cel.CelSyntaxError as e:
            raise LoadError(f"{rid}: bad does not parse: {e}") from e
        _check_functions(rid, c["when"], eng)
        clauses.append((prog, str(c.get("message", rid))))
        refs |= set(prog.references())
        reads |= set(prog.reads() if hasattr(prog, "reads") else prog.references())
    names = {r[0] for r in refs}
    rule = Rule(raw, clauses, names, refs=frozenset(refs), reads=frozenset(reads))
    enforcers = raw.get("enforcers", [])
    if not isinstance(enforcers, list):
        raise LoadError(f"{rid}: enforcers must be a list")

    def names_map(extra: dict[str, str] | None) -> dict[str, str]:
        m = {**bind, **(extra or {})}
        for n in names - DEFAULT_NAMES - set(m):
            if n in reg.providers:
                m[n] = f"provider.{n}"          # auto-bound provider
        undeclared = names - DEFAULT_NAMES - set(m)
        if undeclared:
            raise LoadError(f"{rid}: bad reads undeclared name(s) {sorted(undeclared)}")
        return m

    has_view = any(isinstance((e or {}).get("view") if isinstance(e, dict) else None, dict) for e in enforcers)
    rule.names_for["rule"] = {**bind} if has_view else names_map(None)
    for i, enf in enumerate(enforcers):
        where = f"{rid}.enforcer[{i}]"
        _check_fields(enf, ENFORCER_FIELDS, where)
        if {"point", "timing", "mode"} - set(enf):
            raise LoadError(f"{where}: needs point, timing, mode")
        enf.setdefault("on_violation", "reject")
        enf.setdefault("on_error", "fail_closed")
        point, view = enf["point"], enf.get("view")
        if point == "model_call" and not rule.policy:
            raise LoadError(f"{where}: point model_call needs model_policy")
        if point == "edge" and not rule.policy:
            raise LoadError(f"{where}: an edge enforcer belongs on the rule of the @inv.model_call whose "
                            f"Checked value it receives (a rule with model_policy): the edge re-checks the "
                            f"value against that call's closed schema, and a rule without one has no schema")
        if ("target" in enf) != (point == "edge"):
            raise LoadError(f"{where}: exactly the edge enforcers name their own target")
        if view is not None:
            if not isinstance(view, dict) or point not in VIEW_POINTS:
                raise LoadError(f"{where}: view must be a name map")
            for name, path in view.items():
                _check_path(rid, f"view {name}", path, reg)
        if point in VIEW_POINTS:
            rule.names_for[i] = names_map(view)
        if enf.get("artifact"):                  # conformance binding: artifact hash + test
            path, _, digest = str(enf["artifact"]).partition("#sha256:")
            f = base_dir / path
            if not f.exists():
                raise LoadError(f"{rid}: artifact {path} missing")
            if digest and hashlib.sha256(f.read_bytes()).hexdigest() != digest:
                raise LoadError(f"{rid}: artifact {path} changed; re-run {enf.get('conformance_test')} and re-lock")
    if rule.policy is not None and rule.enforcer("model_call")[1] is None:
        raise LoadError(f"{rid}: model_policy needs a model_call enforcer")
    rule.snapshot = _snapshot_paths(rule, reg)
    if rule.status == "retired":
        return rule
    _check_scopes(rule, reg)
    target_key = rule.target_key
    if target_key:
        rule.target = resolve(target_key)
        t = reg.targets.get(target_key, {"effects": "external"})     # unlisted target: external
        rule.effects = t["effects"]
    elif rule.policy is not None:
        raise LoadError(f"{rid}: a rule with model_policy needs subject.target: the @inv.model_call function "
                        f"(its closed schema is what the model call parses into and every edge re-checks)")
    for i, m in rule.names_for.items():          # provider arguments name declared roots or bound names
        local = {n for n, p in m.items() if not p.startswith("provider.")} | DEFAULT_NAMES
        for p in m.values():
            if p.startswith("provider."):
                for a, ppath in reg.providers[p.split(".")[1]].raw["args"].items():
                    _check_path(rid, f"provider {p.split('.')[1]} arg {a}", ppath, reg, roots=local)
    # every args.<x> resolves and no sync enforcer awaits, against each target known here: the rule's,
    # and an edge enforcer's own (a guard on a rule without subject.target is checked by inv verify)
    for i in [*rule.names_for, *(j for j in range(len(rule.enforcers)) if j not in rule.names_for)]:
        enf = rule.enforcers[i] if i != "rule" else {}
        tk = enf.get("target", target_key)
        if not tk:
            continue
        why = binding_problem(rule, reg, i, resolve(tk), tk)
        if why:
            raise LoadError(why)
    if rule.policy is not None:
        _load_policy(rule, reg)
    _typecheck(rule, reg, [(str(c["when"]), str(c.get("message", rid))) for c in bads])
    _compute_max_status(rule, reg, target_key)
    return rule


def binding_problem(rule: Rule, reg: Registry, i: Any, target: Any, tk: str) -> str | None:
    """Why one enforcer (index i, or "rule") cannot bind against target: an args.<x> its clauses,
    names or provider arguments read that target does not take, or an async provider a sync target
    cannot await (every call would be a rule_error). None when it binds."""
    rid = rule.id
    m = rule.names_for.get(i, rule.names_for.get("rule", {}))
    if i in rule.names_for:
        want = {r[1] for r in rule.refs if r[0] == "args" and len(r) > 1}
        for p in m.values():
            if p.startswith("args."):
                want.add(p.split(".")[1])
            if p.startswith("provider."):
                for ppath in reg.providers[p.split(".")[1]].raw["args"].values():
                    head = ppath.split(".")[0]
                    path = m[head] + ppath[len(head):] if head in m and head not in DEFAULT_NAMES else ppath
                    if path.startswith("args."):
                        want.add(path.split(".")[1])
        missing = want - _params(target)
        if missing:
            return f"{rid}: args.{sorted(missing)[0]} does not resolve against {tk}"
    if i == "rule":
        return None
    enf = rule.enforcers[i]
    if enf["point"] not in EVALUATED or enf["mode"] == "off":
        return None
    fn = inspect.unwrap(target)
    fn = getattr(fn, "__func__", fn)
    if inspect.iscoroutinefunction(fn):
        return None
    for p in m.values():
        if p.startswith("provider.") and reg.providers[p.split(".")[1]].is_async:
            return (f"{rid}: provider {p.split('.')[1]} is async def, but the {enf['point']} enforcer's target "
                    f"{tk} is sync, so it cannot await it (every call would be a rule_error); make {tk} async or "
                    f"the provider sync")
    return None


def _read_paths(rule: Rule, reg: Registry, i: Any) -> Iterator[tuple[str, str]]:
    """(path, how it is read) for every root path one enforcer's evaluation reads: the clauses' own
    paths, the bind entries they read, every view entry, and the arguments of the providers those
    bind. A provider argument naming a bound name reads that name's path, whether or not bad reads
    the name itself (bind: {prev: old_self.size} read only as a provider argument is still old_self)."""
    for r in sorted(rule.refs):
        if r[0] in DEFAULT_NAMES:
            yield ".".join(r), "bad"
    m = rule.names_for.get(i, {})
    view = (rule.enforcers[i].get("view") or {}) if i != "rule" else {}
    for n, path in m.items():
        if n not in rule.names and n not in view:
            continue
        how = "view" if n in view else "bind"
        head, *rest = path.split(".")
        if head == "provider":
            prov = reg.providers.get(rest[0]) if rest else None
            for a, ap in ((prov.raw.get("args") or {}) if prov else {}).items():
                ah = ap.split(".")[0]
                if ah in DEFAULT_NAMES:
                    yield ap, f"provider {rest[0]} argument {a}"
                elif ah in m and not m[ah].startswith("provider."):
                    yield m[ah] + ap[len(ah):], f"provider {rest[0]} argument {a} (bind {ah})"
        else:
            yield path, f"{how} {n}"


def _scope_problem(point: str, timing: str, path: str) -> str | None:
    """Why `path` is never set at an enforcer of this point and timing, with the fix; None if it is."""
    head, *rest = path.split(".")
    if head == "args" and rest and rest[0] == "self":
        return "args never holds self (self is its own root): write self.<field>"
    if head == "args" and rest and rest[0] == "inv_feedback":
        return ("args never holds inv_feedback (it is the parse error the SDK sends the model back); "
                "read the reply or the other arguments")
    guard = point in ("decorator", "middleware")
    if head == "result" and point in EVALUATED and not (guard and timing == "post_effect"):
        if not guard:
            return f"result is never set at a {point} enforcer: the model's parsed reply is `reply`"
        return ("result is never set before the call (pre_effect): it exists only at a post_effect decorator "
                "or middleware enforcer; read args, or put the clause on a post_effect enforcer")
    if head == "reply" and guard:
        return (f"reply is never set at a {point} enforcer: it is the parsed reply of an @inv.model_call (read "
                f"at its model_call and edge enforcers); read args or result")
    if head == "old_self" and point in ("model_call", "edge", "monitor"):
        return (f"old_self is never set at a {point} enforcer: it is the snapshot of self an @inv.guard takes "
                f"before the call; read self or args")
    if head == "old_self" and guard and timing == "pre_effect":
        return ("old_self is the snapshot of self taken before the call, so before the effect (pre_effect) it "
                "is self itself and a transition clause never fires; put it on a post_effect enforcer, or read "
                "self and args")
    return None


def _check_scopes(rule: Rule, reg: Registry) -> None:
    """LoadError for a root an enforcer never sets (it would be a rule_error on every evaluation)."""
    for i, enf in enumerate(rule.enforcers):
        point = enf["point"]
        if point not in VIEW_POINTS:
            continue                          # held elsewhere (type, gateway, build, structure)
        for path, how in _read_paths(rule, reg, i if i in rule.names_for else "rule"):
            why = _scope_problem(point, enf["timing"], path)
            if why:
                raise LoadError(f"{rule.id}: enforcer[{i}] ({point}, {enf['timing']}) reads {path} ({how}), "
                                f"but {why}")


def _snapshot_paths(rule: Rule, reg: Registry) -> list[tuple[str, ...]]:
    """The old_self paths a guard must copy before the call: read by bad, by a bind entry bad reads,
    by any enforcer's view, or by a provider argument of those (only the shortest prefixes)."""
    paths = set()
    keys = list(rule.names_for) or ["rule"]
    for i in keys:
        for path, how in _read_paths(rule, reg, i):
            head, *rest = path.split(".")
            if head != "old_self":
                continue
            if not rest:
                raise LoadError(f"{rule.id}: old_self must be followed by a field path (old_self.<path>), "
                                f"in {how}")
            paths.add(tuple(rest))
    return sorted(p for p in paths if not any(q != p and p[:len(q)] == q for q in paths))


def _schema_type(schema: Any) -> tc.T:
    """The static type of `reply`: a closed pydantic schema is typed by its model's annotations
    (rules read the validated instance, see guard.rule_view), a dataclass by its fields."""
    if schema is None:
        return tc.DYN
    model = getattr(schema, "__inv_pydantic__", None)
    if model is not None:
        return tc.from_py(model)
    if isinstance(schema, dict):
        return tc.T({"map"}, fields={k: tc.from_py(v) for k, v in schema.items()}, closed=True)
    return tc.from_py(schema)


def _type_env(rule: Rule, reg: Registry, i: Any, names: dict[str, str]) -> dict[str, tc.T]:
    """Static types of the names one enforcer's clauses read (dyn where code declares none)."""
    enf = rule.enforcers[i] if i != "rule" else {}
    point = enf.get("point", "decorator")
    env = {n: tc.DYN for n in DEFAULT_NAMES}
    tk = enf.get("target", rule.target_key)
    if point != "monitor" and tk:                       # a monitor's rows are whatever the caller passes
        params, ret = tc.signature_types(resolve(tk))
        params.pop("self", None)
        env["args"] = tc.T({"map"}, fields=params)
        if point in ("decorator", "middleware") and not rule.policy:
            env["result"] = ret
        if rule.policy is not None or point == "edge":
            env["reply"] = _schema_type(getattr(rule.target, "__inv_schema__", None))
    for n, path in names.items():
        head, *rest = path.split(".")
        if head == "provider":
            prov = reg.providers[rest[0]]
            t = tc.from_py(prov.type)
            env[n] = tc.merge(t, tc.T({"null"})) if prov.nullable else t
            continue
        t = env.get(head, tc.DYN)
        for f in rest:
            t = t.field(f)
        env[n] = t
    return env


def _typecheck(rule: Rule, reg: Registry, sources: list[tuple[str, str]]) -> None:
    """Refuse a clause whose operand types have no overload (spec 2.1: type-checked at load)."""
    for i, names in rule.names_for.items():
        env = _type_env(rule, reg, i, names)
        for src, msg in sources:
            try:
                read = tc.check_clause(src, env)
            except tc.TypeMismatch as e:
                where = "" if i == "rule" else f" (enforcer {i}, point {rule.enforcers[i]['point']})"
                raise LoadError(f"{rule.id}: clause {msg!r} is ill-typed{where}: {e}") from None
            except cel.CelSyntaxError:
                continue                                # the backend parsed it; leave it to the backend
            if getattr(reg, "backend_name", None) == "cel-expr" and any(
                    t.kinds and t.kinds & {"timestamp", "duration"} for t in read):
                from .cel.expr_python import NO_PROTOBUF, protobuf_available
                if not protobuf_available():
                    raise LoadError(f"{rule.id}: clause {msg!r} reads a datetime or timedelta, and {NO_PROTOBUF}")


def _compute_max_status(rule: Rule, reg: Registry, target_key: str | None) -> None:
    """The highest status the evidence allows; refuse anything higher."""
    live = [e for e in rule.enforcers if e["mode"] != "off"]
    why_not = []
    if not live:
        rule.max_status = "candidate"
        why_not.append("no enforcer")
    else:
        if not any(e["mode"] == "enforce" for e in live):
            why_not.append("no enforcer with mode enforce")
        structural = all(e["point"] == "structure" for e in live) and bool((rule.raw.get("mediation") or {})
                                                                            .get("sink"))
        if not rule.raw.get("falsified_by") and not structural:
            why_not.append("no falsified_by")         # a structure rule's evidence is the scan itself
        for e in live:
            tk = str(e.get("target", target_key))
            eff = reg.targets.get(tk, {"effects": "external"})["effects"]
            if (e["mode"] == "enforce" and e["timing"] == "post_effect" and e["point"] in EVALUATED
                    and eff != "none"):
                why_not.append(f"timing check fails: post_effect enforcer on {tk} (effects {eff}); "
                               f"rewrite as a precondition or use detect")
        rule.max_status = "detect" if why_not else "enforce"
    if STATUS_RANK.get(rule.status, 0) > STATUS_RANK[rule.max_status]:
        raise LoadError(f"{rule.id}: status {rule.status} is not supported by the evidence "
                        f"(highest allowed: {rule.max_status}): {'; '.join(why_not)}")


def _load_policy(rule: Rule, reg: Registry) -> None:
    rid, pol = rule.id, rule.policy
    if isinstance(pol, dict) and "on_exhaust" in pol:
        raise LoadError(f"{rid}: model_policy has no on_exhaust; the model_call enforcer's on_violation "
                        f"decides once retries are spent")
    _check_fields(pol, POLICY_FIELDS, f"{rid}.model_policy")
    if not isinstance(pol, dict):
        raise LoadError(f"{rid}: model_policy must be a mapping")
    for k in ("format_retries", "semantic_resamples"):
        v = pol.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise LoadError(f"{rid}: model_policy.{k} must be a non-negative integer")
    fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
    _check_fields(fb, {"format", "semantic"}, f"{rid}.model_policy.feedback")
    if fb.get("format", "parser_error") not in ("parser_error", "none") or fb.get("semantic", "none") != "none":
        raise LoadError(f"{rid}: feedback.format must be parser_error|none and feedback.semantic none")
    ab = pol.get("abstain")
    if ab is not None:
        _check_fields(ab, {"field", "value"}, f"{rid}.model_policy.abstain")
        if {"field", "value"} - set(ab):
            raise LoadError(f"{rid}: abstain needs field and value")
        if not isinstance(ab["field"], str) or not ab["field"]:
            raise LoadError(f"{rid}: abstain field must be a field name, not {ab['field']!r}")
        if ab["value"] is not None and type(ab["value"]) not in (str, int, float, bool):
            raise LoadError(f"{rid}: abstain value must be a string, number, bool or null, not {ab['value']!r}")
    held = rule.raw.get("compares_to") == "held_data"
    if held and (pol.get("semantic_resamples", 0) != 0 or fb.get("semantic", "none") != "none"):
        raise LoadError(f"{rid}: compares_to: held_data forces semantic_resamples: 0 and feedback.semantic: none "
                        f"(declared {pol.get('semantic_resamples')})")
    rule.semantic_resamples = 0 if held else pol.get("semantic_resamples", 1)
    tk = rule.target_key
    if fb.get("format", "parser_error") == "parser_error" and "inv_feedback" not in _params(rule.target):
        raise LoadError(f"{rid}: feedback.format parser_error needs a parameter inv_feedback: str | None on {tk}")
    schema = getattr(rule.target, "__inv_schema__", None)
    if schema is None:
        return                                       # verify reports a missing @inv.model_call
    fields = schema_fields(schema)
    name = schema_name(schema)
    if ab is not None:
        if ab["field"] not in fields:
            raise LoadError(f"{rid}: abstain field {ab['field']!r} is not in {name} (fields: {sorted(fields)})")
        t = fields[ab["field"]]
        if typing.get_origin(t) is typing.Literal and not any(literal_eq(ab["value"], a) for a in typing.get_args(t)):
            raise LoadError(f"{rid}: abstain value {ab['value']!r} is not a member of the closed enum "
                            f"{name}.{ab['field']} {list(typing.get_args(t))}")
    for prog, msg in rule.clauses:
        f = _restates_enum(prog, fields, (ab or {}).get("value"))
        if f:
            note = f"{rid}: clause {msg!r} only restates the enum {name}.{f}: held by the schema (rung 1)"
            rule.notes.append(note)
            reg.notes.append(note)


def schema_path() -> pathlib.Path:
    """Path to the bundled JSON Schema for rule files."""
    return pathlib.Path(__file__).with_name("schema") / "invariants.schema.json"
