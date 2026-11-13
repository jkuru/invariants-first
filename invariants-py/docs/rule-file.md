# Rule file reference

A rule file is JSON or YAML. `inv validate` checks it against the bundled JSON Schema
(`inv.schema_path()`) and runs every load-time check. Unknown fields are load errors everywhere. A field that an earlier release had and this one removed
(`consistency`, `transaction`, `tables`, `effects: transactional`, `point: storage`, a `{type: sql}`
contract) is a load error that says what to do instead.
How the fields fit together is in [Invariant-first](design-guide.md).

## The short form

A rule that guards one function can be written in three or four lines:

```yaml
rules:
  - id: PAY-1
    guard: py:payments.refund
    bad: 'args.refunded + args.amount > args.charged'
    tests: [tests/test_payments.py::test_over_refund_is_rejected]
```

`guard` expands to `subject: {target: ...}` and one enforcer
`{point: decorator, timing: pre_effect, mode: enforce}`. `tests` is `falsified_by`. With no `status`,
the rule is `enforce` when it names tests and `detect` (it logs but does not block) when it does not.
The expansion happens when the file is read (`loader.expand_short_forms`), so the loader, the lock and
the scan see only the full form: switching a rule between the two forms is not a change, and deleting
`tests:` lowers the status, which is a weakening. A short rule cannot also give `subject.target` or
`enforcers`, and cannot give both `tests` and `falsified_by`. `rules:` may be empty (null).

### The door form: a path law

A rule that says which API may call which is four lines, and needs no decorator:

```yaml
rules:
  - id: REFUND-GATEWAY-DOOR
    door: py:billing.service.BillingService.refund      # the upper API: a string or a list
    sink: py:billing.gateway.RefundGateway.refund       # the lower API it protects: a string or a list
    allow: [py:scripts.admin.goodwill_refund]           # known back doors, frozen (optional)
```

It expands to `mediation: {sink, door, allow}`, `bad: 'false'`, one enforcer
`{point: structure, timing: pre_effect, mode: enforce}`, and `status: enforce`. Nothing is evaluated at
run time: `inv scan`, inside `inv check`, fails any call path from an entrypoint to a sink that passes
no door, and any `getattr`, `__wrapped__` or monkeypatch of a sink or door. A structure rule may be
`enforce` without `falsified_by`, because its check is the scan itself.

- `allow` lists functions that already call a sink without a door: the call chain as it was when
  `allow` was written. An allowed back door is not a door. A path to the sink is let through only
  when every function on it is in `allow`, so a new caller of an allowed function, or new code an
  allowed function calls that reaches the sink, is a `bypass`. Inside an allowed function, the ways
  it reaches the sink (a direct call, `getattr` of the sink name, the sink used as a value) are
  excused, but `forbidden/unwrap`, `forbidden/monkeypatch` and `unresolved/reflection` are still
  reported. An entry that no longer reaches a sink without a door fails the check
  (`missing/allow-unused`) until it is deleted, and so does an entry that names no function
  (`missing/allow`). An empty or null `allow` means none.
- With `guard:` as well, the guarded function is the door, and the rule also checks its arguments
  (give `bad`, and optionally `tests`). A door rule without `guard:` cannot have `bad`.
- The lock reports these as weakenings: a new `allow` entry, a new door, a removed sink, and
  removing the rule. Deleting an `allow` entry or a door is a tightening.

