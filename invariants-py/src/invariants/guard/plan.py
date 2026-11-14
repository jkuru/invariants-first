"""The call core shared by the sync and async wrappers: one guarded call, one model-call run and
one edge call, each pinned to the registry generation current when it started.

The wrappers in decorators.py only call these steps and await (or not) between them; every
decision is made here, once, for both. Nothing runs off the calling thread: no threads, queues
or event loops of its own.
"""
from __future__ import annotations

import copy
import dataclasses
import inspect
import time
from typing import Any, Callable, Generator, Iterator

from ..cel import CelError
from ..errors import CoverageError, Escalation, ParseError
from ..evaluate import aact, ablated, act, evaluate, report
from ..model import Outcome, Rule
from ..providers import ProviderFailed, prefetch_async, prefetch_many
from ..registry import Handoff, Registry, current, note_decision
from ..snapshot import take_snapshot
from .checked import Checked
from .replies import _field_of, _same, parse, parse_error_text, recheck, rule_view


# --- @inv.guard ---------------------------------------------------------------------------------
class _Call:
    """One guarded call: bound arguments, the rules that apply, provider caches and snapshots."""

    __slots__ = ("reg", "providers", "self_", "base", "rules", "snap", "pre", "post", "broken", "started",
                 "timings")

    def __init__(self, reg: Registry, self_: Any, args: dict[str, Any], providers: dict[str, Any],
                 started: int) -> None:
        self.reg, self.self_ = reg, self_
        # one registry generation per call: inv.load mid-call (a hot reload) installs new tables on reg,
        # and this call's rules must keep binding the providers they were loaded with
        self.providers = providers
        self.base: dict[str, Any] = {"args": args, "self": self_}
        self.rules: list[tuple[Rule, list[tuple[int, dict[str, Any]]]]] = []
        self.snap: dict[str, Any] = {}
        self.pre: dict[Any, Any] = {}       # provider values read before the effect
        self.post: dict[Any, Any] = {}      # provider values read after it
        self.broken: dict[tuple[str, Any], str] = {}   # (rule id, enforcer) whose provider prefetch failed
        self.started: dict[str, int] = {"pre_effect": started}   # when each check began (events, spans)
        self.timings: set[str] = set()      # the timings some live enforcer checks at (the others are skipped)

    def enforcers(self, timing: str) -> Iterator[tuple[Rule, int, dict[str, Any]]]:
        for rule, live in self.rules:
            for i, enf in live:
                if enf["timing"] == timing:
                    yield rule, i, enf

    def env_base(self, rule: Rule, timing: str, result: Any = None) -> dict[str, Any]:
        base = {**self.base, "old_self": self.snap.get(rule.id)}
        if timing == "post_effect":
            base["result"] = result
        return base


_SHAPES_MAX = 256       # call shapes a guard remembers as binding (a **kwargs function may see many)


def start(sig: inspect.Signature, keys: tuple[str, ...], points: tuple[str, ...],
          a: tuple[Any, ...], kw: dict[str, Any], shapes: set[tuple[Any, ...]]) -> _Call | None:
    """The call's bound arguments, its live rules (enforcers at points, mode not off, not ablated)
    and their old_self snapshots, taken before the effect. None when no rule is live (each one
    retired, off or ablated, with its ablated events emitted as always): call straight through.

    A call that does not bind is the TypeError it always was, before any rule is looked up. Binding
    depends only on the call's shape (how many positional arguments, which keyword names), so a
    shape in shapes (the guard's shapes that bound before) is bound only once a live rule needs it.
    """
    reg = current()
    bound: inspect.BoundArguments | None = None
    shape = (len(a), *kw)
    if shape not in shapes:
        bound = sig.bind(*a, **kw)
        if len(shapes) < _SHAPES_MAX:
            shapes.add(shape)
    started = time.time_ns()
    providers, rules = reg.generation(keys)
    call: _Call | None = None
    for rule in rules:
        if rule.status == "retired":
            continue
        live = [(i, e) for i, e in enumerate(rule.enforcers)
                if e["point"] in points and reg.mode(rule, e) != "off"]
        if not live:
            continue
        if reg.skips(rule.id):
            for _, enf in live:
                ablated(reg, rule, enf.get("point"))
            continue
        if call is None:
            if bound is None:
                bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            call = _Call(reg, bound.arguments.get("self"),
                         {k: v for k, v in bound.arguments.items() if k != "self"}, providers, started)
        call.rules.append((rule, live))
        for _, e in live:
            call.timings.add(e["timing"])
        if rule.snapshot:
            call.snap[rule.id] = _old_self(rule, call.self_)
    return call


