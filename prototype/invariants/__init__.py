"""invariants: a TEACHING PROTOTYPE of the Python SDK sketched in Invariant_first.md section 4.4.

Not the library. Explicit decorators only (inspect.signature(...).bind + functools.wraps):
no AST rewriting, no exec/eval of rule or source text (section 2.3, "No weaving").
Implements decisions D1-D9 (providers, status caps mode, targets/effects, model policy, views,
inferred old_self snapshots, outcome edges, verify, edge re-check).
"""
import collections, contextlib, contextvars, copy, dataclasses, functools, hashlib, importlib, inspect, json
import logging, os, pathlib, types, typing
from . import cel as _cel

try:                                   # a real CEL engine would sit behind this adapter (4.3)
    import celpy as _celpy             # noqa: F401  (detected only; adapter not written, see README)
    CEL_BACKEND = "subset (celpy installed but no adapter yet)"
except ImportError:
    CEL_BACKEND = "subset: invariants/cel.py (no CEL engine installed)"

log = logging.getLogger("invariants"); log.addHandler(logging.NullHandler())
DEFAULT_NAMES = {"args", "result", "reply", "self", "old_self"}
PATH_ROOTS = DEFAULT_NAMES | {"provider"}
FILE_FIELDS = {"spec", "contracts", "targets", "providers", "rules"}
TARGET_FIELDS = {"effects", "transaction"}
PROVIDER_FIELDS = {"type", "from", "args", "consistency"}
RULE_FIELDS = {"id", "rev", "status", "class", "owner", "statement", "why", "subject", "scope", "bind", "bad",
               "compensable", "enforcers", "mediation", "model_policy", "compares_to", "falsified_by",
               "evidence", "applies_to"}
ENFORCER_FIELDS = {"point", "target", "timing", "mode", "on_violation", "on_error", "view", "artifact",
                   "generated", "conformance_test"}
POLICY_FIELDS = {"schema", "format_retries", "semantic_resamples", "feedback", "abstain"}
EVALUATED = {"decorator", "model_call", "edge"}          # points the SDK evaluates in-process
ENUMS = {"class": {"law", "policy"}, "effects": {"none", "transactional", "external"},
         "status": {"candidate", "contested", "detect", "enforce", "retired"},
         "point": {"type", "storage", "gateway", "decorator", "middleware", "model_call", "edge", "build",
                   "structure", "monitor"},
         "timing": {"pre_effect", "post_effect"}, "mode": {"off", "detect", "enforce"},
         "on_violation": {"reject", "halt", "escalate", "route"},
         "on_error": {"fail_closed", "fail_open_and_page"},
         "consistency": {"same_txn", "snapshot", "stale_ok", "immutable"}, "compares_to": {"held_data"}}
MODES = ("off", "detect", "enforce")                     # off < detect < enforce (D2)
CAP = {"candidate": "detect", "contested": "detect", "detect": "detect", "enforce": "enforce", "retired": "off"}
STATUS_RANK = {"candidate": 0, "contested": 0, "detect": 1, "enforce": 2}
HOLDS_RACE = {"same_txn", "immutable"}       # consistencies a held guard may read through (D1)

def effective_mode(mode, status):
    """D2: effective mode = min(enforcer.mode, cap(status))."""
    return min(mode, CAP[status], key=MODES.index)

class LoadError(Exception): """The rule file is invalid: a build failure."""
class CoverageError(Exception): """A rule's target does not carry its guard."""
class ParseError(Exception): """A model reply does not parse into the closed schema."""

class InvariantError(Exception):
    def __init__(self, msg, outcome=None, attempts=None, review_id=None):
        super().__init__(msg); self.outcome, self.attempts, self.review_id = outcome, attempts or [], review_id
class Rejected(InvariantError): """on_violation: reject. The caller gets an error."""
class Halted(InvariantError): """on_violation: halt. An internal invariant broke: a bug."""
class Escalation(InvariantError): """on_violation: escalate (and abstain, always). Carries the full attempt record."""
class Routed(InvariantError): """on_violation: route. A review record was opened (review_id); the call does not proceed."""
class RolledBack(InvariantError): """A guard failed inside an @inv.transaction; the caller swallowed it; the SDK rolled back anyway."""
_RAISE = {"reject": Rejected, "halt": Halted, "escalate": Escalation, "route": Routed}

@dataclasses.dataclass
class Outcome:
    kind: str                          # ok | violation | rule_error | parse_error (2.9)
    rule: str
    messages: list = dataclasses.field(default_factory=list)
    error: str = ""

@dataclasses.dataclass
class Provider:
    name: str
    raw: dict
    fn: object
    type: type
    nullable: bool
    consistency = property(lambda s: s.raw["consistency"])
    def accepts(self, v):
        if v is None: return self.nullable
        return isinstance(v, self.type) and not (self.type in (int, float) and isinstance(v, bool))

@dataclasses.dataclass
class Rule:
    raw: dict
    clauses: list                      # [(ast, message)]; evaluated together (D7)
    names: set
    target: object = None
    effects: str = "external"
    transaction: str = None
    snapshot: list = dataclasses.field(default_factory=list)      # D6: inferred old_self paths
    names_for: dict = dataclasses.field(default_factory=dict)     # enforcer index | "rule" -> {name: path}
    notes: list = dataclasses.field(default_factory=list)
    max_status: str = "candidate"
    semantic_resamples: int = 0
    id = property(lambda s: s.raw["id"])
    status = property(lambda s: s.raw["status"])
    policy = property(lambda s: s.raw.get("model_policy"))
    enforcers = property(lambda s: s.raw.get("enforcers", []))
    target_key = property(lambda s: (s.raw.get("subject") or {}).get("target"))
    def mode(self, enf): return effective_mode(enf["mode"], self.status)
    def enforcer(self, point): return next(((i, e) for i, e in enumerate(self.enforcers) if e["point"] == point), (None, None))

