# Rule file reference

A rule file is JSON or YAML. `inv validate` checks it against the bundled JSON Schema
(`inv.schema_path()`) and runs every load-time check. Unknown fields are load errors everywhere.
YAML anchors and aliases (`&name`, `*name`, `<<: *name`) are refused, with the line they are on: a
few nested aliases stand for billions of values, and a rule file is written out in full. `inv lock`
pins only a file `inv validate` accepts. A key repeated in one YAML mapping is refused with both
lines, compared as YAML reads it: `1` and `01`, `true` and `yes`, `null` and `~`, `1.0` and `1.00`
are one key (YAML would keep only the last).
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

It expands to `mediation: {sink, door, allow, allow_unresolved}`, `bad: 'false'`, one enforcer
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
- In the list form any door may reach any sink. When each write has its own door, `sink:` (and
  `mediation.sink`) may instead map each sink to the door or doors that may reach it:

  ```yaml
  - id: LEDGER-REPO-DOOR
    sink:
      py:support_agent.repo.LedgerRepo.record_refund: [py:support_agent.service.BillingService.refund]
      py:support_agent.repo.LedgerRepo.insert_credit: [py:support_agent.service.BillingService.credit]
      py:support_agent.repo.LedgerRepo.cancel_subscription: py:support_agent.service.BillingService.cancel
  ```

  A door listed for one sink is no door for another: a refund record written inside
  `BillingService.cancel` is a `bypass` of `record_refund` (the list form lets it through). Each sink
  is checked against its own doors; a door may be listed for several sinks. `door:` is then left out,
  or names exactly the doors the mapping lists (any other value is a scan error, which `inv validate`
  reports); `subject.target` is no default door here, and `guard:` still makes the rule check the
  guarded function's arguments. A finding two sinks' checks give alike (a parse error, one unresolved
  site) is reported once. The mapping needs at least one sink, each a `py:` path mapped to a `py:` path
  or a non-empty list of them.

  With a mapping, `allow` (and `mediation.allow`) maps each allowed function to the sink or sinks it
  may reach without a door, a `py:` path or a non-empty list of them, each a sink of the mapping:

  ```yaml
    allow:
      py:scripts.admin.fix: [py:support_agent.repo.LedgerRepo.record_refund]
  ```

  An entry excuses only the sinks it lists: a path from `scripts.admin.fix` to `insert_credit` is a
  `bypass`, so a back door frozen for one write cannot start writing another. A plain list there is
  a load error that shows the map form. An entry, or one of its sinks, that no longer reaches that
  sink without a door is `missing/allow-unused` naming the sink. `allow_unresolved` stays rule-wide.
  List-form rules keep the plain list.
- With `guard:` as well, the guarded function is the door, and the rule also checks its arguments
  (give `bad`, and optionally `tests`). The rule then expands as the `guard:` form does: one
  decorator enforcer instead of the structure one, and `status` `enforce` with `tests` or `detect`
  without them (the scan checks the path law either way). A door rule without `guard:` cannot have
  `bad`.