def _old_self(rule: Rule, self_: Any) -> Any:
    """The rule's old_self snapshot of self_, or a ProviderFailed that errors the clauses reading it."""
    try:
        return take_snapshot(rule, self_)
    except CelError as e:
        return ProviderFailed(str(e))
    except Exception as e:
        return ProviderFailed(f"old_self snapshot raised {type(e).__name__}")


async def aprefetch(call: _Call, timing: str, result: Any = None) -> None:
    """Every async provider the enforcers at timing read, awaited together."""
    cache = call.pre if timing == "pre_effect" else call.post
    live = list(call.enforcers(timing))
    try:
        whys = await prefetch_many(call.reg, [(rule, rule.names_for[i], call.env_base(rule, timing, result))
                                              for rule, i, _ in live], cache, call.providers)
    except Exception as e:
        whys = [f"binding providers raised {type(e).__name__}"] * len(live)
    for (rule, i, _), why in zip(live, whys):
        if why is not None:
            call.broken.setdefault((rule.id, i), why)


def outcomes(call: _Call, timing: str, result: Any = None) -> Iterator[tuple[Rule, dict[str, Any], Outcome]]:
    """Each enforcer at timing, evaluated when the caller asks for it (its action may raise first)."""
    reg, cache = call.reg, call.pre if timing == "pre_effect" else call.post
    for rule, i, enf in call.enforcers(timing):
        # a failed old_self snapshot errors only the clauses that read old_self
        broken = call.broken.get((rule.id, i))
        yield rule, enf, (Outcome("rule_error", rule.id, error=broken) if broken is not None else
                          evaluate(rule, call.env_base(rule, timing, result), i, reg, cache, call.providers))


def check(call: _Call, timing: str, result: Any = None) -> None:
    for rule, enf, out in outcomes(call, timing, result):
        act(call.reg, rule, enf, out, started=call.started[timing])


async def acheck(call: _Call, timing: str, result: Any = None) -> None:
    for rule, enf, out in outcomes(call, timing, result):
        await aact(call.reg, rule, enf, out, started=call.started[timing])


