"""The four-outcome evaluator, clause lists, and outcome actions."""
from __future__ import annotations

import logging
import time
from typing import Any

from invariants_cel import CelError
from invariants_cel.errors import ALLOW_NON_FINITE

from .errors import RAISE
from .model import DEFAULT_NAMES, Outcome, Rule
from .providers import ProviderFailed, fetch_providers, read_by_clauses, resolve_path
from .registry import Handoff, Registry, current

log = logging.getLogger("invariants")
log.addHandler(logging.NullHandler())


class View(dict[str, Any]):
    """A filled view: the bound names, plus `failed` ({name: why}) for names that could not be bound."""

    failed: dict[str, str]

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.failed = {}


def fill_view(rule: Rule, enf_index: Any, base: dict[str, Any], reg: Registry | None = None,
              cache: dict[Any, Any] | None = None, providers: dict[str, Any] | None = None) -> View:
    """Fill one enforcer's view as the SDK does (providers called now, or from the call's cache).

    A name that cannot be bound (a failed provider, snapshot or path) is left out and listed in
    `failed`, so only the clauses that read it error. providers: the provider table to bind
    from (a guarded call passes the one it started under); default the registry's.
    """
    reg = reg or current()
    m = rule.names_for[enf_index]
    base = {**base, "provider": fetch_providers(reg, rule, m, base, {} if cache is None else cache,
                                                     providers=providers)}
    env = View()
    for k, v in base.items():
        if k in DEFAULT_NAMES:
            if isinstance(v, ProviderFailed):
                env.failed[k] = v.msg
            else:
                env[k] = v
    for n, p in m.items():
        if p.startswith("provider.") and not read_by_clauses(rule, n):
            continue                     # a provider no clause reads was never called
        try:
            env[n] = resolve_path(base, p)
        except CelError as e:
            env.pop(n, None)
            detail = getattr(e, "detail", None)
            env.failed[n] = f"{e}: {detail}" if reg.debug and detail else str(e)
    return env


def _shared_data(rule: Rule, env: dict[str, Any]) -> Any:
    """Project env once for every clause, when the backend supports it; None to project per clause."""
    progs = [p for p, _ in rule.clauses]
    refs = getattr(rule, "reads", None) or getattr(rule, "refs", None)
    if not refs or len(progs) < 2 or not all(hasattr(p, "evaluate_data") for p in progs):
        return None
    try:
        return type(progs[0]).project(env, refs)  # type: ignore[attr-defined]
    except Exception:                 # one path fails to convert: fall back so other clauses still decide
        return None


def _why(prog: Any, failed: dict[str, str]) -> str | None:
    """The bind failures behind a clause's error, when it reads a name that failed."""
    if not failed:
        return None
    try:
        roots = {r[0] for r in prog.references()}
    except Exception:
        roots = set(failed)
    hit = [failed[n] for n in sorted(roots & set(failed))]
    return "; ".join(dict.fromkeys(hit)) or None


def _msg(e: BaseException) -> str:
    try:
        return str(e)
    except Exception:
        return "<unprintable>"


def evaluate_env(rule: Rule, env: dict[str, Any], reg: Registry | None = None) -> Outcome:
    """Run every clause. Any true -> violation (all fired); else any error -> rule_error; else ok.

    Names in env.failed are absent: a clause that reads one errors (CEL's commutative ||/&& still
    apply inside it), and the other clauses still decide. Exception messages from user code (a
    property that raised) and request values quoted by engine errors (a missing map key, a bad
    regex pattern) are left out of the error text unless reg.debug.

    A clause that reads a NaN or +-Infinity is an error (comparisons with NaN are all false, so
    `x < lo || x > hi` would be silently false), unless reg.allow_non_finite.
    """
    reg = reg or current()
    if reg.allow_non_finite:
        token = ALLOW_NON_FINITE.set(True)
        try:
            return _evaluate_env(rule, env, reg)
        finally:
            ALLOW_NON_FINITE.reset(token)
    return _evaluate_env(rule, env, reg)


def _evaluate_env(rule: Rule, env: dict[str, Any], reg: Registry) -> Outcome:
    fired, errors = [], []
    failed: dict[str, str] = getattr(env, "failed", {})
    data = _shared_data(rule, env)
    debug = reg.debug
    for prog, msg in rule.clauses:
        try:
            v = prog.evaluate_data(data) if data is not None else prog.evaluate(env)  # type: ignore[attr-defined]
        except CelError as e:
            detail = getattr(e, "detail", None)
            errors.append(_why(prog, failed) or (f"{e}: {detail}" if debug and detail else str(e)))
            continue
        except Exception as e:        # a backend or conversion defect is a rule_error, never an escape
            errors.append(_why(prog, failed) or f"evaluation raised {type(e).__name__}"
                          + (f": {_msg(e)}" if debug else ""))
            continue
        if not isinstance(v, bool):
            errors.append(f"bad evaluated to {type(v).__name__}, not bool")
        elif v:
            fired.append(msg)
    bound = {n: env[n] for n in getattr(rule, "names", ()) if n in env}
    if fired:
        return Outcome("violation", rule.id, fired, bound=bound)
    if errors:
        return Outcome("rule_error", rule.id, error="; ".join(errors), bound=bound)
    return Outcome("ok", rule.id, bound=bound)