class Registry:
    def __init__(self):
        self.rules, self.providers, self.targets, self.sink, self.reviews, self.notes = {}, {}, {}, [], [], []
        self.evidence = collections.defaultdict(collections.Counter)   # reach counters (2.4)
        self.ablated = set(filter(None, os.environ.get("INV_ABLATE", "").split(",")))

REGISTRY = Registry()
_WRAPPED = []                          # every guarded callable, for verify

# --- load ----------------------------------------------------------------------------------------
def _check_fields(obj, allowed, where):
    if not isinstance(obj, dict): raise LoadError(f"{where}: expected a mapping")
    unknown = set(obj) - allowed
    if unknown: raise LoadError(f"{where}: unknown field(s) {sorted(unknown)}")
    for k, v in obj.items():
        if k in ENUMS and v not in ENUMS[k]: raise LoadError(f"{where}: {k}={v!r} not in {sorted(ENUMS[k])}")

def resolve(target):
    """'py:pkg.mod.Class.method' -> the object, or LoadError (a missing target fails the build)."""
    if not str(target).startswith("py:"): raise LoadError(f"unsupported target {target!r}")
    parts = target[3:].split(".")
    for i in range(len(parts), 0, -1):
        try: obj = importlib.import_module(".".join(parts[:i])); break
        except ModuleNotFoundError: continue
    else: raise LoadError(f"target {target}: no importable module")
    for p in parts[i:]:
        if not hasattr(obj, p): raise LoadError(f"target {target}: {p!r} does not exist")
        obj = getattr(obj, p)
    return obj

_BUILTIN_TYPES = {"int": int, "str": str, "bool": bool, "float": float, "list": list, "dict": dict}

def _parse_type(s):
    """Provider type: 'int' | 'py:mod.Class', optionally suffixed ' | null' (README, finding D1)."""
    base, nullable = (s[: -len("| null")].strip(), True) if s.replace(" ", "").endswith("|null") else (s.strip(), False)
    t = _BUILTIN_TYPES.get(base) or (resolve(base) if base.startswith("py:") else None)
    if not isinstance(t, type): raise LoadError(f"provider type {s!r} does not name a type")
    return t, nullable

def _type_set(ann):
    if typing.get_origin(ann) in (typing.Union, types.UnionType): return frozenset(typing.get_args(ann))
    return frozenset({ann})

def _params(obj):
    fn = inspect.unwrap(obj)
    return set(inspect.signature(fn).parameters) if callable(fn) else set()

def _arg_refs(node):
    refs = set()
    def walk(n):
        if n[0] == "sel" and n[1] == ("id", "args"): refs.add(n[2])
        for c in _cel._kids(n): walk(c)
    walk(node); return refs

def _old_self_paths(node, rid):
    """D6 (amended): every old_self.<path> that bad reads, as tuples; bare old_self is a load error."""
    out = set()
    def walk(n):
        if n[0] == "sel":
            chain, x = [], n
            while x[0] == "sel": chain.append(x[2]); x = x[1]
            if x == ("id", "old_self"): out.add(tuple(reversed(chain))); return
        if n == ("id", "old_self"):
            raise LoadError(f"{rid}: old_self must be followed by a field path (old_self.<path>)")
        for c in _cel._kids(n): walk(c)
    walk(node); return out

def _check_path(rid, what, path, reg, roots=PATH_ROOTS):
    head, *rest = path.split(".")
    if head not in roots: raise LoadError(f"{rid}: {what}: bad root in {path!r}")
    if head == "provider" and (len(rest) != 1 or rest[0] not in reg.providers):
        raise LoadError(f"{rid}: {what}: {path!r} names no declared provider")

def _schema_fields(schema):
    return typing.get_type_hints(schema) if dataclasses.is_dataclass(schema) else dict(schema)

def _restates_enum(ast, fields, abstain_value):
    """A clause of the form !(reply.F in [...]) whose list covers F's closed enum (minus the abstain value)."""
    if not (ast[0] == "un" and ast[1] == "!" and ast[2][0] == "bin" and ast[2][1] == "in"): return None
    lhs, rhs = ast[2][2], ast[2][3]
    if not (lhs[0] == "sel" and lhs[1] == ("id", "reply") and rhs[0] == "list" and all(x[0] == "lit" for x in rhs[1])): return None
    t = fields.get(lhs[2])
    if typing.get_origin(t) is not typing.Literal: return None
    return lhs[2] if set(typing.get_args(t)) - {abstain_value} <= {x[1] for x in rhs[1]} else None

