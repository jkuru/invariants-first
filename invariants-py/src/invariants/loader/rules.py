"""One rule: its fields, its bad clauses compiled on the backend, its enforcers and the names each reads,
the target it binds against, and the highest status its evidence allows."""
from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import re
from typing import Any, Iterator

from .. import cel
from ..confinement import confined
from ..errors import LoadError
from ..model import DEFAULT_NAMES, ENFORCER_FIELDS, EVALUATED, RULE_FIELDS, STATUS_RANK, VIEW_POINTS, Rule
from ..registry import Registry
from ..resolve import close_matches, resolve
from .paths import _check_scopes, _snapshot_paths, binding_problem
from .policy import _load_policy
from .shapes import _check_fields, _check_mediation, _check_path
from .short_forms import SHORT_FIELDS
from .typebind import _typecheck

# A rule id: letters, digits, '-', '_' and '.', starting with a letter or digit (no spaces, no ':', so
# an id is never mistaken for a 'py:' target key in @inv.guard).
RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

# Python functions a clause may reach for, and what CEL writes instead
PYTHON_FUNCTIONS = {"len": "size(x) (or x.size())", "str": "string(x)", "float": "double(x)",
                    "abs": "x < 0 ? -x : x", "any": "list.exists(e, <condition>)",
                    "isinstance": "type(x) == T (cel-expr backend), or a schema"}
_PY_WORDS = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|\b(and|or|not|is|None|True|False)\b""")
_CEL_FOR = {"and": "&&", "or": "||", "not": "!", "is": "==", "None": "null", "True": "true", "False": "false"}


def _python_hint(source: str) -> str:
    """'; CEL writes `and` as &&, ...' for the Python words outside string literals in a clause."""
    found = [m.group(2) for m in _PY_WORDS.finditer(source) if m.group(2)]
    words = list(dict.fromkeys(found))
    if not words:
        return ""
    return "; CEL writes " + ", ".join(f"`{w}` as {_CEL_FOR[w]}" for w in words)


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
            if name in PYTHON_FUNCTIONS and not method:
                raise LoadError(f"{rid}: bad calls {form}, a Python function CEL does not have: write "
                                f"{PYTHON_FUNCTIONS[name]}")
            hint = (" (the subset backend has no uint, bytes or type; use the cel-expr "
                    "backend)" if eng.name == "subset" and name in cel.ENGINE_FUNCTIONS else "")
            close = close_matches(name, sorted(known), n=1)
            hint += f"; did you mean {close[0]}()?" if close else ""
            raise LoadError(f"{rid}: bad calls {form}, which the {eng.name} CEL backend does not have, so every "
                            f"evaluation would be a rule_error{hint}. Functions on {eng.name}: "
                            f"{', '.join(sorted(known))}")
        if name in cel.subset.CONVERSIONS and method:
            raise LoadError(f"{rid}: bad calls x.{name}(); CEL conversions are global: write {name}(x)")
        why = cel.subset.arity_error(name, method, nargs)
        if why is not None:
            raise LoadError(f"{rid}: bad calls {why}, so every evaluation would be a rule_error")


def _check_rule_fields(rid: str, raw: Any) -> None:
    """The rule's own fields: known, id, subject, status and bad present."""
    if isinstance(raw, dict) and "effects" in raw:
        raise LoadError(f"{rid}: effects belongs to the target; declare it once under targets:")
    _check_fields(raw, RULE_FIELDS, rid, also=SHORT_FIELDS)
    _check_mediation(rid, raw)
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        raise LoadError(f"{rid}: missing id")
    if not RULE_ID.match(raw["id"]):
        fixed = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw["id"]).strip("-_.")
        raise LoadError(f"{raw['id']!r}: not a rule id: an id is letters, digits, '-', '_' and '.', starting with "
                        f"a letter or digit (no spaces)" + (f"; for example {fixed!r}" if fixed else ""))
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


def _bad_clauses(rid: str, raw: dict[str, Any]) -> list[Any]:
    """bad as a list of {when, message} clauses (a bare CEL string is one clause)."""
    if isinstance(raw["bad"], str):
        return [{"when": raw["bad"], "message": raw.get("statement", rid)}]
    if isinstance(raw["bad"], list) and raw["bad"]:
        return raw["bad"]
    raise LoadError(f"{rid}: bad must be a CEL string or a non-empty list of clauses")