def evaluate(rule: Rule, base: dict[str, Any], enf_index: Any = "rule", reg: Registry | None = None,
             cache: dict[Any, Any] | None = None, providers: dict[str, Any] | None = None) -> Outcome:
    """One evaluation, one of the four outcomes. A rule defect is rule_error, never an exception."""
    try:
        env = fill_view(rule, enf_index, base, reg, cache, providers)
    except CelError as e:
        return Outcome("rule_error", rule.id, error=str(e))
    except Exception as e:
        return Outcome("rule_error", rule.id, error=f"binding raised {type(e).__name__}")
    return evaluate_env(rule, env, reg)


def report(reg: Registry, rule: Rule, enf: dict[str, Any], out: Outcome, action: str,
           attempts: list[dict[str, Any]] | None = None, started: int | None = None,
           refused: bool = False) -> dict[str, Any]:
    """Count the evaluation and emit its event.

    started: time.time_ns() when the check began (the guarded call's check phase, the model call,
    the edge call); the event then carries start_ns and end_ns (now), for spans and latency.
    refused: the outcome is an edge's refusal of a received non-ok verdict, acted on by on_violation;
    the event then carries refused: True (only then).
    """
    ev = {"rule": rule.id, "rev": rule.raw.get("rev"), "outcome": out.kind, "mode": reg.mode(rule, enf),
          "point": enf.get("point"), "timing": enf.get("timing"), "messages": out.messages,
          "error": out.error, "attempts": len(attempts or []) or None, "action": action}
    if started is not None:
        ev["start_ns"], ev["end_ns"] = started, time.time_ns()
    if refused:
        ev["refused"] = True
    reg.record(rule.id, out.kind, ev)
    level = (logging.DEBUG if out.kind == "ok" else logging.ERROR if action == "page"
             else reg.violation_log_level)
    if log.isEnabledFor(level):
        log.log(level, "invariant %s", ev)
    return ev


def ablated(reg: Registry, rule: Rule, point: str | None) -> None:
    """Record that an evaluation was skipped because the rule is ablated (not counted as a run)."""
    ev: dict[str, Any] = {"rule": rule.id, "rev": rule.raw.get("rev"), "outcome": "ablated", "mode": "off", "point": point,
          "timing": None, "messages": [], "error": None, "attempts": None, "action": "proceed"}
    reg.record(rule.id, "ablated", ev)
    log.info("invariant %s", ev)


def _decide(reg: Registry, rule: Rule, enf: dict[str, Any], out: Outcome, refusal: bool) -> str | None:
    """The action for an outcome; None for an enforcer in effective mode off (nothing happens)."""
    mode = reg.mode(rule, enf)
    if mode == "off":
        return None
    if out.kind == "ok" or mode == "detect":
        return "proceed"
    if refusal:
        return str(enf["on_violation"])
    if out.kind == "rule_error" and enf["on_error"] == "fail_open_and_page":
        return "page"
    return str(enf["on_violation"])


def _raise(rule: Rule, action: str, enf: dict[str, Any], out: Outcome, attempts: list[dict[str, Any]] | None,
           review: str | None) -> None:
    what = "; ".join(out.messages) or out.error
    raise RAISE[action](f"{rule.id} {out.kind}: {what}" + (f" (opened {review})" if review else ""),
                        out, attempts, review, point=enf.get("point"))


def _handoff(rule: Rule, action: str, out: Outcome, attempts: list[dict[str, Any]] | None,
             ev: dict[str, Any], reason: str = "") -> Handoff:
    return Handoff(rule.id, action, out, dict(out.bound or {}), list(attempts or []), ev, reason)


def act(reg: Registry, rule: Rule, enf: dict[str, Any], out: Outcome,
        attempts: list[dict[str, Any]] | None = None, refusal: bool = False, started: int | None = None) -> str:
    """Detect never blocks; enforce applies on_violation, or on_error for rule_error; off does nothing.

    route and escalate open a review in the registry's review store; page calls its pager.
    refusal: the outcome is a received verdict that is not ok (an edge given a Checked whose
    verdict is rule_error, say). Enforcing, that is refused by on_violation whatever on_error says:
    on_error covers only errors of this enforcer's own evaluation (guide 2.5, 2.6). An enforcer
    in effective mode off (a retired rule, mode off) records nothing and returns "proceed".
    """
    action = _decide(reg, rule, enf, out, refusal)
    if action is None:
        return "proceed"
    ev = report(reg, rule, enf, out, action, attempts, started, refused=refusal and action != "proceed")
    if action == "page":
        reg.page(_handoff(rule, "page", out, attempts, ev, out.error))
    if action in RAISE:
        review = (reg.hand_off(_handoff(rule, action, out, attempts, ev))
                  if action in ("route", "escalate") else None)
        _raise(rule, action, enf, out, attempts, review)
    return action


async def aact(reg: Registry, rule: Rule, enf: dict[str, Any], out: Outcome,
               attempts: list[dict[str, Any]] | None = None, refusal: bool = False,
               started: int | None = None) -> str:
    """act() for async guards, model calls and edges: a review store with an async aopen() (such as
    JsonlReviews, whose fsync runs in a worker thread) is awaited instead of blocking the event loop."""
    action = _decide(reg, rule, enf, out, refusal)
    if action is None:
        return "proceed"
    ev = report(reg, rule, enf, out, action, attempts, started, refused=refusal and action != "proceed")
    if action == "page":
        reg.page(_handoff(rule, "page", out, attempts, ev, out.error))
    if action in RAISE:
        review = (await reg.ahand_off(_handoff(rule, action, out, attempts, ev))
                  if action in ("route", "escalate") else None)
        _raise(rule, action, enf, out, attempts, review)
    return action