def load(source, install=True, base_dir=None):
    """Load and validate a JSON rule file (path or dict). Raises LoadError on any defect."""
    if isinstance(source, (str, pathlib.Path)):
        base_dir = pathlib.Path(source).resolve().parent
        try: doc = json.loads(pathlib.Path(source).read_text())
        except json.JSONDecodeError as e: raise LoadError(f"{source}: {e}")
    else: doc = source
    base_dir = pathlib.Path(base_dir or ".")
    _check_fields(doc, FILE_FIELDS, "file")
    reg = Registry()
    for key, t in (doc.get("targets") or {}).items():          # D3: effects belong to the target
        _check_fields(t, TARGET_FIELDS, f"target {key}")
        if "effects" not in t: raise LoadError(f"target {key}: missing effects")
        resolve(key)
        if t["effects"] == "transactional":
            txn = t.get("transaction")
            if not txn: raise LoadError(f"target {key}: effects transactional must name its transaction boundary")
            if getattr(resolve(txn), "__inv_txn_key__", None) != txn:
                raise LoadError(f"target {key}: transaction {txn} is not an @inv.transaction boundary")
        elif "transaction" in t: raise LoadError(f"target {key}: only effects transactional takes a transaction")
        reg.targets[key] = dict(t)
    for name, p in (doc.get("providers") or {}).items():       # D1: named, typed, read-only
        _check_fields(p, PROVIDER_FIELDS, f"provider {name}")
        if PROVIDER_FIELDS - set(p): raise LoadError(f"provider {name}: needs {sorted(PROVIDER_FIELDS - set(p))}")
        fn = resolve(p["from"]); got = getattr(fn, "__inv_provider__", None)
        if got != name:
            raise LoadError(f"provider {name}: {p['from']} is not registered with @inv.provider({name!r})"
                            + (f"; it is registered as {got!r}" if got else ""))
        params = list(inspect.signature(fn).parameters)
        if set(params) != set(p["args"]):
            raise LoadError(f"provider {name}: signature {params} does not match args {sorted(p['args'])}")
        typ, nullable = _parse_type(p["type"])
        ann = typing.get_type_hints(fn).get("return")
        if _type_set(ann) != frozenset({typ} | ({type(None)} if nullable else set())):
            raise LoadError(f"provider {name}: {p['from']} returns {ann}, but the record declares {p['type']!r}")
        reg.providers[name] = Provider(name, p, fn, typ, nullable)
    for raw in doc.get("rules", []):
        rule = _load_rule(raw, reg, base_dir)
        if rule.id in reg.rules: raise LoadError(f"{rule.id}: duplicate id")
        reg.rules[rule.id] = rule
    if install:
        REGISTRY.rules, REGISTRY.providers, REGISTRY.targets, REGISTRY.notes = reg.rules, reg.providers, reg.targets, reg.notes
        reg = REGISTRY
    return reg

def _load_rule(raw, reg, base_dir):
    rid = raw.get("id", "?") if isinstance(raw, dict) else "?"
    if isinstance(raw, dict) and "effects" in raw:
        raise LoadError(f"{rid}: effects belongs to the target; declare it once under targets: (D3)")
    _check_fields(raw, RULE_FIELDS, rid)
    if "status" not in raw: raise LoadError(f"{rid}: missing status (set only by human review)")
    if "bad" not in raw: raise LoadError(f"{rid}: missing bad")
    bind = raw.get("bind", {})
    for name, path in bind.items(): _check_path(rid, f"bind {name}", path, reg)
    bads = raw["bad"] if isinstance(raw["bad"], list) else [{"when": raw["bad"], "message": raw.get("statement", rid)}]
    clauses, names = [], set()
    for c in bads:
        _check_fields(c, {"when", "message"}, f"{rid}.bad")
        try: ast = _cel.compile(c["when"])
        except _cel.CelSyntaxError as e: raise LoadError(f"{rid}: bad does not parse: {e}")
        clauses.append((ast, c.get("message", rid))); names |= _cel.free_names(ast)
    rule = Rule(raw, clauses, names)
    paths = set().union(*(_old_self_paths(a, rid) for a, _ in clauses))
    rule.snapshot = sorted(p for p in paths if not any(q != p and p[:len(q)] == q for q in paths))

    def names_map(extra):
        m = {**bind, **(extra or {})}
        for n in names - DEFAULT_NAMES - set(m):
            if n in reg.providers: m[n] = f"provider.{n}"          # auto-bound provider (D1)
        undeclared = names - DEFAULT_NAMES - set(m)
        if undeclared: raise LoadError(f"{rid}: bad reads undeclared name(s) {sorted(undeclared)}")
        return m
    rule.names_for["rule"] = names_map(None) if not any(
        isinstance((e or {}).get("view"), dict) for e in rule.enforcers) else {**bind}
    for i, enf in enumerate(rule.enforcers):
        where = f"{rid}.enforcer[{i}]"
        _check_fields(enf, ENFORCER_FIELDS, where)
        if {"point", "timing", "mode"} - set(enf): raise LoadError(f"{where}: needs point, timing, mode")
        enf.setdefault("on_violation", "reject"); enf.setdefault("on_error", "fail_closed")
        point, view = enf["point"], enf.get("view")
        if point == "model_call" and not rule.policy: raise LoadError(f"{where}: point model_call needs model_policy")
        if ("target" in enf) != (point == "edge"): raise LoadError(f"{where}: exactly the edge enforcers name their own target")
        if view == "rows":
            if point != "storage": raise LoadError(f"{where}: view rows is for storage enforcers")
        elif view is not None:
            if not isinstance(view, dict) or point not in EVALUATED: raise LoadError(f"{where}: view must be a name map or 'rows'")
            for name, path in view.items(): _check_path(rid, f"view {name}", path, reg)
        if point in EVALUATED: rule.names_for[i] = names_map(view)
        if enf.get("artifact"):                  # conformance binding: artifact hash + test (2.7)
            path, _, digest = enf["artifact"].partition("#sha256:")
            f = base_dir / path
            if not f.exists(): raise LoadError(f"{rid}: artifact {path} missing")
            if digest and hashlib.sha256(f.read_bytes()).hexdigest() != digest:
                raise LoadError(f"{rid}: artifact {path} changed; re-run {enf.get('conformance_test')} and re-lock")
    if rule.policy is not None and rule.enforcer("model_call")[1] is None:
        raise LoadError(f"{rid}: model_policy needs a model_call enforcer")
    if rule.status == "retired": return rule
    target_key = rule.target_key
    if target_key:
        rule.target = resolve(target_key)
        t = reg.targets.get(target_key, {"effects": "external"})     # unlisted target: external (D3)
        rule.effects, rule.transaction = t["effects"], t.get("transaction")
        for i, m in rule.names_for.items():                          # every args.<x> resolves (critique 11)
            enf = rule.enforcers[i] if i != "rule" else {}
            tk = enf.get("target", target_key)
            refs = set().union(*(_arg_refs(a) for a, _ in clauses))
            local = {n for n, p in m.items() if not p.startswith("provider.")} | DEFAULT_NAMES
            for p in m.values():
                if p.startswith("args."): refs.add(p.split(".")[1])
                if p.startswith("provider."):
                    prov = reg.providers[p.split(".")[1]]
                    if prov.consistency == "same_txn" and reg.targets.get(tk, {}).get("effects") != "transactional":
                        raise LoadError(f"{rid}: provider {prov.name} is same_txn, but {tk} is not effects: transactional, "
                                        f"so there is no transaction to read it in")
                    for a, ppath in prov.raw["args"].items():
                        _check_path(rid, f"provider {p.split('.')[1]} arg {a}", ppath, reg, roots=local)
                        if ppath.startswith("args."): refs.add(ppath.split(".")[1])
            missing = refs - _params(resolve(tk))
            if missing: raise LoadError(f"{rid}: args.{sorted(missing)[0]} does not resolve against {tk}")
    if rule.policy is not None: _load_policy(rule, reg)
    # D2: the highest status the evidence allows; refuse anything higher.
    live = [e for e in rule.enforcers if e["mode"] != "off"]
    why_not = []
    if not live: rule.max_status = "candidate"; why_not.append("no enforcer")
    else:
        if not any(e["mode"] == "enforce" for e in live): why_not.append("no enforcer with mode enforce")
        if not raw.get("falsified_by"): why_not.append("no falsified_by")
        for e in live:
            tk = e.get("target", target_key)
            eff = reg.targets.get(tk, {"effects": "external"})["effects"]
            if e["mode"] == "enforce" and e["timing"] == "post_effect" and e["point"] in EVALUATED and eff not in ("none", "transactional"):
                why_not.append(f"timing check fails: post_effect enforcer on {tk} (effects {eff}); "
                               f"rewrite as a precondition or use detect (2.2)")
        rule.max_status = "detect" if why_not else "enforce"
    if STATUS_RANK.get(rule.status, 0) > STATUS_RANK[rule.max_status]:
        raise LoadError(f"{rid}: status {rule.status} is not supported by the evidence "
                        f"(highest allowed: {rule.max_status}): {'; '.join(why_not)}")
    return rule