# --- @inv.model_call ----------------------------------------------------------------------------
class _ModelRun:
    """The state of one model_call invocation: parse -> abstain -> bad, with bounded retries.

    A rule that is retired, whose model_call enforcer is in effective mode off, or that is ablated
    is passive: the model is called once and its reply parsed into the closed schema, and nothing
    else happens (no format retries, no abstain escalation, no bad, no event but `ablated`).
    """

    __slots__ = ("started", "reg", "providers", "rule", "i", "enf", "passive", "sig", "bound", "schema",
                 "refined", "base", "fb", "prov", "fmt_left", "sem_left", "ab", "record", "feedback", "fed_back")

    def __init__(self, rule_id: str, schema: Any, refined: Any, sig: inspect.Signature,
                 a: tuple[Any, ...], kw: dict[str, Any]) -> None:
        self.started = time.time_ns()
        self.reg = reg = current()
        # one registry generation per call, as @inv.guard: a reload mid-call (a hot inv.load) must not
        # swap the providers this call's rule was loaded with
        self.providers, rules = reg.generation((rule_id,))
        self.rule = rule = rules[0]
        pol = rule.policy or {}
        i, enf = rule.enforcer("model_call")
        if i is None or enf is None:
            raise CoverageError(f"@inv.model_call({rule_id!r}): the rule has no model_call enforcer")
        self.i, self.enf = i, enf
        self.passive = rule.status == "retired" or reg.mode(rule, enf) == "off"
        if not self.passive and reg.skips(rule.id):
            ablated(reg, rule, "model_call")
            self.passive = True
        bound = sig.bind(*a, **kw)
        bound.apply_defaults()
        self.sig, self.bound, self.schema, self.refined = sig, bound, schema, refined
        self.base = {"args": {k: v for k, v in bound.arguments.items() if k not in ("inv_feedback", "self")},
                     "self": bound.arguments.get("self")}
        self.fb = pol.get("feedback", {"format": "parser_error", "semantic": "none"})
        self.prov: dict[Any, Any] = {}                 # this call's provider cache
        self.fmt_left, self.sem_left = pol.get("format_retries", 2), rule.semantic_resamples
        self.ab = pol.get("abstain")
        self.record: list[dict[str, Any]] = []
        self.feedback: str | None = None               # the full text sent to the model as inv_feedback
        self.fed_back: str | None = None               # what the attempt record says was sent (value-free)

    def call_args(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """(args, kwargs) for the next model call, with inv_feedback set when declared."""
        if "inv_feedback" in self.sig.parameters:
            self.bound.arguments["inv_feedback"] = self.feedback
        return self.bound.args, self.bound.kwargs

    def passive_result(self, raw: Any) -> Any:
        """The one reply of a passive run, parsed (verdict ok), or as given (verdict parse_error)."""
        try:
            reply = parse(raw, self.schema)
        except ParseError:
            return self.refined._inv_make(raw, 1, self.rule.id, "parse_error")
        return self.refined._inv_make(reply, 1, self.rule.id, "ok")

    def checked(self, out: Outcome, value: Any) -> Any:
        return self.refined._inv_make(value, len(self.record), self.rule.id, out.kind)

    def finish(self, out: Outcome, value: Any) -> Any:
        act(self.reg, self.rule, self.enf, out, self.record, started=self.started)
        return self.checked(out, value)

    async def afinish(self, out: Outcome, value: Any) -> Any:
        await aact(self.reg, self.rule, self.enf, out, self.record, started=self.started)
        return self.checked(out, value)

    def parsed(self, raw: Any) -> tuple[str, Any]:
        """('retry', None) | ('finish', (outcome, value)) | ('abstain', reply) | ('eval', reply).
        Nothing but an InvariantError escapes."""
        rule, reg = self.rule, self.reg
        try:
            reply = parse(raw, self.schema)
        except ParseError as e:
            told = parse_error_text(e, reg)         # value-free unless debug: it reaches events and logs
            self.record.append({"attempt": len(self.record) + 1, "outcome": "parse_error", "error": told,
                                "fed_back": self.fed_back})
            out = Outcome("parse_error", rule.id, error=told)
            if self.fmt_left:
                self.fmt_left -= 1
                send = self.fb.get("format", "parser_error") == "parser_error"
                self.feedback = e.feedback if send else None      # the model sees the full text
                self.fed_back = told if send else None
                report(reg, rule, self.enf, out, "retry", self.record)
                return "retry", None
            return "finish", (out, raw)
        ab = self.ab
        if ab is not None:
            try:
                abstained = _same(_field_of(reply, ab["field"]), ab["value"])
            except Exception as e:      # by contract unreachable (parse gives plain data); fail closed
                self.record.append({"attempt": len(self.record) + 1, "outcome": "rule_error",
                                    "fed_back": self.fed_back})
                return "finish", (Outcome("rule_error", rule.id, error=f"abstain check raised {type(e).__name__}"),
                                  reply)
            if abstained:
                self.record.append({"attempt": len(self.record) + 1, "outcome": "abstain",
                                    "fed_back": self.fed_back})
                return "abstain", reply
        return "eval", reply

    def handoff(self, reply: Any) -> Handoff:
        note_decision(self.rule.id)          # an abstain is the rule's decision (see --inv-ablate)
        return Handoff(self.rule.id, "escalate", None, {"reply": reply, **self.base}, list(self.record),
                       reason="abstain")

    def abstained(self) -> Outcome:
        """The outcome of an abstain below enforce: recorded (action proceed), no review, no decision."""
        return Outcome("abstain", self.rule.id, [f"model abstained ({(self.ab or {}).get('value')})"])

    def escalation(self, review: str) -> Escalation:
        return Escalation(f"{self.rule.id}: model abstained ({(self.ab or {}).get('value')}); sent to a person "
                          f"(opened {review})", None, self.record, review, rule=self.rule.id, point="model_call")

    def eval_base(self, reply: Any) -> dict[str, Any]:
        return {**self.base, "reply": rule_view(reply)}

    async def aprefetch(self, reply: Any) -> str | None:
        """The async providers the evaluation of reply reads, awaited; why it failed, else None (as
        @inv.guard: a failed prefetch makes the evaluation a rule_error)."""
        try:
            return await prefetch_async(self.reg, self.rule, self.rule.names_for[self.i], self.eval_base(reply),
                                        self.prov, self.providers)
        except Exception as e:
            return f"binding providers raised {type(e).__name__}"

    def judged(self, reply: Any, broken: str | None = None) -> tuple[str, Any]:
        """('retry', None) | ('finish', (outcome, reply)). broken: why the async prefetch failed."""
        rule, reg = self.rule, self.reg
        out = (Outcome("rule_error", rule.id, error=broken) if broken is not None else
               evaluate(rule, self.eval_base(reply), self.i, reg, self.prov, self.providers))
        self.record.append({"attempt": len(self.record) + 1, "outcome": out.kind, "messages": out.messages,
                            "fed_back": self.fed_back})
        if out.kind == "violation" and self.sem_left and reg.mode(rule, self.enf) == "enforce":
            self.sem_left -= 1
            self.feedback = self.fed_back = None     # fresh resample: never the rule text or expected value
            report(reg, rule, self.enf, out, "resample", self.record)
            return "retry", None
        return "finish", (out, reply)              # rule_error never resamples: on_error applies

    def steps(self, prefetch: bool) -> Generator[tuple[str, Any], Any, None]:
        """The retry loop of an active run, driven alike by the sync and the async wrapper (which only
        call, await and send the answer back). Yields ("call", (args, kwargs)) for the model's next raw
        reply, ("prefetch", reply) when prefetch (an async run) for why the async providers bad reads
        failed to fetch (None when they did; a sync run's evaluate() binds its providers itself),
        ("hand_off", handoff) for the id of the review an abstain opens, and last ("finish",
        (outcome, value)): the wrapper then acts on the outcome and stops driving."""
        while True:
            step, v = self.parsed((yield "call", self.call_args()))
            if step == "eval":
                step, v = self.judged(v, (yield "prefetch", v) if prefetch else None)
            if step == "abstain":
                if self.reg.mode(self.rule, self.enf) != "enforce":
                    # below enforce nothing blocks (detect, a candidate or detect rule): the abstain is
                    # recorded and the call goes on with verdict abstain (an edge in force refuses it)
                    yield "finish", (self.abstained(), v)
                    return
                raise self.escalation((yield "hand_off", self.handoff(v)))
            if step == "finish":
                yield "finish", v
                return


# --- @inv.edge ---------------------------------------------------------------------------------
def _received(value: Any) -> tuple[str | None, Any]:
    """(verdict, value) of a received Checked; verdict None when it was not built by inv.model_call
    (no verdict, or a verdict that is not a plain string)."""
    try:
        verdict, reply = object.__getattribute__(value, "verdict"), object.__getattribute__(value, "value")
    except AttributeError:
        return None, None
    return (verdict, reply) if type(verdict) is str else (None, None)


@dataclasses.dataclass(slots=True)
class _EdgeCall:
    """One edge call that is checked: what judge() needs, then the consumer's (args, kwargs).
    broken: why the async prefetch of its providers failed (the evaluation is then a rule_error)."""

    reg: Registry
    rule: Rule
    i: int
    enf: dict[str, Any]
    verdict: str | None
    perr: str | None
    base: dict[str, Any]
    providers: dict[str, Any]
    started: int
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    cache: dict[Any, Any] = dataclasses.field(default_factory=dict)
    broken: str | None = None


def _get_field(obj: Any, f: str) -> Any:
    if isinstance(obj, dict):
        return dict.get(obj, f)          # the dict's own storage: a subclass's __getitem__ or get() is not read
    return getattr(obj, f, None)


def _check_state(obj: Any, declared: Any, who: str) -> None:
    """TypeError unless obj is a state the edge can copy exactly: a dict (any dict: the consumer gets a
    plain dict), or an instance of exactly the dataclass or pydantic model the parameter is annotated
    with. A subclass could answer the consumer's reads (a property, __getattribute__) with something
    other than the Checked the edge re-checked."""
    if isinstance(obj, dict) or not isinstance(declared, type) or issubclass(declared, dict):
        return
    if type(obj) is not declared:
        raise TypeError(f"{who} accepts only a {declared.__name__} state, not a {type(obj).__name__}: the "
                        f"consumer gets a copy of the state, built as a {declared.__name__}")


def _with_field(obj: Any, f: str, value: Any) -> Any:
    """A copy of a state object with one field replaced (the caller's object is unchanged): a plain
    dict for any dict (a dict subclass's own reads never reach the consumer), else a copy of exactly
    the dataclass or pydantic model (_check_state)."""
    if isinstance(obj, dict):
        new = dict(dict.items(obj))
        new[f] = value
        return new
    if hasattr(obj, "model_copy") and isinstance(getattr(type(obj), "model_fields", None), dict):
        return obj.model_copy(update={f: value})
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.replace(obj, **{f: value})
    raise TypeError(f"cannot hand the consumer a copy of a {type(obj).__name__} with {f} re-checked: use a "
                    f"dict (TypedDict), a dataclass or a pydantic model for the state")


def _set_path(root: Any, path: tuple[str, ...], value: Any) -> Any:
    """root with the field at path replaced, each container on the way a copy (_with_field)."""
    if not path:
        return value
    return _with_field(root, path[0], _set_path(_get_field(root, path[0]), path[1:], value))


def no_schema(rule: Rule) -> str:
    """Why an edge of rule cannot re-check what it receives: its target carries no closed schema."""
    return (f"{rule.target_key} has no inv.model_call schema (__inv_schema__), so the edge cannot re-check the "
            f"value it received: the target is not the inv.model_call wrapper (is a decorator above "
            f"@inv.model_call missing functools.wraps?)")


class _EdgePlan:
    """What one @inv.edge needs on every call: where the Checked value arrives (parameter, then
    fields), its Checked subclass, and how to pick the rule's edge enforcer for this consumer."""

    __slots__ = ("rule_id", "sig", "pname", "fields", "refined", "fn", "enforcer_for", "states")

    def __init__(self, rule_id: str, sig: inspect.Signature, path: tuple[str, ...], refined: Any, fn: Any,
                 enforcer_for: Callable[[Rule], tuple[int, dict[str, Any]]],
                 states: tuple[Any, ...] = ()) -> None:
        self.rule_id, self.sig, self.refined, self.fn = rule_id, sig, refined, fn
        self.pname, self.fields = path[0], path[1:]
        self.enforcer_for = enforcer_for
        self.states = states        # the annotated type of each state container on the path

    def prepare(self, a: tuple[Any, ...], kw: dict[str, Any]) -> _EdgeCall | None:
        """None to call straight through; else the checked call."""
        started = time.time_ns()
        reg = current()
        bound = self.sig.bind(*a, **kw)
        bound.apply_defaults()
        value = bound.arguments[self.pname]
        for j, f in enumerate(self.fields):                 # read once: the value checked is this one
            _check_state(value, self.states[j] if j < len(self.states) else None, self.fn.__name__)
            value = _get_field(value, f)
        if type(value) is not self.refined:                 # exactly: a subclass could rebuild itself
            raise TypeError(f"{self.fn.__name__} accepts only {self.refined.__name__}")
        providers, rules = reg.generation((self.rule_id,))  # one generation per call
        rule = rules[0]
        i, enf = self.enforcer_for(rule)
        if reg.mode(rule, enf) == "off":
            return None
        if reg.skips(rule.id):
            ablated(reg, rule, "edge")
            return None
        verdict, reply = _received(value)
        perr: str | None = None
        schema = getattr(rule.target, "__inv_schema__", None)
        call = (a, kw)
        if schema is None:          # nothing to re-check against: refused whatever on_error says
            raise CoverageError(f"@inv.edge({rule.id!r}): {no_schema(rule)}")
        if verdict == "ok":
            try:                    # the closed schema again: a forged or mutated value fails here
                reply = recheck(reply, schema)
            except ParseError as e:
                perr = parse_error_text(e, reg)
            else:                   # the consumer gets the copy the rule judges, never the received one
                call = self.handed(bound, value, reply, verdict)
        base = {"args": {k: v for k, v in bound.arguments.items() if k != "self"},
                "self": bound.arguments.get("self"), "reply": rule_view(reply)}
        return _EdgeCall(reg, rule, i, enf, verdict, perr, base, providers, started, *call)

    def handed(self, bound: inspect.BoundArguments, value: Any, reply: Any,
               verdict: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """The consumer's (args, kwargs): a new Checked over the re-validated reply in place of value."""
        _, attempts, rid, _ = Checked._inv_state(value)
        # the base constructor, for exactly the annotated class: never a method the received value's
        # class could override
        fresh = Checked._inv_make.__func__(self.refined, reply, attempts, rid, verdict)  # type: ignore[attr-defined]
        bound.arguments[self.pname] = _set_path(bound.arguments[self.pname], self.fields, fresh)
        return bound.args, bound.kwargs

    async def aprefetch(self, c: _EdgeCall) -> None:
        """The async providers bad reads, awaited, when it will be evaluated (as @inv.guard: a failed
        prefetch makes the evaluation a rule_error)."""
        if c.verdict != "ok" or c.perr is not None:
            return
        try:
            c.broken = await prefetch_async(c.reg, c.rule, c.rule.names_for[c.i], c.base, c.cache, c.providers)
        except Exception as e:
            c.broken = f"binding providers raised {type(e).__name__}"

    def judge(self, c: _EdgeCall) -> tuple[Outcome, bool]:
        refusal = c.verdict != "ok"          # a received non-ok verdict is refused, never failed open
        rule, refined = c.rule, self.refined
        if c.verdict is None:
            out = Outcome("rule_error", rule.id, [f"received a {refined.__name__} that inv.model_call did not "
                                                  f"build (it has no verdict)"])
        elif c.verdict != "ok":
            out = Outcome(c.verdict if c.verdict in ("violation", "rule_error", "parse_error") else "rule_error",
                          rule.id, [f"received a {refined.__name__} with verdict {c.verdict}"])
        elif c.perr is not None:
            out = Outcome("parse_error", rule.id, error=c.perr)
        elif c.broken is not None:
            out = Outcome("rule_error", rule.id, error=c.broken)
        else:
            out = evaluate(rule, c.base, c.i, c.reg, c.cache, c.providers)
        return out, refusal