def _compile_rule(raw: dict[str, Any], rid: str, bads: list[Any], eng: cel.Backend) -> tuple[Rule, set[str]]:
    """Compile each clause on the backend; the Rule, and the root names its clauses read."""
    clauses, refs, reads = [], set(), set()
    for n, c in enumerate(bads, 1):
        if isinstance(c, str):
            raise LoadError(f"{rid}.bad: clause {n} is a bare string; in a list each clause is a mapping: "
                            f"- {{when: {json.dumps(c)}, message: \"...\"}}")
        _check_fields(c, {"when", "message"}, f"{rid}.bad")
        if not isinstance(c.get("when"), str):
            raise LoadError(f"{rid}.bad: each clause needs a string when")
        try:
            prog = eng.compile(c["when"])
        except cel.CelSyntaxError as e:
            which = (f"clause {c['message']!r} " if "message" in c else f"clause {n} ") if len(bads) > 1 else ""
            raise LoadError(f"{rid}: bad {which}does not parse: {e} (in {c['when']!r}){_python_hint(c['when'])}") \
                from e
        _check_functions(rid, c["when"], eng)
        clauses.append((prog, str(c.get("message", rid))))
        refs |= set(prog.references())
        reads |= set(prog.reads() if hasattr(prog, "reads") else prog.references())
    names = {r[0] for r in refs}
    return Rule(raw, clauses, names, refs=frozenset(refs), reads=frozenset(reads)), names


def _names_map(rid: str, names: set[str], bind: dict[str, Any], reg: Registry,
               extra: dict[str, str] | None, rule: Rule | None = None) -> dict[str, str]:
    """Name -> path for one enforcer: bind, its view, and auto-bound providers; every name bad reads."""
    m = {**bind, **(extra or {})}
    for n in names - DEFAULT_NAMES - set(m):
        if n in reg.providers:
            m[n] = f"provider.{n}"          # auto-bound provider
    undeclared = names - DEFAULT_NAMES - set(m)
    if undeclared:
        hints = _undeclared_hints(sorted(undeclared), m, reg, rule)
        raise LoadError(f"{rid}: bad reads undeclared name(s) {sorted(undeclared)}"
                        + (f": {'; '.join(hints)}" if hints else
                           f" (a clause reads {', '.join(sorted(DEFAULT_NAMES))}, a declared provider or a bind "
                           f"name)"))
    return m


def _target_params(rule: Rule | None) -> list[str]:
    """The parameters of the rule's target, in order ([] when it has none or does not resolve yet)."""
    tk = rule.target_key if rule is not None else None
    if not isinstance(tk, str):
        return []
    try:
        fn = inspect.unwrap(resolve(tk))
        return [p for p in inspect.signature(fn).parameters if p not in ("self", "cls")]
    except Exception:                       # the target's own error is reported where it is bound
        return []


def _undeclared_hints(undeclared: list[str], m: dict[str, Any], reg: Registry, rule: Rule | None) -> list[str]:
    """What each undeclared name probably meant: a parameter (args.x), a provider read by its bare name,
    or a near miss of a name the clause can read."""
    params = _target_params(rule)
    readable = sorted({*DEFAULT_NAMES, *reg.providers, *m})
    out = []
    for n in undeclared:
        if n == "provider":
            subs = sorted({r[1] for r in (rule.refs if rule else ()) if r[0] == "provider" and len(r) > 1})
            for s in subs:
                if s in reg.providers:
                    out.append(f"a provider is read by its name: write {s}, not provider.{s}")
                else:
                    declared = sorted(reg.providers)
                    close = close_matches(s, declared, n=1)
                    out.append(f"no provider {s} is declared"
                               + (f" (did you mean {close[0]}?)" if close else
                                  f" (declared: {', '.join(declared)})" if declared else
                                  " (declare it under providers:)") + f"; a provider is read by its name, not "
                                                                       f"provider.{s}")
        elif n in params:
            out.append(f"{n} is a parameter of {rule.target_key if rule else 'the target'}: write args.{n}")
        else:
            close = close_matches(n, readable, n=1)
            pclose = close_matches(n, params, n=1)
            if pclose:
                out.append(f"{n}: did you mean args.{pclose[0]}?")
            elif close:
                out.append(f"{n}: did you mean {close[0]}?")
    return out