def _load_policy(rule, reg):
    rid, pol = rule.id, rule.policy
    if isinstance(pol, dict) and "on_exhaust" in pol:
        raise LoadError(f"{rid}: model_policy has no on_exhaust; the model_call enforcer's on_violation decides once retries are spent")
    _check_fields(pol, POLICY_FIELDS, f"{rid}.model_policy")
    fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
    _check_fields(fb, {"format", "semantic"}, f"{rid}.model_policy.feedback")
    if fb.get("format", "parser_error") not in ("parser_error", "none") or fb.get("semantic", "none") != "none":
        raise LoadError(f"{rid}: feedback.format must be parser_error|none and feedback.semantic none")
    ab = pol.get("abstain")
    if ab is not None:
        _check_fields(ab, {"field", "value"}, f"{rid}.model_policy.abstain")
        if {"field", "value"} - set(ab): raise LoadError(f"{rid}: abstain needs field and value")
    if rule.raw.get("compares_to") == "held_data" and (pol.get("semantic_resamples", 0) != 0 or fb.get("semantic", "none") != "none"):
        raise LoadError(f"{rid}: compares_to: held_data forces semantic_resamples: 0 and feedback.semantic: none "
                        f"(declared {pol.get('semantic_resamples')})")
    rule.semantic_resamples = 0 if rule.raw.get("compares_to") == "held_data" else pol.get("semantic_resamples", 1)
    i, enf = rule.enforcer("model_call")
    tk = rule.target_key
    if fb.get("format", "parser_error") == "parser_error" and "inv_feedback" not in _params(rule.target):
        raise LoadError(f"{rid}: feedback.format parser_error needs a parameter inv_feedback: str | None on {tk}")
    schema = getattr(rule.target, "__inv_schema__", None)
    if schema is None: return                                       # verify reports a missing @inv.model_call
    fields = _schema_fields(schema)
    if ab is not None:
        if ab["field"] not in fields: raise LoadError(f"{rid}: abstain field {ab['field']!r} is not in {schema.__name__}")
        t = fields[ab["field"]]
        if typing.get_origin(t) is typing.Literal and ab["value"] not in typing.get_args(t):
            raise LoadError(f"{rid}: abstain value {ab['value']!r} is not a member of the closed enum "
                            f"{schema.__name__}.{ab['field']} {list(typing.get_args(t))}")
    for ast, msg in rule.clauses:
        f = _restates_enum(ast, fields, (ab or {}).get("value"))
        if f:
            note = f"{rid}: clause {msg!r} only restates the enum {schema.__name__}.{f}: held by the schema (rung 1)"
            rule.notes.append(note); reg.notes.append(note)

# --- evaluation ----------------------------------------------------------------------------------
class _ProviderFailed:
    def __init__(self, msg): self.msg = msg

def _walk(v, parts):
    """Follow a bind/view/provider path: dict keys or public attributes. Errors are CelError (rule_error)."""
    for p in parts:
        if isinstance(v, _ProviderFailed): raise _cel.CelError(v.msg)
        if isinstance(v, dict):
            if p not in v: raise _cel.CelError(f"no such key {p!r}")
            v = v[p]
        elif p.startswith("_") or not hasattr(v, p): raise _cel.CelError(f"no such field {p!r} on {type(v).__name__}")
        else: v = getattr(v, p)
    if isinstance(v, _ProviderFailed): raise _cel.CelError(v.msg)
    return v

