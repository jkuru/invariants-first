# Rule file reference

A rule file is JSON or YAML. `inv validate` checks it against the bundled JSON Schema
(`inv.schema_path()`) and runs every load-time check. Unknown fields are load errors everywhere.
The design behind each field is in [the design guide](design-guide.md), sections 2.1 to 2.9 and 4.1.

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

- `allow` lists functions that already call a sink without a door. The scan lets them, and everything
  inside them, through, so the code as it is passes and a new back door fails. An entry that no
  longer reaches a sink without a door fails the check (`missing/allow-unused`) until it is deleted,
  and so does an entry that names no function (`missing/allow`). An empty or null `allow` means none.
- With `guard:` as well, the guarded function is the door, and the rule also checks its arguments
  (give `bad`, and optionally `tests`). A door rule without `guard:` cannot have `bad`.
- `tables` (optional, below the API) also forbids raw SQL writes to those tables outside the sinks
  and doors; see [scan.md](scan.md#tables-and-raw-sql).
- The lock reports these as weakenings: a new `allow` entry, a new door, a removed sink or table, and
  removing the rule. Deleting an `allow` entry or a door is a tightening.

`inv init` drafts door rules from the code: see [start.md](start.md#3-path-laws-doors-and-back-doors).

## Top level

| Field | Meaning |
|---|---|
| `spec` | `invariant/v0.1`. |
| `contracts` | Named contracts: `{type: python \| pydantic \| dataclass \| schema \| openapi \| sql \| ..., ref: ...}`. A `pydantic` contract pins the model's constraints and a `sql` contract a schema file's constraints, and loosening either is a weakening ([lockfile.md](lockfile.md#weakening)). `inv lock` pins each one at contract granularity: see [lockfile.md](lockfile.md). |
| `targets` | `"py:pkg.mod.fn": {effects: none \| transactional \| external, transaction: "py:..."}`. Effects belong to the target, once. `transactional` must name an `@inv.transaction` boundary. An unlisted target is `external`. |
| `providers` | `name: {type, from, args, consistency}`. See below. |
| `rules` | The list of rules. |

## Providers

| Field | Meaning |
|---|---|
| `type` | `int`, `str`, `bool`, `float`, `list`, `dict` or `py:mod.Class`, optionally `\| null`. The function's return annotation must match. |
| `from` | `py:` path of a function marked `@inv.provider("name")` (sync or `async def`). Its parameters must be exactly the keys of `args`. |
| `args` | Parameter -> binding path (`args.x`, `self`, `reply.y`, or a bound name). |
| `consistency` | `same_txn`: read inside the target's transaction. `snapshot`: read once before the effect. `stale_ok`: any recent value. `immutable`: never changes. Only `same_txn` and `immutable` can make a rule held without a storage enforcer. |

**When providers are read.** A `pre_effect` enforcer reads its providers before the call. A
`post_effect` enforcer reads them after the effect (inside the transaction for a transactional
target), except `snapshot` providers, which are read before the effect. A provider that raises or
returns the wrong type is a `rule_error`; its exception message appears only with
`inv.configure(debug=True)` or `INV_DEBUG=1`. An async provider is awaited by async guards, model
calls and edges; a sync call that needs one gets a `rule_error`.

## Rules

| Field | Meaning |
|---|---|
| `id`, `rev` | Identity and revision. |
| `status` | Set by human review: `candidate`, `contested`, `detect`, `enforce`, `retired`. The loader computes the highest status the evidence allows (`Rule.max_status`) and refuses a higher one. `enforce` needs an enforcer with mode `enforce`, `falsified_by`, and a passing timing check. |
| `class`, `owner`, `statement`, `why` | Documentation. `statement` is the default clause message. |
| `subject` | `{target: "py:...", contract: name, operation: ..., table: ...}`. |
| `scope` | `call`, `transition`, `aggregate(key: ...)`: the scope of the property. |
| `bind` | Name -> path, for names `bad` reads. A name that matches a provider is bound to it automatically. |
| `bad` | A CEL string, or a list of `{when, message}` clauses. Any clause true is a violation; else any clause error is a rule_error; else ok. `old_self.<path>` makes a transition rule: the loader snapshots only those paths, which must be plain data. Each clause is type-checked at load against the target annotations, the provider types and the model-call schema (see [cel.md](cel.md#the-load-time-type-check) for its scope); an operation with no overload for the declared types is a load error, and so is a call to a function the chosen CEL backend does not have ([cel.md](cel.md#functions-per-backend) lists them per backend). So is a known function called with the wrong number of arguments, or a macro in global form (`exists(xs, x, p)`). A clause that reads a NaN or ±Infinity, or makes one (`0.0 / 0.0`, an overflow, `double("NaN")`), is a rule_error on every backend unless `inv.configure(allow_non_finite=True)`. Values are read by the mapping in "Python to CEL types" below. On `cel-expr` a clause nested more than 32 levels deep (cel-cpp's fixed limit) is a load error that says so. |
| `enforcers` | See below. |
| `mediation` | `{chokepoint, residual_paths, sink, door, allow, tables}`. `allow` lists known back doors the scan lets through (see "The door form" above). `chokepoint` is the positive mediation argument. `sink`, `door` and `tables` drive `inv scan`. A `tables` item is a table name (every raw SQL write to it outside the sink and door is forbidden) or a one-key mapping `{table: [insert, update, delete, ddl]}` that forbids only those statement kinds. A rule gets a scan warning, and is not reported `held`, in two cases. One is when its sink's own SQL writes a table, or a statement kind, that `tables` does not cover, whatever the sink's `effects` are. The other is when its door guards stored data in process (a `transactional` door or sink, or a storage enforcer) and it declares no `tables`. SQL in a helper or module-level constant that only door or sink code reaches counts as door code (call-graph reachability). |
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
| `point` | `decorator` and `middleware` (`@inv.guard`), `model_call`, `edge` (needs `target`, and the rule must have `model_policy`: the edge re-checks that model call's schema), `monitor` (`inv.monitor`), `storage` (a CHECK, trigger or UNIQUE; map its error with `inv.storage_errors`), `structure` (`inv scan`), `type`, `gateway`, `build`. |
| `timing` | `pre_effect` or `post_effect`. A post-effect enforcer that enforces needs `effects: none` or `transactional`. |
| `mode` | `off`, `detect`, `enforce` (quote `"off"` in YAML). Effective mode is `min(mode, cap(status))`. |
| `on_violation` | `reject`, `halt`, `escalate`, `route` (default `reject`). |
| `on_error` | `fail_closed` (applies `on_violation`) or `fail_open_and_page` (proceeds and pages). Detect never blocks. |
| `view` | Name -> path for this enforcer (in-process points and `monitor`), or `rows` for storage. |
| `artifact`, `conformance_test`, `generated` | For hand-written enforcers: `FILE#sha256:...` is checked at load; `inv lock --update-artifacts` rewrites a stale digest. |

## The lockfile

`inv lock` writes `invariants.lock` beside the rule file (lock version 3). It pins what each rule's
meaning depends on, and it refuses a change that weakens a rule unless `--accept-weakening` is
given. The format, the policy for every field of this schema (`lockfile.FIELD_POLICY`), how `bad`
edits are compared, and migration from older versions are in [lockfile.md](lockfile.md).