- `allow_unresolved` (short for `mediation.allow_unresolved`) accepts unresolved findings a person
  has reviewed, one site each: `{at: "app/plugins.py:41", code: unresolved/reflection, sha:
  3f9c0a17be52d4e1, why: "..."}`, with the sha (16 hex digits) `inv scan --format json` gives the
  finding: a hash of its code, its function and its whole statement (and, for an inferred path, the
  entrypoints and name-matched calls that reach it). A matching finding is shown as accepted and does
  not fail the rule; an edit above it keeps the entry (reported as moved), an edit of any line of its
  statement does not, nor does a new caller of an inferred path, and an entry that matches nothing is
  `missing/allow-unresolved-unused`. Only
  `unresolved/*` codes can be accepted, never a `bypass` or `forbidden` finding (see
  [scan.md](scan.md#accepting-a-reviewed-unresolved-site)). In a door rule it may also be written
  as `mediation: {allow_unresolved: [...]}`, but not in both places; an entry error names it as
  written (`allow_unresolved[0]`). `inv check`'s hint names the spelling of the failing rule's form.
  `door` and `allow` beside a top-level `sink:` go at the top too, not under `mediation`.
- One reviewed site several path laws see (a plugin loader every rule reports as
  `unresolved/reflection`) may be accepted once for all of them, in the file's top-level
  `allow_unresolved` (see "Top level" below): each entry carries `rules: [ID, ...]` and accepts its
  site once under each rule it lists.
- The lock reports these as weakenings: a new `allow` entry, a new `allow_unresolved` entry (a
  second entry with the same file, code and sha included: each accepts one finding, so it accepts one
  more copy of that statement), one that drops its column, a new door, a removed sink, and removing
  the rule. With a sink mapping it judges sink by sink: a door that may now reach a sink it could not
  (added to that sink's list, or the list form replacing the mapping, since there every door reaches
  every sink) is a weakening, and the same doors per sink written another way is not; it judges the
  `allow` map entry by entry: a new entry, or a sink added to an entry, is a weakening, and removing
  either a tightening (a plain list pinned before the map form counts as every sink). A shared
  `allow_unresolved` entry is pinned in each rule it lists: a new one, or a rule added to its
  `rules`, is a weakening for that rule. Deleting an `allow` or `allow_unresolved` entry, a door, a
  sink's door, or a rule from a shared entry's `rules` is a tightening.

`inv init` drafts door rules from the code: see [start.md](start.md#3-path-laws-doors-and-back-doors).

## Top level

| Field | Meaning |
|---|---|
| `spec` | `invariant/v0.1`, the only version this library reads; any other value is a load error. |
| `contracts` | Named contracts: `{type: python \| pydantic \| dataclass \| schema \| openapi \| ..., ref: ...}`, or `{type: openapi, app: "module:app"}` to read the OpenAPI document of a live app (`app.openapi()`, as a FastAPI app has). A `pydantic`, `dataclass` or TypedDict contract pins its JSON Schema's constraints, and loosening one is a weakening ([lockfile.md](lockfile.md#weakening)); removing a contract is one too. `inv lock` pins each one at contract granularity: see [lockfile.md](lockfile.md). |
| `targets` | `"py:pkg.mod.fn": {effects: none \| external}`. Effects belong to the target, once. An unlisted target is `external`. |
| `providers` | `name: {type, from, args}`. See below. |
| `rules` | The list of rules. |
| `allow_unresolved` | Reviewed unresolved sites shared by several path laws: `[{at, code, sha, why, rules: [ID, ...]}]`. An entry is checked as a rule's own entry is, and `rules` names one or more path laws (rules with a sink) of this file, each once. The loader gives each listed rule the entry, at the end of its `mediation.allow_unresolved` (with `rules`, so the scan's text, JSON and SARIF say which shared entry accepted a finding); it then accepts its site once under each rule, and is `missing/allow-unresolved-unused` under a rule where it matches nothing. The lock pins it in each rule. Errors name it `allow_unresolved[0]`, after the file and the entry's line (`invariants.yaml:31: allow_unresolved[0]: ...`). |

## Providers

| Field | Meaning |
|---|---|
| `type` | `int`, `str`, `bool`, `float`, `list`, `dict` or `py:mod.Class`, optionally `\| null`. The function's return annotation must match, read through aliases (`type UserId = int`), `NewType` and `Annotated`: `-> Optional[UserId]` matches `int \| null`. |
| `from` | `py:` path of a function marked `@inv.provider("name")` (sync or `async def`). Its parameters must be exactly the keys of `args`. |
| `args` | Parameter -> binding path (`args.x`, `self`, `reply.y`, or a bound name): dotted names. |
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
| `id`, `rev` | Identity and revision. An `id` is letters, digits, `-`, `_` and `.`, starting with a letter or digit (`^[A-Za-z0-9][A-Za-z0-9_.-]*$`: no spaces, no `:`); it is unique in the file. |
| `status` | Set by human review: `candidate`, `contested`, `detect`, `enforce`, `retired`. The loader computes the highest status the evidence allows (`Rule.max_status`) and refuses a higher one. `enforce` needs an enforcer with mode `enforce`, `falsified_by`, and a passing timing check. |
| `class`, `owner`, `statement`, `why` | Documentation. `statement` is the default clause message. |
| `subject` | `{target: "py:...", contract: name, operation: ...}`; the JSON Schema refuses any other key (`targets:` is a typo). |
| `scope` | `call`, `transition`, `aggregate(key: ...)`: the scope of the property. |
| `bind` | Name -> path, for names `bad` reads: a root (`args`, `result`, `reply`, `self`, `old_self`) and a field path, or `provider.<name>` (nothing after it). A name that matches a provider is bound to it automatically. |
| `bad` | A CEL string, or a list of `{when, message}` clauses. Any clause true is a violation; else any clause error is a rule_error; else ok. `old_self.<path>` makes a transition rule: the loader snapshots only those paths, which must be plain data. Each clause is type-checked at load against the target annotations, the provider types and the model-call schema (see [cel.md](cel.md#the-load-time-type-check) for its scope), once per enforcer that evaluates it, with that enforcer's point and target (a `monitor`'s rows are whatever the caller passes, so they are not typed from `subject.target`); an operation with no overload for the declared types is a load error, and so is a call to a function the chosen CEL backend does not have ([cel.md](cel.md#functions-per-backend) lists them per backend). So is a known function called with the wrong number of arguments, or a macro in global form (`exists(xs, x, p)`). A clause that reads a NaN or ±Infinity, or makes one (`0.0 / 0.0`, an overflow, `double("NaN")`), is a rule_error on every backend unless `inv.configure(allow_non_finite=True)`. Values are read by the mapping in "Python to CEL types" below. On `cel-expr` a clause nested more than 32 levels deep (cel-cpp's fixed limit) is a load error that says so. |
| `compensable` | Optional, `true` or `false`: a violation can be undone after the fact. Nothing at run time reads it; the lock pins it, and setting it to `true` is a weakening. |
| `enforcers` | See below. |
| `mediation` | `{chokepoint, residual_paths, sink, door, allow, allow_unresolved}`. `allow` lists known back doors the scan lets through (see "The door form" above). `allow_unresolved` lists unresolved findings a person reviewed and accepts, one site each: `{at: "path:line[:col]", code: "unresolved/...", sha, why}` ([scan.md](scan.md#accepting-a-reviewed-unresolved-site)). `chokepoint` is the positive mediation argument. `sink` and `door` drive `inv scan`; `sink` may map each sink to the doors that may reach it (see "The door form" above). |
| `model_policy` | For a `model_call` enforcer: `{schema, format_retries, semantic_resamples, feedback: {format: parser_error \| none, semantic: none}, abstain: {field, value}}`. When the abstain field is a closed enum (a `Literal[...]`, also under `Annotated` or a `type X = ...` alias), its value must be a member, compared by value and type (`true` is not `1`; an `Enum` member stands for its value). A clause `!(reply.F in [...])` listing every member of F's enum (the abstain value aside, when one is configured) gets a note: the schema already holds it. |
| `compares_to` | `held_data`: forces `semantic_resamples: 0`. |
| `falsified_by` | pytest node ids (or prefixes) of tests that must fail when the rule is ablated. `pytest --inv-ablate` checks them. |
| `applies_to` | fnmatch pattern(s) over `targets:` keys; `inv lock` expands them. |
| `evidence` | Computed; not hashed. |

## Python to CEL types

`bad` reads Python values through one mapping, the same on every backend and in the load-time type
checker (`invariants_cel/values.py`, in the invariants-cel library; [cel.md](cel.md#python-to-cel-types) has the details):

| Python value | CEL value |
|---|---|
| `None` | `null` (`x == null` and `has(r.x)` work for an `Optional` field) |
| `bool`, `int` (int64), `float` (finite), `str`, `bytes` | `bool`, `int`, `double`, `string`, `bytes` |
| `Enum` member | its value |
| a `str`, `int`, `float` or `bytes` subclass | its plain value (`class Role(str)` is a `string`) |
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