def _resolve_path(base, path):
    head, *rest = path.split(".")
    if head not in base: raise _cel.CelError(f"{head} not available here")
    return _walk(base[head], rest)

def _arg_key(v):
    try: hash(v); return v
    except TypeError: return ("id", id(v))            # unhashable: share only the very same object

def fetch_providers(reg, rule, m, base, cache, in_txn=False):
    """D1: call every provider the names map reads and return {name: value} for this rule.
    Provider args resolve in this rule's binding environment; values are cached per
    (provider, resolved args) for the call, so two rules that bind differently never share one.
    A provider that raises or returns the wrong type becomes a value that turns into rule_error."""
    local, out = dict(base), {}
    for n, p in m.items():
        if not p.startswith("provider."):
            try: local[n] = _resolve_path(base, p)
            except _cel.CelError: pass
    for p in m.values():
        if not p.startswith("provider."): continue
        name = p.split(".")[1]
        if name in out: continue
        prov = reg.providers[name]
        try: args = {a: _resolve_path(local, ap) for a, ap in prov.raw["args"].items()}
        except _cel.CelError as e:
            out[name] = _ProviderFailed(f"provider {name}: an argument does not resolve: {e}"); continue
        key = (name, tuple(sorted((a, _arg_key(v)) for a, v in args.items())))
        if key not in cache:
            if prov.consistency == "same_txn" and not in_txn:
                cache[key] = _ProviderFailed(f"provider {name} is same_txn, but the call is not inside its target's "
                                             f"declared transaction")
            else:
                try:
                    v = prov.fn(**args)
                    cache[key] = v if prov.accepts(v) else _ProviderFailed(
                        f"provider {name} returned {type(v).__name__}, declared {prov.raw['type']}")
                except Exception as e:
                    cache[key] = _ProviderFailed(f"provider {name} raised {type(e).__name__}: {e}")
        out[name] = cache[key]
    return out

def fill_view(rule, enf_index, base, reg=None, cache=None, in_txn=False):
    """Fill one enforcer's view the way the SDK does (providers called now, or taken from the
    call's cache). Returns the name env."""
    reg = reg or REGISTRY
    m = rule.names_for[enf_index]
    base = {**base, "provider": fetch_providers(reg, rule, m, base, {} if cache is None else cache, in_txn)}
    env = {k: v for k, v in base.items() if k in DEFAULT_NAMES}
    for n, p in m.items(): env[n] = _resolve_path(base, p)
    return env

def evaluate_env(rule, env):
    """D7 clause lists: every clause runs. Any true -> violation (all fired); else any error -> rule_error; else ok."""
    fired, errors = [], []
    for ast, msg in rule.clauses:
        try: v = _cel.evaluate(ast, env)
        except _cel.CelError as e: errors.append(str(e)); continue
        if not isinstance(v, bool): errors.append(f"bad evaluated to {type(v).__name__}, not bool")
        elif v: fired.append(msg)
    if fired: return Outcome("violation", rule.id, fired)
    if errors: return Outcome("rule_error", rule.id, error="; ".join(errors))
    return Outcome("ok", rule.id)

def evaluate(rule, base, enf_index="rule", reg=None, cache=None, in_txn=False):
    """One evaluation, one of the four outcomes. Never raises for a rule defect: that is rule_error."""
    try: env = fill_view(rule, enf_index, base, reg, cache, in_txn)
    except _cel.CelError as e: return Outcome("rule_error", rule.id, error=str(e))
    return evaluate_env(rule, env)

def _report(reg, rule, enf, out, action, attempts=None):
    reg.evidence[rule.id]["runs"] += 1; reg.evidence[rule.id][out.kind] += 1
    ev = {"rule": rule.id, "rev": rule.raw.get("rev"), "outcome": out.kind, "mode": rule.mode(enf),
          "point": enf.get("point"), "timing": enf.get("timing"), "messages": out.messages,
          "error": out.error, "attempts": len(attempts or []) or None, "action": action}
    reg.sink.append(ev)
    (log.info if out.kind == "ok" else log.error if action == "page" else log.warning)("invariant %s", ev)
    return ev

def _act(reg, rule, enf, out, attempts=None):
    """D7: detect never blocks; enforce applies on_violation, or on_error for rule_error (fail_closed = on_violation)."""
    if out.kind == "ok" or rule.mode(enf) == "detect": action = "proceed"
    elif out.kind == "rule_error" and enf["on_error"] == "fail_open_and_page": action = "page"
    else: action = enf["on_violation"]
    _report(reg, rule, enf, out, action, attempts)
    if action in _RAISE:
        review = None
        if action == "route":
            review = f"review-{len(reg.reviews) + 1}"
            reg.reviews.append({"id": review, "rule": rule.id, "outcome": out})
        what = "; ".join(out.messages) or out.error
        raise _RAISE[action](f"{rule.id} {out.kind}: {what}" + (f" (opened {review})" if review else ""),
                             out, attempts, review)
    return action

# --- old_self (D6) -------------------------------------------------------------------------------
def _plain(v, depth=0):
    if depth > 64: return False
    if v is None or isinstance(v, (bool, int, float, str)): return True
    if isinstance(v, (list, tuple)): return all(_plain(x, depth + 1) for x in v)
    if isinstance(v, dict): return all(isinstance(k, (str, int)) and _plain(x, depth + 1) for k, x in v.items())
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return all(_plain(getattr(v, f.name), depth + 1) for f in dataclasses.fields(v))
    return False

