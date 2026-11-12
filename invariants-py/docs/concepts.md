# Concepts

Every layer of a system is an API: an HTTP route, a service method, a repository method, a model
call, an external client. A rule is a law on one of those boundaries, and there are two kinds:

- **a value law** says which inputs (`args`) or outputs (`result`, `reply`) an API must never accept
  or produce. Pinned pydantic constraints are value laws, and so is a rule with a `guard` and `bad`;
- **a path law** says which upper API a lower one may be called through: a door rule (`door`,
  `sink`), checked by `inv scan`.

Rules stay on the boundary. What happens inside an implementation, and below the lowest API (SQL,
triggers, transactions, isolation levels, private fields), is the implementation's business. Earlier
releases had features that reached below the API (storage enforcers, `tables`, `transactional`
effects, provider consistency levels, `.sql` contracts). They were removed; a rule file that still
uses one fails to load with a message that says what to do instead.

This page explains the moving parts: what a rule is, where it runs, what it can answer, and how
the library decides whether a rule really holds. Every rule-file field is listed in
[rule-file.md](rule-file.md), and the whole system, end to end, is in
[Invariant-first](design-guide.md).

- [Rules, targets and effects](#rules-targets-and-effects)
- [Enforcers](#enforcers)
- [Outcomes](#outcomes)
- [Status and mode](#status-and-mode)
- [Standing](#standing)
- [Providers](#providers)
- [Views](#views)
- [Transition rules: `old_self`](#transition-rules-old_self)
- [Model calls](#model-calls)
- [Edges](#edges)
- [Async](#async)
- [Ablation](#ablation)
- [Operations](#operations)

## Rules, targets and effects

A **rule** is a record with an `id`, a human-set `status`, and a CEL expression `bad` that is true
when the property is broken. `bad` may be one string or a list of `{when, message}` clauses. Rules
never say what to do, only what must not happen. The library never rewrites your code's AST, and it
never calls `eval` or `exec` on rule text or source code. Rules run through a pluggable CEL engine
([cel.md](cel.md)).

Each rule has a subject **target**, a `py:` path. The target declares its **effects** once, under
`targets:`:

| Effects | Meaning | Who may enforce |
|---|---|---|
| `none` | pure | pre-effect and post-effect checks |
| `external` | cannot be undone; the default for a target that is not listed | pre-effect checks only; a post-effect check on it can only detect |

## Enforcers

An enforcer is where a rule is checked:

| Point | How it runs |
|---|---|
| `decorator`, `middleware` | `@inv.guard` on a function, or `invariants.integrations.fastapi.guard` on a route |
| `model_call` | `@inv.model_call`: parse, abstain, `bad`, with bounded retries |
| `edge` | `@inv.edge` on the consumer of a model-call result |
| `monitor` | `inv.monitor(rule_id, rows)` over stored rows; it never blocks |
| `structure` | `inv scan` ([scan.md](scan.md)) |
| `type`, `gateway`, `build` | held elsewhere (a closed `Literal`, a gateway, a build step) and recorded for standing |

Each enforcer has a `timing` (`pre_effect` or `post_effect`), a `mode` (`off`, `detect` or
`enforce`), an `on_violation` (`reject`, `halt`, `escalate` or `route`; the default is `reject`) and
an `on_error` (`fail_closed`, which applies `on_violation`, or `fail_open_and_page`, which proceeds
and pages).

When a rule fires, `on_violation` decides what is raised: `inv.Rejected`, `inv.Halted`,
`inv.Escalation` or `inv.Routed`. Each of these carries `.rule`, `.outcome`, `.attempts`, `.point`
and `.bound` (the values the rule read). `Routed` and `Escalation` also carry `.review_id`. If no
review can be opened, route and escalate raise `inv.ReviewNotOpened` instead (see
[Operations](#operations)).

## Outcomes

Every evaluation has one of four outcomes:

| Outcome | Meaning |
|---|---|
| `ok` | no clause is true |
| `violation` | at least one clause is true (every clause that fired is reported) |
| `rule_error` | the rule could not be evaluated: a missing field, a failed provider, a cost limit, a NaN |
| `parse_error` | a model reply does not fit its closed schema |

Any clause true is a violation; otherwise any clause error is a `rule_error`; otherwise the outcome
is `ok`. A rule error is never a violation: its enforcer's `on_error` decides what happens. Detect
mode reports and always proceeds.

## Status and mode

`status` is what review decided: `candidate`, `contested`, `detect`, `enforce` or `retired`. The
loader computes the highest status the evidence supports (`Rule.max_status`) and refuses a higher
one. An `enforce` rule needs an enforcer whose mode is `enforce`, a `falsified_by` list, and a
passing timing check (a post-effect enforcer that enforces needs `effects: none`; on an `external`
target the loader refuses it, since the effect has already happened).

An enforcer's effective mode is `min(mode, cap(status))` (`inv.effective_mode`), so a `detect`
rule never blocks, whatever its enforcers say.

In YAML, quote `mode: "off"`. A bare `off` is read as a boolean, and the loader rejects it with a
hint.

## Standing

Standing is computed, never claimed. `inv status` and `inv.standing(rule_id)` report one of:

| Standing | When |
|---|---|
| `held` | enforced, `mediation.chokepoint` is named, and `inv verify` and `inv scan` pass |
| `enforce(known-callers)` | enforced, but one of the conditions for `held` is missing; the reasons are listed |
| `monitored` | the rule runs, but no enforcer is in effective mode `enforce` |
| `candidate` | status `candidate` or `contested`, or every enforcer is `off` |
| `retired` | status `retired`: kept for the record, every enforcer off |

`inv status` explains, per rule, why it is not held. The usual path from
`enforce(known-callers)` to `held`:

1. **Name the chokepoint.** `mediation.chokepoint` states the positive argument in words
   ("pools.size is written only by resize_door").
2. **Declare what the scan checks.** `mediation.sink` is the protected effect (for example the
   repository method that writes), `mediation.door` is the guarded function or functions
   every path must pass (by default, the rule's target), and `mediation.allow` lists the back doors
   that were reviewed. A rule on an `effects: none` target needs no sink: the guard wraps the
   function itself. See [scan.md](scan.md).
3. **Pass verify.** `inv verify --import your.app` (or `--inv-verify` in pytest) checks that every
   target is wrapped by its guard, and that every guard names a rule whose target it is.
4. **Prove it is load-bearing.** `pytest --inv-ablate` must report every `enforce` rule as caught.

While a rule lists any `mediation.residual_paths`, `inv status` withholds `held`.

## Providers

A provider is a typed, read-only function that gives a rule data the call does not carry (an
approval, a job record). Mark it with `@inv.provider("name")` and declare it under `providers:`.
The loader checks its name, its parameters (exactly the keys of `args`) and its return type.

**When providers are read.** A provider is a plain read at its enforcer's timing: a `pre_effect`
enforcer reads it before the call, a `post_effect` enforcer after it. Values are cached per
provider and resolved arguments, per call. A provider that raises or returns the wrong type is a
`rule_error`.

**A check is not a lock.** A rule on an API's arguments is checked per call. Two concurrent callers
can both pass a check that reads shared state, and both proceed. Keeping concurrent writes
consistent (the store's own constraints, its transactions) is the implementation's business.

**Providers are eager.** Every provider an enforcer's names map reads is called, even when a clause
would short-circuit without it. A sync provider called from an async guard runs on the event loop.
An async provider is awaited by async guards, model calls and edges. A rule whose sync target reads
an `async def` provider is a `LoadError`, since a sync wrapper cannot await it.

## Views

One rule can have several enforcers, and each may bind the names `bad` reads its own way.
`view: {leased: provider.pool_leased, n: args.slots}` binds names for a door. `bind:` sets the names for the rule as a whole. When `bind:` is
omitted, `args`, `result` (or `reply`, for a model call), `self` and `old_self` are bound
automatically, and so is any provider name that `bad` references.

## Transition rules: `old_self`

A rule that compares before and after reads `old_self.<path>`:

```yaml
scope: transition
bad: 'self.prices[args.sku] * 2 > old_self.prices[args.sku] * 3'
```

The loader finds every `old_self.<path>` in `bad` and deep-copies only those paths before the call,
so a database connection on `self` is never touched. Each path must be plain data (numbers,
strings, lists, maps, and dataclasses of those). That check needs a real instance, so it runs at
test time with `inv.check_snapshot(rule_id, instance)` and fails like a load error. A non-plain path
reached at run time is a `rule_error`, and nothing is copied. Bare `old_self` is a load error.

## Model calls

```python
class CauseReply(BaseModel):
    model_config = ConfigDict(extra="forbid")      # a closed schema is required
    cause: Literal["OUT_OF_MEMORY", "TIMEOUT", "CANNOT_COMPLY"]
    message: str

class CheckedCause(inv.Checked): ...

@inv.model_call("R-7", schema=CauseReply)          # parse -> abstain -> bad, with bounded retries
def propose_cause(log_excerpt: str, job_id: str, inv_feedback: str | None = None) -> CheckedCause:
    return call_model(log_excerpt, inv_feedback)   # the raw reply; inv_feedback carries the parser error
```

The rule's `model_policy` sets `schema`, `format_retries`, `semantic_resamples`, `feedback` and
`abstain`. A reply is handled the same way for every schema kind (a dataclass, a `{field: type}`
map, or a pydantic model with `extra='forbid'`):

- **Parse.** The reply (JSON text, bytes, or an already-decoded object) must be a JSON object with
  exactly the schema's fields and types. Field types may be plain classes, `Any`, `Literal`,
  `Optional` or unions, `list[T]`, `dict[str, T]`, or a nested dataclass; any other type is a
  `TypeError` when the function is decorated. A `datetime` field takes RFC 3339 text with a timezone
  (`"2024-01-31T09:30:00Z"`; without one it is a `parse_error`). A `timedelta` field takes CEL
  duration text (`"90s"`, `"1h30m"`) or ISO 8601 (`"PT1H30M"`, as pydantic writes it), and a `UUID`
  field takes its text. The parsed reply holds the `datetime`, `timedelta` and `UUID` values, so
  rules read timestamps, durations and strings.
- **Every failure is a `parse_error`.** That covers invalid UTF-8, non-object JSON, non-string keys,
  nesting deep enough to exhaust the stack, objects whose `__eq__`, `__hash__` or `__repr__` raise,
  a schema `__post_init__` that raises, a reply longer than `max_reply_size` (1,000,000 characters
  by default), a `NaN`, `Infinity` or `-Infinity` token, a number that overflows to infinity like
  `1e999`, and a non-finite float anywhere in an already-decoded reply, `Any` fields included. A
  parse error is retried up to `format_retries` times, then the enforcer's `on_violation` applies.
  No other exception leaves `model_call` for a reply. An exception that your own function raises is
  yours, and it propagates unchanged.
- **Pydantic schemas are strict.** A pydantic reply is validated in pydantic's strict mode over its
  JSON form (`model_validate_json(strict=True)`), so it gets the same types a dataclass schema does:
  `"0.5"`, `true` and `"NaN"` are not floats, `"1"` is not an int, and an enum or `Literal` field
  takes its plain value. After validation, the instance is walked, and a NaN or ±Infinity anywhere in
  it (nested models, `list[float]`, `dict[str, float]` values) is a `parse_error`. A model whose
  `model_config` sets `strict=False` explicitly opts out and gets pydantic's lax coercion (`"0.5"`
  becomes `0.5`). The non-finite walk still applies, so `"NaN"` is refused there too. A pydantic
  reply must include every field, even fields that have defaults.
- **What a rule reads from a pydantic reply.** The reply's attributes (`c.at`, `c.cause`) keep its
  JSON form (an enum's value, a datetime's text). `integrations.pydantic.model_of(c)` is the typed
  view: on every call it validates those attributes again (strictly, as at parse) and returns a new
  model instance, so it always matches the JSON form an edge judges, and changing it changes neither
  the reply nor what any other caller gets (a tampered reply raises `ParseError`). Keep the result
  in a local rather than calling it in a loop. Rules read the validated instance through the same
  mapping as arguments ([cel.md](cel.md#python-to-cel-types)): a `datetime` field is a timestamp, a
  `timedelta` a duration, a `UUID` a string, an `Enum` its value, and nested models, lists and dicts
  likewise, on `model_call`, at an edge and on a copied or unpickled `Checked`. Reply clauses are
  type-checked against the model's annotations exactly as `args` are, so `reply.at == 'x'` with
  `at: datetime`, and any read of a `NaiveDatetime`, `Decimal` or `date` field, are `LoadError`s.
  Only the model's fields, computed fields and your own properties are fields: pydantic's
  `BaseModel` API (`model_fields`, `model_config`, `copy`, `json`, ...) is not, so
  `reply.model_fields` is a `LoadError` that lists the real fields, for a pydantic argument too.
- **Parse-error text.** `str(ParseError)` (and so the exception message, `Outcome.error`, the
  attempt record, the event and the log line) names the field and the kind of error, never a reply
  value or a key the reply made up ("field cause: string is not one of the allowed values"; extra
  keys and `dict[str, T]` keys show as `*`; pydantic errors show as `loc: error_type`). The full
  text, with the value, is `ParseError.feedback`. That is what the model gets as `inv_feedback`, and
  what outcomes and logs show with `inv.configure(debug=True)`.
- **Abstain.** When `model_policy.abstain.field` equals `abstain.value` (the same plain type: `True`
  is not `1`), the call raises `Escalation` and opens a review before `bad` runs.
- **Bad.** Then `bad` runs, and on a violation `on_violation` applies, after up to
  `semantic_resamples` fresh resamples. `compares_to: held_data` forces `semantic_resamples: 0`.
- **Typing.** `@inv.model_call` keeps the declared return type: `propose_cause(...)` is a
  `CheckedCause` to mypy, not `Any`.

## Edges

```python
@inv.edge("R-7")                                   # checks .verdict, re-validates the schema, re-evaluates bad
def remediate(cause: CheckedCause, job_id: str): ...
```

An `@inv.edge` takes only a verdict-ok `Checked`. It re-validates the value against the same
closed schema (an extra or missing field, an out-of-enum value, a wrong type, a value of another
class, a cyclic value or a NaN is a `parse_error`), and it re-evaluates `bad` on the validated copy.
The consumer is then called with a new `Checked` over that copy (equal to the one it was passed,
`==` but not `is`): the exact value the rule judged. So a received value that other code mutates or
swaps after the model call never reaches the consumer, through its attributes or through
`model_of()`.

The edge enforcer belongs on the model call's rule (the one with `model_policy`), since that is
where the schema comes from. An edge enforcer on a rule without `model_policy` is a `LoadError`.

**Forgeable values.** Python cannot make `inv.Checked` unforgeable. The type is a convenience, and
the edge is what checks: it re-validates and re-checks every value it receives, then hands its
consumer the copy it checked. An enforcing edge refuses (by `on_violation`) any value whose verdict
is not ok, whatever its `on_error`. A forged value that fits the schema and passes `bad` gets
through: that is all the edge can check.

**Copies.** `copy.copy` of a `Checked` returns it (it is immutable). `copy.deepcopy` and pickle give
an equal `Checked` over a copy of the value. A copy carries no extra trust: an edge re-validates it
like any other. For edge tests, `invariants.testing.make_checked(cls, value, verdict=...)` builds a
`Checked`.

## Async

`model_call`, `edge` and `guard` also work on `async def` functions: replies and async providers
are awaited. A generator function cannot be
guarded, because its body runs after the guard returns.

## Ablation

Ablation turns a rule off to prove that its falsifiers then fail.

- `inv.ablated(rule_id)` is context-local: the rule is off only in this thread or task (and in tasks
  started inside the block, which copy the context). `scope="registry"` turns it off for every thread
  and task that uses the registry, for a falsifier that calls the guard from worker threads.
  Overlapping registry-scope blocks (two threads, or nested blocks) are counted, so the rule stays off
  until the last one exits.
- `invariants.registry.ablated_everywhere(rule_id)` turns a rule off in every registry and every
  thread. The pytest plugin's `--inv-ablate` uses it, with `INV_ABLATE` for subprocesses.
- **What `--inv-ablate` counts as caught.** A falsifier that still passes with the rule off fails the
  session with "guard not load-bearing". An `enforce` rule with no caught falsifier (not collected,
  failing with the guard on, or inconclusive) also fails it. A failure counts as caught only when the
  ablation caused it: the falsifier must pass on a control re-run without ablation, and the ablated
  re-run must reach the rule (a guard, `inv.is_ablated` or `inv scan` acted on the ablation, in
  process or in a subprocess through `INV_ABLATE_TRACE`). Otherwise it is `inconclusive`. It is also
  `inconclusive (rule_error, not violation)` when, on the control re-run, the rule's in-process
  evaluations gave `rule_error` and never `violation` or `parse_error`: the falsifier passed because
  the rule could not be evaluated, not because it caught the bad state.
- **Schema-held rules.** Ablating a rule enforced by `point: type` (a closed `Literal`, or
  `extra="forbid"`) does not relax `inv.parse`, so a parse-error falsifier still passes and
  `--inv-ablate` reports the guard as not load-bearing. Give such a rule an in-process enforcer too,
  or leave it below `enforce`.
- **Environment ablation.** `INV_ABLATE` is honoured only with `INV_ABLATE_TESTING=1`, which the
  pytest plugin sets for falsifier runs and their subprocesses. Set both in a production process and
  those rules' guards are off. The loader logs this at ERROR outside a pytest session (and on stderr
  when no logging is configured), but does not refuse, because an `inv scan` or app subprocess of a
  falsifier must see it. See [SECURITY.md](../SECURITY.md).
- A rule ablated through `INV_ABLATE` is not scanned, so a scan inside a falsifier is ablated too.
- The plugin does not support pytest-xdist.

## Operations

```python
inv.configure(
    review_store=MyReviews(),            # route and escalate (and abstain) call .open(Handoff) -> review id
    pager=page_owner,                    # fail_open_and_page and rule-error spikes call it with a Handoff
    exporter=otel.exporter(),            # invariants.integrations.otel: a span per evaluation (the otel extra)
    error_spike=inv.ErrorSpike(errors=20, window=60),   # demote a rule to detect on a rule-error spike
    violation_log_level=logging.INFO,    # violations log at WARNING by default; ok at DEBUG
    sink_size=1000,                      # recent events kept in Registry.sink (0 keeps none)
    review_fallback=inv.JsonlReviews("reviews-fallback.jsonl"),  # tried when review_store fails
    max_reply_size=1_000_000,            # longest model reply text inv.parse accepts (0: no limit)
    debug=False,                         # True (or INV_DEBUG=1) puts user exception messages in errors
    allow_non_finite=False,              # True lets rules read NaN/Infinity as numbers (cel.md)
)
```

- **Startup.** `inv.load("invariants.yaml")` validates the file, resolves every target and raises
  `inv.LoadError` on any defect. `inv.verify()` checks that every target is guarded and every guard
  names a rule, and raises `inv.CoverageError`. `inv.load_resource("my_pkg")` loads a rule file
  shipped inside a package.
- **Guards never run open.** A guarded call raises `LoadError` when a rule-id key is not loaded, or
  when a `py:` key matches no loaded rule and no `targets:` entry.
- **Rule reload.** `inv.load` into the registry a call is using swaps its rules and providers
  atomically. A guarded call already running keeps the rules and providers it started with, through
  its post-effect check.
- **When no review can be opened.** If the review store raises or returns no id, the
  `review_fallback` store is tried (`inv.JsonlReviews(path)` appends each hand-off as one JSON line
  and fsyncs it). If that fails too, or none is configured, route, escalate and abstain raise
  `inv.ReviewNotOpened` instead of `Routed` or `Escalation`. The call still does not proceed. The
  exception carries `.handoff` (the work), `.action`, and the store's error as `__cause__`, and the
  HTTP mapping answers it with 503. Catch it next to `Routed` and `Escalation`.
- **Hand-offs.** A `Handoff` carries the rule, the action, the outcome, the attempts and `bound`
  (the values the rule read). The default store, `inv.MemoryReviews`, is in memory and bounded: give
  a durable one in production.
- **Redaction.** An exception raised by your code while a rule reads a value (a property, a mapping
  lookup, a provider) appears in `rule_error` text, events and logs as its type only ("reading
  'boom' raised RuntimeError"). Its message, which may hold secrets, is added only with `debug=True`
  or `INV_DEBUG=1`. CEL evaluation errors are reported the same way, as their kind ("no such key",
  "invalid regular expression", "index out of range", "conversion failed" and so on), never the
  missing key, the pattern or the value from the request (a field or key name written in the rule
  text is still named). The engine's own text is added only with debug. One thing the library cannot
  redact: on `cel-expr`, the C++ RE2 library prints a bad dynamic pattern (`args.s.matches(args.p)`
  with a request-supplied `p`) straight to the process's stderr, and absl may print a one-line
  logging banner there. Do not build patterns from request data on `cel-expr`, or treat that
  process's stderr as holding request data.
- **Events.** `Registry.sink` is a ring buffer (1000 events by default, `INV_SINK_SIZE`). The reach
  counters are separate and exact. In tests, use `inv.capture()` or the pytest fixture `inv_events`.
  `inv.observe(fn)` sees every evaluation in every registry. An observer, exporter, store or pager
  that raises is logged, and never reaches the guarded call.
- **Error spikes.** A spike sets `Registry.demoted[rule_id]`, and the rule's effective mode stays
  `detect` until `Registry.restore(rule_id)`. It is off unless configured, on purpose: when request
  data can cause rule errors (a missing field, an oversized input past the cost budget), a default
  demotion would let a client switch an enforcing rule to detect by sending bad requests, and then
  pass. Turn
  it on for rules whose errors only your own code can cause.
- **Monitors.** `inv.monitor(rule_id, rows)` evaluates a rule's `monitor` enforcer over
  `(key, base)` rows and records each outcome. It never blocks.