def _load_enforcer(rule: Rule, reg: Registry, base_dir: pathlib.Path, names: set[str], bind: dict[str, Any],
                   i: int, enf: Any) -> None:
    """One enforcer: its fields and defaults, its point against the rule, its view, its artifact."""
    rid = rule.id
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
    if "target" in enf and not (isinstance(enf["target"], str) and enf["target"].startswith("py:")):
        raise LoadError(f"{where}: target must be a py: name (py:package.module.function)")
    if view is not None:
        if not isinstance(view, dict) or point not in VIEW_POINTS:
            raise LoadError(f"{where}: view must be a name map")
        for name, path in view.items():
            _check_path(rid, f"view {name}", path, reg)
    if point in VIEW_POINTS:
        rule.names_for[i] = _names_map(rid, names, bind, reg, view, rule)
    if enf.get("artifact"):                  # conformance binding: artifact hash + test
        _check_artifact(rid, enf, base_dir)


def _check_artifact(rid: str, enf: dict[str, Any], base_dir: pathlib.Path) -> None:
    path, _, digest = str(enf["artifact"]).partition("#sha256:")
    f = confined(base_dir / path, f"{rid}: artifact {path}")
    if not f.exists():
        raise LoadError(f"{rid}: artifact {path} missing")
    if digest and hashlib.sha256(f.read_bytes()).hexdigest() != digest:
        raise LoadError(f"{rid}: artifact {path} changed; re-run {enf.get('conformance_test')} and re-lock")


def _bind_target(rule: Rule, reg: Registry) -> str | None:
    """Resolve subject.target and take its effects (an unlisted target is external)."""
    target_key = rule.target_key
    if target_key:
        rule.target = resolve(target_key)
        t = reg.targets.get(target_key, {"effects": "external"})     # unlisted target: external
        rule.effects = t["effects"]
    elif rule.policy is not None:
        raise LoadError(f"{rule.id}: a rule with model_policy needs subject.target: the @inv.model_call function "
                        f"(its closed schema is what the model call parses into and every edge re-checks)")
    return target_key


def _check_provider_args(rule: Rule, reg: Registry) -> None:
    """Provider arguments name declared roots or bound names."""
    for i, m in rule.names_for.items():
        local = {n for n, p in m.items() if not p.startswith("provider.")} | DEFAULT_NAMES
        for p in m.values():
            if p.startswith("provider."):
                for a, ppath in reg.providers[p.split(".")[1]].raw["args"].items():
                    _check_path(rule.id, f"provider {p.split('.')[1]} arg {a}", ppath, reg, roots=local)


def _check_bindings(rule: Rule, reg: Registry, target_key: str | None) -> None:
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


def _load_rule(raw: Any, reg: Registry, base_dir: pathlib.Path, eng: cel.Backend) -> Rule:
    rid = raw.get("id", "?") if isinstance(raw, dict) else "?"
    _check_rule_fields(rid, raw)
    bind = raw.get("bind", {})
    if not isinstance(bind, dict):
        raise LoadError(f"{rid}: bind must be a mapping")
    for name, path in bind.items():
        _check_path(rid, f"bind {name}", path, reg)
    bads = _bad_clauses(rid, raw)
    rule, names = _compile_rule(raw, rid, bads, eng)
    enforcers = raw.get("enforcers", [])
    if not isinstance(enforcers, list):
        raise LoadError(f"{rid}: enforcers must be a list")
    has_view = any(isinstance((e or {}).get("view") if isinstance(e, dict) else None, dict) for e in enforcers)
    rule.names_for["rule"] = {**bind} if has_view else _names_map(rid, names, bind, reg, None, rule)
    for i, enf in enumerate(enforcers):
        _load_enforcer(rule, reg, base_dir, names, bind, i, enf)
    if rule.policy is not None and rule.enforcer("model_call")[1] is None:
        raise LoadError(f"{rid}: model_policy needs a model_call enforcer")
    rule.snapshot = _snapshot_paths(rule, reg)
    if rule.status == "retired":
        return rule
    _check_scopes(rule, reg)
    target_key = _bind_target(rule, reg)
    _check_provider_args(rule, reg)
    _check_bindings(rule, reg, target_key)
    if rule.policy is not None:
        _load_policy(rule, reg)
    _typecheck(rule, reg, [(str(c["when"]), str(c.get("message", rid))) for c in bads])
    _compute_max_status(rule, reg, target_key)
    return rule


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