def _take_snapshot(rule, obj):
    """Deep-copy only the referenced old_self paths; never touch anything else on self."""
    snap = {}
    for path in rule.snapshot:
        v = _walk(obj, path)
        if not _plain(v): raise _cel.CelError(f"old_self.{'.'.join(path)} is not plain data ({type(v).__name__})")
        d = snap
        for p in path[:-1]: d = d.setdefault(p, {})
        d[path[-1]] = copy.deepcopy(v)
    return snap

def check_snapshot(key, instance, reg=None):
    """Test-time check with a real instance: every old_self path the rules on `key` read is plain data.
    A path that is not plain data is a LoadError (D6), not a runtime surprise."""
    checked = []
    for rule in _rules_for(key, reg or REGISTRY):
        for path in rule.snapshot:
            try: v = _walk(instance, path)
            except _cel.CelError as e: raise LoadError(f"{rule.id}: old_self.{'.'.join(path)}: {e}")
            if not _plain(v):
                raise LoadError(f"{rule.id}: old_self.{'.'.join(path)} is not plain data on a real "
                                f"{type(instance).__name__} (it is {type(v).__name__})")
            checked.append(f"{rule.id}:old_self.{'.'.join(path)}")
    return checked

# --- transactions (D3) ---------------------------------------------------------------------------
_TXN = contextvars.ContextVar("inv_txn", default=())

class _Frame:
    def __init__(self, key, owner): self.key, self.owner, self.rollback_only = key, owner, False

def transaction(fn):
    """@inv.transaction on a generator method (like contextlib.contextmanager) that BEGINs, yields, and
    COMMITs, or ROLLBACKs when an exception is thrown in. The SDK marks the boundary so a guard can
    confirm at runtime that it runs inside it; a failed guard dooms the transaction even if swallowed."""
    key, inner = f"py:{fn.__module__}.{fn.__qualname__}", contextlib.contextmanager(fn)
    @functools.wraps(fn)
    @contextlib.contextmanager
    def wrapper(*a, **kw):
        frame = _Frame(key, a[0] if a else None)
        token = _TXN.set(_TXN.get() + (frame,))
        try:
            with inner(*a, **kw) as v:
                yield v
                if frame.rollback_only:
                    raise RolledBack(f"a guard failed inside {key}; the transaction was rolled back")
        finally: _TXN.reset(token)
    wrapper.__inv_txn_key__ = key
    return wrapper

def _boundary(key, owner):
    for f in reversed(_TXN.get()):
        if f.key == key and (owner is None or f.owner is owner): return f
    return None

# --- guard ---------------------------------------------------------------------------------------
def _rules_for(key, reg):
    if key.startswith("py:"): return [r for r in reg.rules.values() if r.target_key == key]
    if key not in reg.rules: raise LoadError(f"guard names rule {key!r}, which is not loaded")
    return [reg.rules[key]]

def provider(name):
    """@inv.provider("approval"): registers a read-only provider; the loader checks name, args and return type."""
    def deco(fn): fn.__inv_provider__ = name; return fn
    return deco

def guard(*keys):
    """@inv.guard("R-1", ...) or @inv.guard("py:jobs.split_work"): pre- and post-effect checks."""
    def deco(fn):
        sig = inspect.signature(fn)
        def begin(a, kw):
            reg = REGISTRY
            bound = sig.bind(*a, **kw); bound.apply_defaults()
            self_ = bound.arguments.get("self")
            call = {"reg": reg, "self": self_, "prov": {}, "snap": {}, "rules": [],
                    "base": {"args": {k: v for k, v in bound.arguments.items() if k != "self"}, "self": self_}}
            for rule in (r for k in keys for r in _rules_for(k, reg)):
                if rule.status == "retired" or rule.id in reg.ablated: continue
                frame = _boundary(rule.transaction, self_) if rule.effects == "transactional" else None
                call["rules"].append((rule, frame))
                if rule.snapshot:
                    try: call["snap"][rule.id] = _take_snapshot(rule, self_)
                    except _cel.CelError as e: call["snap"][rule.id] = _ProviderFailed(str(e))
                for i, enf in enumerate(rule.enforcers):     # D1: providers bound before pre-effect checks
                    if enf["point"] == "decorator" and rule.mode(enf) != "off":
                        fetch_providers(reg, rule, rule.names_for[i], call["base"], call["prov"], frame is not None)
            check(call, "pre_effect"); return call
        def check(call, timing, result=None):
            reg = call["reg"]
            for rule, frame in call["rules"]:
                for i, enf in enumerate(rule.enforcers):
                    if enf["point"] != "decorator" or enf["timing"] != timing or rule.mode(enf) == "off": continue
                    snap = call["snap"].get(rule.id)
                    if timing == "post_effect" and rule.effects == "transactional" and frame is None:
                        out = Outcome("rule_error", rule.id, error=f"cannot confirm {rule.target_key} runs inside "
                                      f"{rule.transaction}; a post-effect guard needs that boundary to roll back")
                    elif isinstance(snap, _ProviderFailed): out = Outcome("rule_error", rule.id, error=snap.msg)
                    else:
                        base = {**call["base"], "old_self": snap}
                        if timing == "post_effect": base["result"] = result
                        out = evaluate(rule, base, i, reg, call["prov"], frame is not None)
                    try: _act(reg, rule, enf, out)
                    except InvariantError:
                        if frame is not None: frame.rollback_only = True    # D3: roll back on violation
                        raise
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                call = begin(a, kw); result = await fn(*a, **kw)
                check(call, "post_effect", result); return result
        else:
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                call = begin(a, kw); result = fn(*a, **kw)
                check(call, "post_effect", result); return result
        wrapper.__inv_rules__, wrapper.__inv_kind__ = keys, "guard"
        _WRAPPED.append(wrapper); return wrapper
    return deco