`inv init` drafts door rules from the code: see [start.md](start.md#3-path-laws-doors-and-back-doors).

## Top level

| Field | Meaning |
|---|---|
| `spec` | `invariant/v0.1`. |
| `contracts` | Named contracts: `{type: python \| pydantic \| dataclass \| schema \| openapi \| ..., ref: ...}`, or `{type: openapi, app: "module:app"}` to read the OpenAPI document of a live app (`app.openapi()`, as a FastAPI app has). A `pydantic`, `dataclass` or TypedDict contract pins its JSON Schema's constraints, and loosening one is a weakening ([lockfile.md](lockfile.md#weakening)); removing a contract is one too. `inv lock` pins each one at contract granularity: see [lockfile.md](lockfile.md). |
| `targets` | `"py:pkg.mod.fn": {effects: none \| external}`. Effects belong to the target, once. An unlisted target is `external`. |
| `providers` | `name: {type, from, args}`. See below. |
| `rules` | The list of rules. |

## Providers

| Field | Meaning |
|---|---|
| `type` | `int`, `str`, `bool`, `float`, `list`, `dict` or `py:mod.Class`, optionally `\| null`. The function's return annotation must match. |
| `from` | `py:` path of a function marked `@inv.provider("name")` (sync or `async def`). Its parameters must be exactly the keys of `args`. |
| `args` | Parameter -> binding path (`args.x`, `self`, `reply.y`, or a bound name). |
| `concurrent` | Optional, `true` or `false`. `true` lets this async provider be awaited together with other async providers that opted in (`asyncio.gather`). By default async providers run one at a time, in the order they are declared here, which is what providers sharing one `AsyncSession` or connection need. When the record says nothing, `@inv.provider("name", concurrent=True)` on the function opts in. A change is recorded by `inv lock`, never as a weakening. |

**When providers are read.** A provider is a plain typed read at its enforcer's timing: a
`pre_effect` enforcer reads it before the call, a `post_effect` enforcer after it. A provider that raises or
returns the wrong type is a `rule_error`; its exception message appears only with
`inv.configure(debug=True)` or `INV_DEBUG=1`. An async provider is awaited by async guards, model
calls and edges; a sync call that needs one gets a `rule_error`. A check that reads a provider is
not a lock: two concurrent callers can both pass it.

## Rules

| Field | Meaning |
|---|---|
| `id`, `rev` | Identity and revision. |
| `status` | Set by human review: `candidate`, `contested`, `detect`, `enforce`, `retired`. The loader computes the highest status the evidence allows (`Rule.max_status`) and refuses a higher one. `enforce` needs an enforcer with mode `enforce`, `falsified_by`, and a passing timing check. |
| `class`, `owner`, `statement`, `why` | Documentation. `statement` is the default clause message. |
| `subject` | `{target: "py:...", contract: name, operation: ...}`. |
| `scope` | `call`, `transition`, `aggregate(key: ...)`: the scope of the property. |
| `bind` | Name -> path, for names `bad` reads. A name that matches a provider is bound to it automatically. |
| `bad` | A CEL string, or a list of `{when, message}` clauses. Any clause true is a violation; else any clause error is a rule_error; else ok. `old_self.<path>` makes a transition rule: the loader snapshots only those paths, which must be plain data. Each clause is type-checked at load against the target annotations, the provider types and the model-call schema (see [cel.md](cel.md#the-load-time-type-check) for its scope); an operation with no overload for the declared types is a load error, and so is a call to a function the chosen CEL backend does not have ([cel.md](cel.md#functions-per-backend) lists them per backend). So is a known function called with the wrong number of arguments, or a macro in global form (`exists(xs, x, p)`). A clause that reads a NaN or ±Infinity, or makes one (`0.0 / 0.0`, an overflow, `double("NaN")`), is a rule_error on every backend unless `inv.configure(allow_non_finite=True)`. Values are read by the mapping in "Python to CEL types" below. On `cel-expr` a clause nested more than 32 levels deep (cel-cpp's fixed limit) is a load error that says so. |
| `enforcers` | See below. |
| `mediation` | `{chokepoint, residual_paths, sink, door, allow}`. `allow` lists known back doors the scan lets through (see "The door form" above). `chokepoint` is the positive mediation argument. `sink` and `door` drive `inv scan`. |
| `model_policy` | For a `model_call` enforcer: `{schema, format_retries, semantic_resamples, feedback: {format: parser_error \| none, semantic: none}, abstain: {field, value}}`. |
| `compares_to` | `held_data`: forces `semantic_resamples: 0`. |
| `falsified_by` | pytest node ids (or prefixes) of tests that must fail when the rule is ablated. `pytest --inv-ablate` checks them. |
| `applies_to` | fnmatch pattern(s) over `targets:` keys; `inv lock` expands them. |
| `evidence` | Computed; not hashed. |

## Python to CEL types

`bad` reads Python values through one mapping, the same on every backend and in the load-time type
checker (`invariants/cel/values.py`; [cel.md](cel.md#python-to-cel-types) has the details):

| Python value | CEL value |
|---|---|
| `None` | `null` (`x == null` and `has(r.x)` work for an `Optional` field) |
| `bool`, `int` (int64), `float` (finite), `str`, `bytes` | `bool`, `int`, `double`, `string`, `bytes` |
| `Enum` member | its value |
| `uuid.UUID` | `string` (canonical lower-case form) |
| `datetime` with a `tzinfo` | `timestamp` (the same instant, in UTC) |
| `timedelta` | `duration` |
| `list`, `tuple` / `dict`, `Mapping` / dataclass, pydantic model, object | list / map / map of public fields |
| naive `datetime` | refused: no timezone is ever assumed |
| `datetime.date`, `datetime.time` | refused: pass an aware `datetime` or an ISO string; seconds or `"HH:MM"` |
| `decimal.Decimal`, `Fraction`, `complex` | refused: CEL has no decimal and a double loses money precision; pass int minor units or a `str` |
| `set`, `frozenset` | refused: pass a list |

A clause that reads a refused type is a load error when the annotation shows it (on the value, or
inside a record, list or map the clause reads whole), else a rule_error naming the type. Only what
a clause reads is converted: a comprehension reads just the element fields its body names
(`args.items.exists(i, i.qty < 0)` never reads `price`) and `size()` only the length. The type
checker treats `datetime` as timestamp, `timedelta` as duration and `UUID` as string, so
`args.deadline < 5` or `args.wait * 2` is a load error, and so is a constant `timestamp('...')` or
`duration('...')` that does not parse, or a constant timezone that is not one (`getHours('+25:00')`).
`int` and `float` are CEL `int` and `double`: they compare with each other, but arithmetic does not
mix them (CEL has no implicit conversion), so `args.price * args.qty` with `price: float`, `qty: int`
is a load error; write `args.price * double(args.qty)`. `%` takes ints only.

A model-call reply is typed by its schema: a pydantic schema by the model's annotations (rules read
the validated instance, so a `datetime` field is a timestamp), a dataclass or `{field: type}` schema
by its fields (a `datetime` field parses RFC 3339 text with a timezone, a `timedelta` field `"90s"`
or ISO 8601 `"PT90S"`, a `UUID` field its text).

## Enforcers

| Field | Meaning |
|---|---|
| `point` | `decorator` and `middleware` (`@inv.guard`), `model_call`, `edge` (needs `target`, and the rule must have `model_policy`: the edge re-checks that model call's schema), `monitor` (`inv.monitor`), `structure` (`inv scan`), `type`, `gateway`, `build`. |
| `timing` | `pre_effect` or `post_effect`. A post-effect enforcer that enforces needs `effects: none`; on an `external` target it can only detect. |
| `mode` | `off`, `detect`, `enforce` (quote `"off"` in YAML). Effective mode is `min(mode, cap(status))`. |
| `on_violation` | `reject`, `halt`, `escalate`, `route` (default `reject`). |
| `on_error` | `fail_closed` (applies `on_violation`) or `fail_open_and_page` (proceeds and pages). Detect never blocks. |
| `view` | Name -> path for this enforcer (in-process points and `monitor`). |
| `artifact`, `conformance_test`, `generated` | For hand-written enforcers: `FILE#sha256:...` is checked at load; `inv lock --update-artifacts` rewrites a stale digest. |

## The lockfile

`inv lock` writes `invariants.lock` beside the rule file (lock version 3). It pins what each rule's
meaning depends on, and it refuses a change that weakens a rule unless `--accept-weakening` is
given. The format, the policy for every field of this schema (`invariants.lock.FIELD_POLICY`), and how `bad`
edits are compared are in [lockfile.md](lockfile.md).