# --- model calls (D4) and edges (D9) -------------------------------------------------------------
class Checked:
    """Base for refined types (CheckedCause): built by model_call, with a verdict (D4).
    Python cannot make this unforgeable (object.__new__ exists): the type is convenience,
    enforce(known-callers); an @inv.edge re-checks bad on what it receives (D9)."""
    __slots__ = ("value", "attempts", "rule", "verdict")
    def __init__(self, *a, **k): raise TypeError(f"{type(self).__name__} is constructed only by inv.model_call")
    @classmethod
    def _inv_make(cls, value, attempts, rule, verdict):
        obj = object.__new__(cls)
        for k, v in (("value", value), ("attempts", attempts), ("rule", rule), ("verdict", verdict)):
            object.__setattr__(obj, k, v)
        return obj
    def __setattr__(self, k, v): raise AttributeError("checked values are immutable")
    def __getattr__(self, k): return getattr(object.__getattribute__(self, "value"), k)
    def __repr__(self): return f"{type(self).__name__}({self.value!r}, verdict={self.verdict}, attempts={self.attempts})"

def parse(raw, schema):
    """STEP 1 (2.5): parse into a closed schema (dataclass or {field: type}); extras/missing = error."""
    try: data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except json.JSONDecodeError as e: raise ParseError(f"not JSON: {e.msg}")
    if not isinstance(data, dict): raise ParseError("reply is not an object")
    fields = _schema_fields(schema)
    extra, missing = set(data) - set(fields), set(fields) - set(data)
    if extra: raise ParseError(f"extra fields forbidden: {sorted(extra)}")
    if missing: raise ParseError(f"missing fields: {sorted(missing)}")
    for k, t in fields.items():
        if typing.get_origin(t) is typing.Literal:
            if data[k] not in typing.get_args(t):
                raise ParseError(f"field {k!r}: {data[k]!r} is not one of {list(typing.get_args(t))}")
        elif not (isinstance(data[k], t) and not (t in (int, float) and isinstance(data[k], bool))):
            raise ParseError(f"field {k!r}: {data[k]!r} is not {getattr(t, '__name__', t)}")
    return schema(**data) if dataclasses.is_dataclass(schema) else data

def model_call(rule_id, schema):
    """@inv.model_call("R-7", schema=CauseReply): parse -> abstain -> bad -> accept, with bounded retries.
    Once retries are spent, the model_call enforcer's on_violation decides (D4 amended)."""
    def deco(fn):
        sig = inspect.signature(fn)
        refined = sig.return_annotation
        if not (isinstance(refined, type) and issubclass(refined, Checked)):
            raise TypeError(f"{fn.__name__} must be annotated to return an inv.Checked subclass")
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            reg = REGISTRY; rule = _rules_for(rule_id, reg)[0]; pol = rule.policy or {}
            i, enf = rule.enforcer("model_call")
            mode = "detect" if rule.mode(enf) == "off" else rule.mode(enf)    # off acts as detect: never verdict ok unchecked
            bound = sig.bind(*a, **kw); bound.apply_defaults()
            base = {"args": {k: v for k, v in bound.arguments.items() if k != "inv_feedback"}}
            fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
            prov = {}                                  # this call's provider cache
            fmt_left, sem_left = pol.get("format_retries", 2), rule.semantic_resamples
            ab, record, feedback = pol.get("abstain"), [], None
            def finish(out, value):        # enforce: non-ok raises via on_violation / on_error; else a verdict
                _act(reg, rule, {**enf, "mode": mode} if mode != enf["mode"] else enf, out, record)
                return refined._inv_make(value, len(record), rule.id, out.kind)
            while True:
                call_kw = {**bound.arguments, "inv_feedback": feedback} if "inv_feedback" in sig.parameters else bound.arguments
                raw = fn(**call_kw)
                try: reply = parse(raw, schema)
                except ParseError as e:
                    record.append({"attempt": len(record) + 1, "outcome": "parse_error", "error": str(e), "fed_back": feedback})
                    out = Outcome("parse_error", rule.id, error=str(e))
                    if fmt_left:                       # retry; the parser error is fed back if format: parser_error
                        fmt_left -= 1; feedback = str(e) if fb.get("format", "parser_error") == "parser_error" else None
                        _report(reg, rule, enf, out, "retry", record); continue
                    return finish(out, raw)
                if ab is not None and getattr(reply, ab["field"], None) == ab["value"]:   # before bad, always escalates
                    record.append({"attempt": len(record) + 1, "outcome": "abstain", "fed_back": feedback})
                    raise Escalation(f"{rule.id}: model abstained ({ab['value']}); sent to a person", None, record)
                out = Outcome("ok", rule.id) if rule.id in reg.ablated else evaluate(rule, {**base, "reply": reply}, i, reg, prov)
                record.append({"attempt": len(record) + 1, "outcome": out.kind, "messages": out.messages, "fed_back": feedback})
                if out.kind == "violation" and sem_left and mode == "enforce":
                    sem_left -= 1; feedback = None     # fresh resample: never the rule text or expected value
                    _report(reg, rule, enf, out, "resample", record); continue
                return finish(out, reply)              # rule_error never resamples: on_error applies
        wrapper.__inv_rules__, wrapper.__inv_kind__, wrapper.__inv_schema__ = (rule_id,), "model_call", schema
        _WRAPPED.append(wrapper); return wrapper
    return deco

def edge(rule_id):
    """@inv.edge("R-7") on the consumer of a Checked value (2.6, D4, D9). The type gate stops honest
    mistakes; then the verdict must be ok, and bad is re-evaluated on the received value (stops forgery)."""
    def deco(fn):
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        pname, refined = next(((n, t) for n, t in hints.items() if isinstance(t, type) and issubclass(t, Checked)), (None, None))
        if pname is None: raise TypeError(f"{fn.__name__}: an edge needs a parameter annotated with an inv.Checked subclass")
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            reg = REGISTRY
            bound = sig.bind(*a, **kw); bound.apply_defaults()
            value = bound.arguments[pname]
            if not isinstance(value, refined): raise TypeError(f"{fn.__name__} accepts only {refined.__name__}")
            rule = _rules_for(rule_id, reg)[0]
            i = next(i for i, e in enumerate(rule.enforcers) if e["point"] == "edge")
            enf = rule.enforcers[i]
            if rule.id in reg.ablated or rule.mode(enf) == "off": return fn(*a, **kw)
            verdict = object.__getattribute__(value, "verdict")
            if verdict != "ok":
                out = Outcome(verdict if verdict in ("violation", "rule_error", "parse_error") else "rule_error",
                              rule.id, [f"received a {refined.__name__} with verdict {verdict}"])
            else:
                reply = object.__getattribute__(value, "value")
                schema = getattr(rule.target, "__inv_schema__", None)
                try:
                    if schema is not None and dataclasses.is_dataclass(schema):
                        if not isinstance(reply, schema): raise ParseError(f"value is {type(reply).__name__}, not {schema.__name__}")
                        parse(dataclasses.asdict(reply), schema)
                    out = evaluate(rule, {"args": dict(bound.arguments), "reply": reply}, i, reg)
                except ParseError as e: out = Outcome("parse_error", rule.id, error=str(e))
            _act(reg, rule, enf, out)
            return fn(*a, **kw)
        wrapper.__inv_rules__, wrapper.__inv_kind__ = (rule_id,), "edge"
        _WRAPPED.append(wrapper); return wrapper
    return deco

# --- verify (D8), standing (D1/D2), ablation ----------------------------------------------------
def verify_coverage(reg=None):
    """`inv verify` (2.3, D8): every rule's target carries its guard, and every guard names a rule in the
    file. The code-graph `inv scan` is out of scope for the prototype."""
    reg, problems = reg or REGISTRY, []
    for rule in reg.rules.values():
        if rule.status == "retired" or rule.target is None: continue
        if rule.policy or any(e["point"] == "decorator" for e in rule.enforcers):
            keys = getattr(rule.target, "__inv_rules__", ())
            if rule.id not in keys and rule.target_key not in keys:
                problems.append(f"{rule.id}: {rule.target_key} is not wrapped by its guard")
            elif rule.policy and getattr(rule.target, "__inv_kind__", "") != "model_call":
                problems.append(f"{rule.id}: has model_policy but target is not @inv.model_call")
            elif rule.policy and getattr(rule.target.__inv_schema__, "__name__", "") != rule.policy.get("schema"):
                problems.append(f"{rule.id}: decorator schema differs from model_policy.schema")
        for e in rule.enforcers:
            if e["point"] == "edge":
                t = resolve(e["target"])
                if getattr(t, "__inv_kind__", "") != "edge" or rule.id not in getattr(t, "__inv_rules__", ()):
                    problems.append(f"{rule.id}: edge target {e['target']} is not wrapped by @inv.edge({rule.id!r})")
    for w in _WRAPPED:
        for k in w.__inv_rules__:
            if not k.startswith("py:") and k not in reg.rules:
                problems.append(f"{w.__qualname__}: guards {k}, which is not in the rule file")
    if problems: raise CoverageError("; ".join(problems))
    return sum(1 for r in reg.rules.values() if r.target is not None)

verify = verify_coverage

def standing(rule_id, reg=None, scan=False):
    """Computed standing (D2): held | enforce(known-callers) | monitored | candidate, with the reasons.
    held needs status enforce, an enforcing enforcer, a complete mediation, verify and scan passing (D8),
    and no provider other than same_txn or immutable without a storage enforcer behind it (D1)."""
    reg = reg or REGISTRY; rule = reg.rules[rule_id]
    if rule.status == "retired": return "retired", []
    modes = [rule.mode(e) for e in rule.enforcers]
    if rule.status in ("candidate", "contested") or not any(m != "off" for m in modes): return "candidate", []
    if "enforce" not in modes: return "monitored", ["no enforcer in effective mode enforce"]
    why = []
    med = rule.raw.get("mediation") or {}
    if not med.get("chokepoint"): why.append("mediation names no chokepoint")
    if med.get("residual_paths"): why.append(f"residual paths: {med['residual_paths']}")
    weak = sorted({p.split(".")[1] for m in rule.names_for.values() for p in m.values()
                   if p.startswith("provider.") and reg.providers[p.split(".")[1]].consistency not in HOLDS_RACE})
    if weak and not any(e["point"] == "storage" and rule.mode(e) == "enforce" for e in rule.enforcers):
        why.append("reads shared state through " + ", ".join(f"{p} ({reg.providers[p].consistency})" for p in weak)
                   + " with no storage enforcer: the guard is an early refusal only")
    try: verify_coverage(reg)
    except CoverageError as e: why.append(f"verify fails: {e}")
    if not scan: why.append("inv scan not run (out of scope for the prototype)")
    return ("held" if not why else "enforce(known-callers)"), why

@contextlib.contextmanager
def ablated(rule_id):
    """Disable one guard: the `inv status --mutate` ablation of 4.3 (falsified_by must then fail)."""
    REGISTRY.ablated.add(rule_id)
    try: yield
    finally: REGISTRY.ablated.discard(rule_id)

@contextlib.contextmanager
def using(reg):
    """Run guards against another loaded registry (a variant rule file), then restore."""
    global REGISTRY
    old, REGISTRY = REGISTRY, reg
    try: yield reg
    finally: REGISTRY = old
