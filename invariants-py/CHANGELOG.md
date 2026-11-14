# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned as 0.1.0, the first release. Publishing metadata (the final package name, `project.urls`,
authors, PyPI trusted publishing) is not settled yet: see "Before publishing" in
[RELEASING.md](RELEASING.md). Contributors add their entries here.

The publishing metadata holds dummy values on `example.com` (authors, `[project.urls]`, the SARIF
`informationUri`); the release workflow refuses to upload while any remains.

What 0.1.0 ships, for Python 3.11 to 3.14. The core depends only on PyYAML; the extras are
`schema` (jsonschema, for `inv validate`), `cel` (the cel-expr engine), `pydantic`, `fastapi`,
`otel` and `anthropic`. The package is imported as `invariants`.

### The rule file

- One reviewed JSON or YAML file holds the laws: `targets` (the `py:` functions rules guard, with
  their declared `effects`: `none` or `external`), `providers` (typed, read-only functions that give
  a rule data the call does not carry), `contracts` and `rules`.
- A rule has an `id`, a `status` set by a person (`candidate`, `contested`, `detect`, `enforce`,
  `retired`), and `bad`: CEL clauses that are true when the property is broken, read over `args`,
  `result`, `reply`, `self`, `old_self` and `provider`.
- Enforcers say where a rule runs (`decorator`, `middleware`, `model_call`, `edge`, `monitor`,
  `structure`, and the out-of-process `type`, `gateway` and `build`), with a `timing`, a `mode`
  (`off`, `detect`, `enforce`; capped by the status), an `on_violation` (`reject`, `halt`,
  `escalate`, `route`) and an `on_error` (`fail_closed`, `fail_open_and_page`).
- Short forms: `guard:`/`tests:` for a decorator rule, and `door:`/`sink:`/`allow:` for a path law.
  Both pin to the same lock as the long form.
- `inv validate` checks the file against the bundled JSON Schema (`inv.schema_path()`) and runs every
  load-time check: unknown fields, enum values, root paths, bindings, providers, the functions each
  CEL backend has, and a static type check of every clause. Every defect is a `LoadError`.
- The type check, a provider's declared type and the lock's pins read an annotation the same way
  (one internal reader): through PEP 695 aliases, `NewType`, `Annotated` and TypedDict key
  qualifiers. The type check used to read an alias or a `NewType` (and a list or map of one) as
  dyn, and a `typing_extensions.TypedDict` as an open map, so a clause such as `args.amount == "x"`
  with `amount: Cents` (`type Cents = int`) passed the load; it is now refused as with `int`. A
  provider returning `UserId` (`NewType("UserId", int)`), an alias of `int` or `Annotated[int, ...]`
  now matches `type: int` instead of being refused. An alias whose value does not evaluate (`type
  Later = OnlyTyping`, `OnlyTyping` imported under `TYPE_CHECKING`) is read as an unresolved
  annotation (dyn; pinned by its name): `inv.load`, `inv validate` and `inv lock` used to raise its
  `NameError`.
- A `view` or `bind` path into a field of a scalar parameter (`view: {x: args.n.m}` with `n: int`)
  is a `LoadError` naming the rule; it was a bare `TypeMismatch` from `inv.load` and `inv validate`.
- Load errors say what to do: a near miss is named (`unknown field(s) ['tset'] (did you mean
  'tests'?)`, `billing has no 'servce'; did you mean 'service'?`, `args.amout ... (did you mean
  args.amount? its parameters: ...)`, `amount is a parameter of py:...: write args.amount`, a
  provider read as `provider.x` is told to write `x`); a clause written in Python is told the CEL
  form (`and` as `&&`, `or` as `||`, `not` as `!`, `len(x)` as `size(x)`); a parse error names its
  clause; a contract ref `pkg.mod.Class` is told to write `pkg.mod:Class`; a provider's bad `from:`
  is named as the provider's, not a target's. An error in one rule, provider or target of a file is
  prefixed with the file and its YAML line (`invariants.yaml:19: REFUND: ...`), a YAML syntax error
  names the file, and a duplicate rule id names both rules and their lines.
- A target whose module imports a package that is not installed reports that package (`cannot import
  billing.service: ModuleNotFoundError: No module named 'x' (while importing billing.models)`), not
  "'service' does not exist".
- `spec` must be `invariant/v0.1`, and a rule id is letters, digits, `-`, `_` and `.`, starting with a
  letter or digit (no spaces); anything else is a `LoadError` (and a JSON Schema error).
- Door-to-sink pairing: `sink:` (and `mediation.sink`) may map each sink to the door or doors that
  may reach it (`sink: {py:store.record_refund: [py:BillingService.refund], py:store.record_cancel:
  py:BillingService.cancel}`). A door listed for one sink is no door for another, so a write moved
  into the wrong door (a refund record added inside `BillingService.cancel`) is a `bypass`; the list
  form keeps its meaning (any door reaches any sink). `door:` is then left out or names exactly the
  doors the mapping lists; `allow` and `allow_unresolved` stay rule-wide. `inv status`, the MCP
  `explain` and `propose_rules` tools and `inv scan --format json` (`pairs`) show the pairing.
- A top-level `allow_unresolved:` accepts one reviewed site under several path laws: each entry is
  `{at, code, sha, why, rules: [ID, ...]}`, every listed rule must exist and declare a sink (once
  each), and the entry is given to each of them, accepting its site once under each (unused under
  one: `missing/allow-unresolved-unused` there). The text, JSON (`accepted_by`) and SARIF output say
  which shared entry accepted a finding.

### Run time

- `@inv.guard("ID")` checks a target's rules before (`pre_effect`) or after (`post_effect`) the
  call, sync or async; outcomes are `ok`, `violation`, `rule_error` and `parse_error`, a rule error
  is never a violation, and `on_error` decides it. Errors (`Rejected`, `Halted`, `Escalation`,
  `Routed`) never carry the values a rule read.
- `@inv.model_call` parses a model reply into a closed schema (a dataclass, a `{field: type}` map or
  a pydantic model with `extra="forbid"`), retries a format failure with the parser's message,
  resamples a reply that breaks a rule without showing the model the rule (never when the rule
  compares the reply with held data: `compares_to: held_data`), and turns an abstention into a
  review. It returns an `inv.Checked` value. At effective `enforce` an abstain records its event
  (outcome `abstain`, action `escalate`, counted, observed and exported like any outcome, and a
  decision for `--inv-ablate`) before it opens the review and raises `Escalation` (whose `outcome`
  is that abstain). Below effective `enforce` (detect, a candidate rule) an
  abstain never blocks: it is recorded (outcome `abstain`, action `proceed`) and the call returns
  verdict `abstain`, sync and async alike. A dataclass or pydantic reply field that a `Checked`
  attribute would shadow (`value`, `attempts`, `rule`, `verdict`, `_inv_*`, an attribute of the
  subclass) is a `TypeError` at decoration, and so is a pydantic `@computed_field` or a property
  (`property`, `functools.cached_property`) of the reply class with such a name.
- `@inv.model_call` over a sync wrapper of an async def (a `functools.wraps` decorator without
  `inspect.markcoroutinefunction`) awaits the body's coroutine: the decorated call returns a
  coroutine that runs the model call asynchronously, as for an `async def` body. It used to parse
  the coroutine as the reply (`parse_error: reply is not an object (got coroutine)`), retry it and
  reject every call without ever awaiting the model. A body that returns another awaitable (a
  `Future`, a `Task`) is a `TypeError`, never blamed on the model.
- retry, resample and monitor events carry `start_ns` and `end_ns` (a retry or resample spans its
  attempt; a monitor event its row), so their OpenTelemetry spans have real times, not zero-length
  spans stamped when the exporter flushes.
- `@inv.edge` on the consumer of a reply re-validates the schema and re-checks `bad` against state as
  it is now (`inv.Checked` is forgeable, so the edge never trusts it). A received verdict that is
  not ok is refused by `on_violation` as that outcome: a verdict `abstain` is outcome `abstain`
  (HTTP 422 `urn:invariants:abstained` for reject, 202 for route or escalate), never a
  `rule_error` (it was a 503 and counted toward an error spike).
- An edge hands its consumer exactly the value it re-checked: the received `Checked` must be exactly
  the annotated class, and a state is copied as a plain `dict` or as exactly the annotated dataclass
  or pydantic model (a subclass never answers the consumer's reads).
- `post_effect` checks always see the call's real result: over a sync wrapper of an async def, the
  guard awaits the coroutine first; another awaitable is a `TypeError`. `@inv.guard` above
  `@pydantic.validate_call` is a `TypeError` (it would check the uncoerced arguments).
- Whether a guard or an edge can read an async provider is judged by the function it decorates, as
  the wrapper runs it, not by the innermost function: a `pre_effect` check reading one over a sync
  wrapper of an async def is a `LoadError` (it loaded, then every call was a `rule_error`), and a
  guard over an async wrapper of a sync def reads one (it was refused at load though the guard
  runs async). A model call over a sync wrapper of an async def still runs async.
- `JsonlReviews` writes an int too long for decimal text as `"<int 0x...>"`, so a value's size never
  stops a review from opening.
- Providers are read at the enforcer's timing; `old_self` rules compare a target's state before and
  after the call.
- Registries: `inv.load()` installs a rule file in the current registry; `inv.isolated()`,
  `inv.using()`, `inv.capture()` and `inv.observe()` cover tests, and `inv.configure()` the
  production wiring (the event sink's size, the review store, a pager, an exporter, an error-spike alarm). Hand-offs go to a review store (`MemoryReviews`,
  `JsonlReviews`, or your own `ReviewStore`).
- Standing is computed, not claimed: `inv.standing()` gives `held` only when the rule is enforced
  (in effective mode) and not ablated, its chokepoint names one of the rule's functions, no residual
  path is listed, no live `allow` back door is left undeclared, verify passes, and the scan passes
  with no site accepted by `allow_unresolved`; with ablation results, a falsifier must also have
  been caught. Otherwise `enforce(known-callers)`, `monitored` or `candidate`, with the reasons.

### CEL

- Two backends behind one front end, so a rule parses the same everywhere: `subset` (the default,
  standard library only, with a cost budget) and `cel-expr` (Google's C++ runtime, from the `cel`
  extra). `INV_CEL_BACKEND=subset|cel-expr|auto` or `backend=` picks one; `auto` prefers cel-expr.
- The extensions `inv.sum`, `inv.zip` and `inv.unique_by`; timestamps and durations; NaN and
  Infinity are rule errors unless allowed. The shared conformance suite runs against every
  installed backend.
- A pydantic `RootModel` is read as its `.root`; any other sequence or iterable that is not a list
  or tuple is a `rule_error` on every backend (a `LoadError` when annotated), never the map of its
  attributes. An `Enum` map key is its value, and a record is the map of its fields for `size()`,
  `in`, `[]` and the macros, on every backend.
- On cel-expr, a value only errors the clauses that read it (a NUL in one argument never hides
  another clause's violation), and an int past 2^53 is compared exactly with a double per clause,
  with the engine's verdict and every cel-expr function kept.
- A `matches()` pattern has a shared limit (4,096 characters, 64 nested groups, 5,000 instructions
  with repeats expanded), and the subset charges its compile to the budget; a timestamp getter
  whose zone moves past year 1 or 9999 is a `rule_error` on every backend.
- A `str`, `int`, `float` or `bytes` subclass is read as its plain value on every backend (the
  subset compared `class Role(str)` unequal to every string literal, and refused arithmetic on
  `numpy.float64`); the subset does `size()`, ordering and `+` on bytes, as cel-expr.
- On the subset, looking up a value that can never be a map key (bytes, `null`, a list, a map, a
  timestamp: `null in args.m`) is a `rule_error`, as on cel-expr, not a quiet `false`.
- String literals follow CEL's escapes on every backend: an unknown escape (`'\d+'`), an octal
  escape past `\377` and a newline inside the quotes are parse errors (`LoadError`), where the
  subset read `'\d+'` as `'d+'` and so did cel-expr whenever the front end rewrote the clause (any
  `matches()` or `contains()`). Raw strings (`r'\d+'`) parse, and the error says to use one.
- `has(x.f)` on the subset reads `x.f` whole, as cel-expr's projection does: a present field holding
  a refused value (NaN, a `Decimal`, a set, a generator) is a `rule_error` on both, not `true`; on
  cel-expr, arithmetic inside a `has()` argument is checked finite, as everywhere else.
- `isinstance()` in a clause is refused with advice that loads (`type(x) == type(1)` on cel-expr); a
  bare CEL type name (`type(x) == int`) is refused with the same advice instead of a bare
  "undeclared name".
- `inv.evaluate(rule, base)` (the rule-level names, `"rule"`) binds auto-bound providers when an
  enforcer has a `view:`; it was a `rule_error` ("undeclared reference") there.
- Under `(?i)` the subset's regex classes (`\w`, `\W`, `\d`, `\s`, `\pL`, `\p{Lu}`, `[:upper:]`)
  fold case as RE2 does (closed under folding, then negated): `(?i)\p{Lu}` matches `a` and
  `(?i)\W` no longer matches the Kelvin sign, as on cel-expr. `\_` is a literal `_` and `\é` is
  refused, as in RE2. `\Q...\E` is its characters one at a time, as in RE2: a quantifier after it
  repeats the last one (`^\Qab\E*$` matched `abab` on the subset, `abbb` on cel-expr), an empty
  `\Q\E` is nothing (`\Q\E?` was true on the subset and passed the load-time check), and a repeat
  count with nothing before it (`{2}`) is an error, not a literal. A search may start inside a
  multi-byte character, as RE2's byte search does, where `\B` holds: `"aéa".matches("\\B")` is true
  on both (it was false on the subset).
- A timestamp getter given a null zone (`t.getHours(args.tz)` with `tz` None) is a `rule_error` on
  every backend; the subset read it as UTC. So is any argument to a duration's getter.
- `int()`, `uint()` and `double()` of text take ASCII whitespace around the number only, as absl
  does on cel-expr: `int("500\u00a0")` was `500` on the subset and is a `rule_error` on both, and
  `"1\u001c"` no longer escapes as a Python `ValueError` (every conversion failure is a CelError).
- An even run of `!` or unary `-` (`!!args.s == "x"`, `--args.i`) checks its operand on cel-expr:
  cel-cpp's parser drops such a run unchecked, so the front end sends that clause re-printed
  (`!(!(args.s))`); it was `true` on cel-expr and a `rule_error` on the subset.
- A clause nested more than 64 levels deep (parentheses, brackets, calls, `?:`, unary operators) or
  whose tree is more than 256 levels deep (a 3,000-term `&&` chain) is a `LoadError` naming the
  rule, on both backends; it was a bare `RecursionError` (`inv validate` printed it). The middle
  of a `?:` is no longer another unparenthesized `?:` on the subset (`a ? b ? c : d : e`), which
  cel-expr already refused.
- An octal escape in a `matches()` pattern reads as RE2 reads it: `\101` is `A`, `\12` a newline,
  `\1234` is `S` then `4`; the subset refused every one but `\0..` as a backreference. A lone `\1`
  to `\7` is still refused, as in RE2.
- cel-expr's cost message names the backend once: `cost limit exceeded (cel-expr: at most 9,999
  ...)`, not `cel-expr: cost limit exceeded (cel-expr: ...)`.

### The lock

- A sink mapping is pinned as written and judged sink by sink: a door that may now reach a sink it
  could not (added to its list, or the list form replacing the mapping) is WEAKENED, a door taken
  off a sink is a tightening, and the same doors per sink written another way is not a weakening. A
  top-level `allow_unresolved` entry is pinned in each rule it lists: a new one, or a rule added to
  its `rules`, is WEAKENED for that rule, with the same multiset judgement per (rule, file, code, sha)
  as a rule's own entries; a rule taken out is a tightening.
- The process that decides WEAKENED never imports the project. `inv validate`, `inv lock`, `inv
  check`, `inv init` and the stop hook import the app in one child process per command (`python -I`,
  the same import path) and take from it only what importing alone can say (signatures, model
  schemas, a live app's OpenAPI document, clause types); the rules, the scan settings, declared
  effects and provider fields, and file contracts are pinned in the deciding process, from the rule
  file, the locks and the scanned sources it read before the child ran. A module that sets
  `INV_ALLOW_WEAKENING`, patches `invariants.lock.changes`, fakes the lock or rewrites a file at
  import no longer passes `inv check --base` or lets plain `inv lock` write a weakened lock. The
  reviewer's switch is read before anything else. The trust boundary that is left (the lock trusts
  the imported code to describe itself) is stated in lockfile.md, "What the lock trusts", agents.md
  and SECURITY.md; start.md's CODEOWNERS advice now covers the guarded and contract modules and
  `.github/workflows/`.
- `inv lock` refuses a rule file `inv validate` rejects (an old 10-digit or malformed
  `allow_unresolved` entry, for one), with validate's message, even with `--accept-weakening`.
- A change to `mediation.allow_unresolved` is listed entry by entry (added, another entry for,
  removed, moved, new reason, a malformed entry by its index), not as the whole list on both sides.
- An OpenAPI YAML contract that stands for more than a million values once its aliases are
  expanded is refused instead of walked.

- `a && true` and `a || false` (for a bool `a`) are the same condition as `a`: no weakening, and the
  message no longer says "matches fewer cases".
- A schema's `required` list is compared as a set: a reordered list (a pydantic field moved in its
  class) or a dropped duplicate is no longer WEAKENED; a field added stays a tightening and one
  removed a weakening.
- A resolved annotation that is an object of no recognised form is pinned as `{"object":
  "module.Type"}`, never its repr (a memory address made the lock differ every run, and a repr can
  carry data); a type nested past the depth limit is pinned by its name.
- With `unevaluatedProperties` or `unevaluatedItems` false (or a schema) on a schema or an ancestor
  it is an in-place subschema of, an applicator added or changed there (`allOf`, `dependentSchemas`,
  `if`, `contains`, `$ref`, a property inside an `allOf` branch, ...) is WEAKENED: what it evaluates
  is no longer refused. It was judged a tightening.
- A Draft 4 / OpenAPI 3.0 boolean `exclusiveMaximum` or `exclusiveMinimum` turned from `true` to
  `false` is WEAKENED.
- A pinned schema change is judged at the polarity of its place, carried through `$ref`s and the
  schema table: a `$defs` entry or an OpenAPI component used under `not` is judged the other way
  round (`not: {$ref: A}` with A's `maximum` 5 -> 3 lets 4 through and is WEAKENED; 5 -> 9 is a
  tightening), and one used under `oneOf`, `if` or a `contains` with `maxContains` (or both ways)
  is judged both ways. These were judged by the change alone.
- A subschema replaced by `true` (an `allOf`/`anyOf`/`oneOf`/`prefixItems` entry, a property, a
  `$defs` entry, `not`, `items`, ...), a removed `patternProperties` entry, a removed Draft 7
  `dependencies` entry (or a name dropped from one), and a `minContains: 0` added are WEAKENED; they
  were not judged.
- `const` and `enum` are compared as whole values: a structured value (`const: {"a": 1}` to
  `{"a": 2}`, `enum: [[1]]` to `[[2]]`) was diffed as if it were a schema, and a removed `const:
  null` was read as absent. Both are WEAKENED now.
- Draft 4 / OpenAPI 3.0 / Swagger 2.0 schemas are judged with Draft 4's bounds, where a numeric
  `exclusiveMaximum` bounds nothing: `maximum: 10` to `exclusiveMaximum: 11` is WEAKENED there. An
  OpenAPI 3.0 or Swagger 2.0 contract (and its component schemas) now pins `dialect: openapi-3.0`
  (`swagger-2.0`), so the first `inv lock` after upgrading re-pins such contracts (a change, not a
  weakening); OpenAPI 3.1 pins are unchanged.
- `$ref`s are resolved through `$id`, `$anchor` and `$dynamicAnchor`: an edit of one that makes an
  unchanged `$ref` name a schema with another body is WEAKENED, as is a changed `$schema` and, in
  Draft 7 and earlier, a `$ref` added beside other keywords (which it then hides).

- `inv lock` writes `invariants.lock` (lock version 3), which pins what each rule's meaning depends
  on: the rule's semantics, its targets' signatures and resolved types (through aliases and into
  dataclass, TypedDict and pydantic fields), parameter constraints, providers, contracts (pydantic,
  dataclass, TypedDict, `{field: type}` schemas, OpenAPI files or a live app's `app.openapi()`,
  python modules, other files by digest), the scan configuration, and a content-addressed table of
  schemas without prose.
- Every difference is explained as old -> new; a change that may relax a rule is **WEAKENED**, and
  `inv lock` refuses to write it without `--accept-weakening`. `bad` edits are compared as canonical
  trees: an edit that provably flags more cases is accepted, and anything undecidable is a possible
  weakening.
- `inv lock --check` fails CI on a stale lock. Only lock version 3 is read: a lock of another
  version is refused with what to do.
- An OpenAPI operation's `parameters` are compared by `in` and `name`, not by position: removing
  one shifted the rest, so a removed parameter was never WEAKENED, and a loosened one could be read
  against its neighbour. A removed parameter is now WEAKENED (a required one: a request without it
  passes; an optional one: its value is no longer checked), an added one only tightens, and
  `allowEmptyValue` turned on is WEAKENED. Changes name the parameter
  (`operations.op.parameters[query limit].schema.maximum`).
- A Swagger 2.0 parameter's own schema fields (`type`, `format`, `maximum`, `enum`, `items`, ...;
  every parameter but `in: body`) were not pinned, so loosening one was no change at all. They are
  pinned as the parameter's `schema` (with `collectionFormat`) and judged as Draft 4. A lock of a
  Swagger 2.0 contract that names such a parameter is stale once: re-pin it with `inv lock`.
- A Swagger 2.0 response's `schema` (beside its `description`, not under `content`) was pinned as an
  empty response, so loosening it was no change. It is pinned under each media type the operation
  (or the document) `produces`, `application/json` by default, and judged like an OpenAPI 3
  response. A lock of such a contract is stale once: re-pin it with `inv lock`.
- A plain `inv lock` over a stored lock whose schemas table has a hand-edited entry wrote a changed
  schema with "the change cannot be shown" and exit 0, though the change could loosen it. A changed
  schema whose old entry the stored lock does not hold is WEAKENED: `--accept-weakening` writes it.

### Static checks

- `inv scan` reads the source with `ast` (it never imports it) and checks every door rule: no call
  path from an entrypoint reaches a `sink` without passing its `door`. Findings are `bypass`,
  `forbidden` (a bypass idiom: `getattr`, `__wrapped__`, monkeypatching, reflection), `unresolved`
  (never counted as a pass) and `missing`. Known back doors are frozen in `allow`; a new caller of
  one still fails. Output is text, JSON or SARIF 2.1.0.
- The scan's file handling fails closed. Symlinked directories inside a root are followed, as
  Python imports them (cycles cut; a target outside the roots is scanned and noted). An import of
  a module a scanned package does not hold is `unresolved/unknown-module`, never third-party (an
  installed portion of a namespace package is the one exception, below). A
  file in an always-skipped directory (`build/`, a dot-directory, a hand-made `site-packages/`) that
  imports any scanned module, or imports code it names at run time, is
  `unresolved/skipped-importer`; only a recognised virtualenv or node package and exact copies of
  scanned modules stay quiet. A zip archive holding Python code in a root is `unresolved/no-source`,
  and scanned code that writes `sys.path`, `sys.meta_path` or `sys.path_hooks` is
  `unresolved/reflection`.
- The scan runs on large trees with the same results: one path search per rule instead of one per
  entrypoint, a name-matched call kept as one record instead of one edge per method of that name,
  and each module's tree walked once for the passes that only collect from it. SQLAlchemy (239k
  lines) scans in 3 s instead of 16 s; PyTorch (1M lines; it did not finish in 18 minutes before) in
  20 s and 1.6 GB; transformers in 30 s and 1.6 GB (it did not finish in 20 minutes before). A scan
  that runs past 5 seconds prints a progress line on stderr (`INV_PROGRESS=0` turns it off).
- A portion of a namespace package installed outside the roots (`google.genai` beside an app's own
  `google.adk`) is third-party, not `unresolved/unknown-module` (google-adk: 258 findings to 0 with its
  dependencies installed). `inv check` lists each accepted `allow_unresolved` site under its `ok scan`
  line instead of a bare "no path skips a door or guard".
- Reviewed exceptions: `mediation.allow_unresolved` (short form `allow_unresolved:`) accepts an
  unresolved finding a person has read, one site at a time: `{at: "path:line[:col]", code, sha,
  why}`, where `sha` (16 hex digits, in `--format json`) hashes the finding's code, function and
  whole statement (every line of it, token by token, so a reformat keeps it; a compound statement's
  header), and for an `unresolved/inferred-path` finding also the entrypoints and name-matched calls
  that reach its site (listed as `reached_by`), so a new untyped caller of an accepted helper is a
  new, unaccepted finding. An accepted finding is shown (`accepted` in text, JSON, a SARIF
  suppression with the `why`, `inv status`, MCP `explain`) and never fails the rule, but a rule with
  one is not reported `held` (a person's judgment, not the scan's proof); an edit above
  the site keeps the entry (reported as moved), an edit of any line of the statement does not; an
  entry that matches nothing is `missing/allow-unresolved-unused`; entries at their own line are
  matched before moved ones. `bypass` and `forbidden/*` findings can never be accepted. The lock
  compares entries as a multiset: a site named by more entries than before (a second entry for a
  copied statement included), or by more entries without a column, is a weakening
  (`--accept-weakening`); fewer is a tightening. The SARIF fingerprint is `invSite/v2`.
- Narrowing the scan can prove: a `getattr` with a computed name on an instance (or the class) of a
  class whose whole family the scan sees, where no attribute is named like a sink or door and none
  can reach a sink without a door, and each base outside the scan is a plain one (the builtin types
  and exceptions, `abc.ABC`, the `Enum` family, `NamedTuple`, `Generic`, pydantic's `BaseModel`), is
  not reported; reading only a namespace's names (`globals().keys()`, `name in globals()`) is not
  reflection. Reflection elsewhere is still reported wherever it is (a module that imports nothing
  can still be handed the sink). A `getattr` with a computed name on a module or object outside the
  scan (`getattr(logging, level)`, `getattr(builtins, name)`) is reported: a module's attributes
  include the modules it imports (`logging.sys`, `logging.os`), its `__builtins__` and `__loader__`.
  On jupyter_server's session door this, with the unwrap change below, took 1 forbidden and 19
  unresolved findings to 17 unresolved (4 on tornado and traitlets bases); on google-adk's credential
  door 2 forbidden and 76 unresolved to 77 unresolved (8 on `logging`, `litellm` and
  `google.genai`; besides 258 imports of `google.*` packages the roots lack).
- A function used as a value is an entrypoint: one that can reach a sink without passing a door (a
  door's own helper included) and is taken as a value outside the doors and allowed functions (a
  thread target, a callback, a dict entry, an alias, a bound method) is reported where it escapes; an
  allowed back door taken as a value is a new caller, a `bypass`. A name bound to several functions
  (a conditional import, a star import re-exporting another one) calls each of them.
- Values the scan did not see as values. A class-body alias of a method (`post =
  record`) is that method under another name, a name used in a class body is looked up in the class
  first, and any other class-body use of a method (`{"post": record}`, `property(record)`) is a value.
  A dunder method or alias (other than `__init__`, `__new__`, `__post_init__`) is called by Python from
  syntax (`a + b`, `x[k]`, `with`, `len()`, a format), so it is an entrypoint even when the door calls
  it too. A decorator that may keep the function (`@app.post(...)`, `@atexit.register`, a registry
  that appends it) is handed it as a value; known wrappers (`property`, `functools.lru_cache`,
  `contextmanager`, inv guards, a scanned decorator that only returns its argument or a wrapper
  calling it) are not, and an allowed route is not a new caller of itself. A scanned module used as a
  value (`{"stripe": stripe_backend}`, `m = payments`, `self.mod = payments`, a class-body `import
  payments`) hands out its functions, and a call on a receiver the scan cannot type may be a module's
  function as well as a method. A class used as a value (`Thread(target=Sender)`, `self.op = Sender`)
  runs its constructors when called (not when it is only tested: `isinstance`, a base, `except`,
  `case`, a generic); a call of a class runs its `__new__`, `__post_init__` and metaclass `__call__`
  too, and `type(x)(...)`, `x.__class__(...)`, `copy.copy(x)` and `dataclasses.replace(x)` run the
  constructors of x's class (any class when x is untyped). A property read on an unknown receiver may
  run a getter of that name; `operator.attrgetter`/`methodcaller` names are followed like a getattr's;
  `__subclasses__` is reflection. A module-level name bound once to a scanned class's constructor
  (`settings = Settings()`) is typed where the module lists all its names and no code sets it from
  outside.
- `forbidden/unwrap` ("calls a guarded function without its guard") is reported only when what is
  unwrapped can be a guarded door (a door or target of a rule, or a function with an inv guard);
  any other unwrap that bears on the rule is `unresolved/unwrap`, and `hasattr(f, "__wrapped__")` or
  an unwrap of a standard-library or third-party object is not reported.
- A write of a package's `__path__` (`__path__.append`, `pkgutil.extend_path`) is
  `unresolved/reflection`: its submodules may come from any directory. `sys.path` changed through a
  name bound to it (`p = sys.path; p.insert(0, d)`) is reported too. Name-matched alternatives are
  followed in sorted order, so a scan gives the same paths on every run.
- A class's inferred type is trusted only when its identity cannot be altered: known
  identity-preserving decorators (`@dataclass`, `@functools.total_ordering`, ...) and scanned ones that
  hand back their parameter unchanged, a `__new__` that passes on its own first parameter, no
  `__getattribute__` of its own, not a `Protocol`, no virtual subclass. Writes through `__dict__` it
  cannot name, an alias of it included, untype the attributes or are reported.
- Code run from strings or found without a name is `unresolved`: `string.Formatter`, `code`
  interpreters, `timeit` with string statements, `doctest`, `trace`, `gc` object walks, frame
  `f_globals` / `f_locals`, `inspect.getmodule`, and subprocesses that run Python.
- A return annotation over an unannotated factory that visibly returns another class is not trusted,
  and a binding in a nested scope types that scope only. Parameter and field annotations stay trusted
  (a type checker checks them).
- Names bind the scope they are in. An import in a function or class body is not a module attribute
  (`from ops import go` sees the module's own `go`); a name a body imports as an object the module
  scope (or another body) binds otherwise resolves to nothing, and calls and values through it reach
  every object. A name a module binds to more than one object (if/else imports, a star import) is
  each of them in its importers too (`from ops import go`, `ops.go`), classes used as values
  included. A function's locals are its own scope's: a nested function's or lambda's parameters and
  bindings, a comprehension's targets and a nested `global` / `nonlocal` no longer hide or retype the
  enclosing function's names, and a match capture (`case x`, `[*x]`, `{**x}`) binds a local.
- Methods are looked up along the C3 method resolution order (in `class D(B, C)`, an override in `C`
  comes before what `B` inherits from a shared base). A class name rebound by a star import or a
  `case {**name}` capture at module level is not trusted.
- A scanned decorator whose wrapper is itself decorated by anything but `functools.wraps(...)` may
  keep the function (it is used as a value). `setattr` / `delattr` of a dunder of a sink or door
  (`setattr(door, "__code__", ...)`) is `forbidden/monkeypatch`, as the assignment was; a write of
  `__code__`, `__defaults__` or `__kwdefaults__` on a function the scan cannot name is
  `unresolved/unknown-receiver`.
- Fix round 28 (false passes, then adoption on real codebases). An absolute import found through the
  importing file's own directory (a script's `from pay import sink`) or a deeper `sys.path` entry
  (`import fleet.store` for `cli/fleet/store.py`) is followed, where it used to resolve to nothing so
  even a direct sink call passed; a name several such modules match is `unresolved/unknown-module`.
  `cls(...)` in a `@classmethod` builds the class or a subclass. Annotations of module-level defs,
  methods and module or class bodies are walked: a call in one runs, `Annotated` metadata
  (`Depends(f)`, `AfterValidator(h)`) are values, string and postponed annotations are parsed, and an
  empty FastAPI `Depends()` builds the annotated class. A literal lookup naming a scanned class
  (`getattr(svc, "K")()`, `svc.__dict__["K"]()`, `pydoc.locate("svc.K")()`) builds it, and one naming
  a class or module is a value. An allowed `pkg.mod.<module>` entry (which `inv init` writes) no
  longer excuses a new module-level sink call or sink value in that file.
- Fewer false findings: writing a door's `__doc__`, `__name__`, `__qualname__`, `__module__` or
  `__annotations__` is not a monkeypatch; a computed `setattr` on a plain class the scan sees whole
  (no property, descriptor or `__setattr__` in its family) is narrowed like a computed `getattr` (fix
  round 29: only when its name cannot be `__class__`); an
  unwrap of an unknown object is reported only when a decorated function (or a method named
  `__wrapped__`) can reach the sink, and an unknown `fn.__closure__` read is `unresolved/unwrap`, not
  `forbidden/unwrap`. The sha ignores black's magic trailing comma (not a one-element tuple's), so the
  sha of a statement written with one changed once; a moved `allow_unresolved` entry with a column is
  told its new column. Shas are worked out only when shown (JSON, SARIF, a rule with
  `allow_unresolved`), from the index's own parsed trees. `inv init` counts back doors and unresolved
  sites apart, and lists the unresolved ones for review as drafted `allow_unresolved` entries (an
  empty `why` the rule file refuses until a reviewer writes it) instead of dropping the draft. A
  relative import naming no module is reported under the indexed package name.
- Fix round 29 (false passes from an independent review). A method taken as a value on a receiver the
  scan cannot type (`Thread(target=r.pay)`, `ex.submit(e.pay)`, `f = e.pay`) may be any scanned
  function of that name: `unresolved/sink-escapes` when one reaches the sink without a door (it was
  reported only when the name was the sink's). A computed `setattr` may set `__class__`: on a method's
  self it distrusts the class as `self.__class__ = ...` does, on `obj.attr` or a module-level singleton
  it untypes them, and it is narrowed only when its name is a dataclass field's or what a
  `__setattr__` or `__setstate__` passes on (`for k, v in d.items(): setattr(self, k, v)` is reported
  again). `self.x` bound by a for / async for / with / comprehension target is untyped, a module-level
  singleton rebound by a match capture or `except E as x` is untyped, and a method that rebinds its
  first parameter (`self = Repo()`, `for self in`, `except E as self`, `self += x`, `cls = Repo`) no
  longer types it as the class. An import the importing file's own directory also holds, shadowing a
  top-level module (under every rule) or a standard-library module (under a rule that module's code
  bears on), is `unresolved/unknown-module`.
- Fix round 29 (adoption on real codebases). **Every `allow_unresolved` sha changes once**: a site's
  sha now hashes the syntax tree of its statement (a compound statement's header: the test, the
  iterable and target, the items, the decorators and signature), not its tokens, so a formatter
  (black, `ruff format`: quotes, f-string quotes, redundant or added parentheses, `0XAB` to `0xAB`,
  `1E5` to `1e5`, line breaks) never changes it and a change of what the statement means does; the
  key is the same on Python 3.11 to 3.14. A sha is still 16 hex digits: an entry that names the sha
  this version used to give is `missing/allow-unresolved-unused` with the new sha of the same site to
  write (then `inv lock`, as for any changed entry); the SARIF fingerprint is `invSite/v3`. `inv init`
  works out the functions on door-free paths once (it re-ran the path search for every allowed name:
  sqlalchemy 48 s, torch did not finish), and freezes every function on a door-free path to the sink,
  not only those on the shortest path from each entrypoint: a function on a longer path was frozen and
  then reported as an unused allow entry, which deleting failed the rule (fact-pipeline's draft was
  refused for good); an allow entry is in use when it is on any such path. The freeze stops when a
  round freezes nothing new (unresolved sites left kept a rule from passing, so all five rounds re-ran
  the same scan), a function taken as a value is searched from once, the path search spreads a
  name-matched fan once per search, and the source read for shas is kept across the scans of one
  graph; findings, paths and messages are unchanged. A long freeze prints progress lines. A method call on what calling a value class of the standard library makes
  (`pathlib.Path(__file__).resolve()`, `datetime(...)`, `Decimal(...)`, a `str` literal) is that class's
  method and never name-matches a scanned method of that name (another class outside the scan still
  is: a wrapper may delegate, `io.TextIOWrapper(f).write` calls `f.write`). An import that is not
  installed and that several modules below a root may be says to run the scan where the dependency
  is installed. The text report shows each finding's code, and a rule with unresolved findings and no
  `allow_unresolved` is told that `allow_unresolved: []` shows their shas.
- `inv init` froze in `allow` every function on any door-free path, a name-matched one too, so an
  `unresolved/inferred-path` site (`r.save(x)` on a receiver the scan cannot type) was silently
  excused and the drafted rule passed. It freezes only the functions on a door-free path of
  extracted calls; an inferred-path site stays an unresolved finding, listed and drafted in
  `allow_unresolved` for review (the rule is written commented out until each says why).
- Lookups the scan missed (round 31): `x.__getattribute__("save")`, `object.__getattribute__(x,
  "save")` and `type.__getattribute__(type(x), "pay")` on any receiver are `getattr(x, "save")`
  (naming the sink is `forbidden/getattr-sink`; otherwise an edge or a name-matched call);
  `type(r).__dict__["pay"]`, `.get("pay")`, `r.__class__.__dict__["pay"]` and
  `type(r).__mro__[0].__dict__["pay"]` are the method of r's class (typed) or name-matched;
  `"{0.total}".format(a)` and `format_map` look up the attributes their literal fields name, as
  `attrgetter` does; a datetime or time method that asks its `tzinfo` (`utcoffset`, `dst`, `tzname`,
  `astimezone`, `isoformat`, `strftime`, `timestamp`, ...) is name-matched to the scanned methods
  `utcoffset`, `dst`, `tzname` and `fromutc`. A value class of the standard library honours a
  second module-level binding (`global Path; Path = Repo`) as a scanned class does, and a name
  written as an attribute of its module by any code (`ops.Safe = Repo`, `setattr(ops, "Safe",
  Repo)`) is a runtime value there. Two different huge integer literals no longer share a sha.
- Rule checks (round 31): a sink that is its own door (`{S: [S]}`, or `S` in both `sink` and `door`)
  is a load error; a rule's own `allow_unresolved` entry that lists `rules` other rules do not hold
  is refused (an entry for several rules goes in the file's top-level `allow_unresolved`); with a
  sink mapping, a stale `allow` entry that is a door of one sink is `missing/allow-unused` when every
  other sink's check finds it unused. The hint for an `allow_unresolved` sha from an earlier inv
  says that a person runs `inv lock --accept-weakening` after writing the new sha.
- `inv verify` imports the app and checks that every target carries its guard and every guard
  names a rule.
- `inv check`'s guards step took the child's `inv verify` verdict as it was, and the child imports
  the app first: a module that replaced `invariants.cli.child.cmd_verify` at import passed the step
  with `@inv.guard` removed from the door. The deciding process now reads each target's and edge
  target's decorator from the source (never importing it), and the step passes only when that and
  the child both pass; a target it cannot read from the source (no `.py` file, a name bound twice or
  in a block, non-literal rule ids, a decorator made another way) fails closed. The child also
  refuses to report when the imported code replaced its check (docs/lockfile.md, "What the lock
  trusts").
- `inv init --root/--exclude` beside an existing `invariants.lock` no longer writes
  `[tool.invariants.scan]` into `pyproject.toml`: it prints what to add by hand. `inv hook pretool`
  blocks `inv init` with `--root` or `--exclude` in a tree that holds a lock.
- A key repeated in one mapping of the rule file (YAML or JSON) was read as its last value; it is a
  LoadError naming the lines. A lock with a repeated key is not read either.
- `inv status` reports each rule's standing with its reasons.

### CLI

- `inv validate` (and `inv check`'s rules step) prints the loader's and the scan's specific
  messages before the JSON Schema's, and cuts each schema message to 200 characters.
- A YAML anchor or alias in a rule file is refused with the line it is on (a nested alias bomb hung
  `inv validate`).
- A contract `ref` whose attribute path goes through a module (`app.mod:os.environ`,
  `app:models.Refund`) is refused: name the module before the colon. Under `inv mcp`
  (`INV_CONFINE_ROOT`) an attribute path of a target, provider or contract goes only through objects
  defined inside `--root`, so `os.environ` or `sys.modules` cannot reach the `check` tool's output.
- The pretool hook treats every `python` option cluster with `c` before the script as code from a
  string (`python -I -c`, `python3 -Ic`, `python3 -u -c`, `-W ignore -c`), and `python -` as code
  from stdin.
- `inv status` says to accept a reviewed unresolved site in `allow_unresolved` (with the sha `inv
  scan --format json` gives), not in `allow:`, and `inv check`'s failing scan step says how to get
  the sha. `inv init` mentions commented-out door drafts only when it wrote one.

- `inv init` says when it wrote no path law (so `inv check` will skip its scan).
- `inv init --root/--exclude` writes what it scanned into `[tool.invariants.scan]` of the
  `pyproject.toml` beside the rule file (`roots` relative to the rule file, the globs as
  `extend_exclude`), creating the file or table when missing and only inserting lines (the rest of
  the file is kept byte for byte), and prints what it wrote, so the lock it takes and the next `inv
  check` scan exactly what it scanned. It never changes settings already there: one that differs is
  named in a note beside what init used. (It used to print a note asking for the settings by hand.)
- `inv init` drafts a door rule whose writes have different doors as a sink mapping (each write
  with its own doors) instead of one list of every door, and drafts an unresolved site several
  drafted rules see once, in a top-level `allow_unresolved` entry listing them.
- The pretool hook reads each command segment in every directory an earlier `cd` or `pushd` moves to
  (`cd tools && python ../run.py`). `inv status` says a rule below `enforce` flags (logs) a bad call
  rather than refusing it. The cel-expr cost message names the real limit (at most 9,999 iterations).

- `inv init` drafts `invariants.yaml` from the laws the code already has (constrained pydantic
  models as contracts, service-to-repository door rules with today's back doors frozen in `allow`,
  and function preconditions translated to CEL, written commented out), then locks it. It never
  overwrites a rule file, and reads a Graphify `graph.json` for door hints (`--graph`); every
  proposed door is checked by the scan. It writes at most `--max-doors` door rules (default 5), the
  most important first, and lists the rest. With no Python source to read it exits 2 and writes
  nothing. It tries the lock's pin on each drafted contract first, and writes a model pydantic
  cannot give a JSON Schema for (or whose module does not import) commented out with the reason, so
  the lock step does not fail on it. It skips private functions and
  `assert self.x is not None` checks, reads `if ...: raise` preconditions after a docstring, logging
  call or plain assignment, does not count `dict.update`, `set.add`, logging or callback
  registration as writes, and never proposes a read-only function (`get_*`, `list_*`, ...) as a door.
- `inv lock` names the contract and its ref when it cannot pin one (`contract agent
  (app.models:Agent) cannot be pinned: PydanticInvalidForJsonSchema: ...`).
- `inv check` runs validate, `lock --check`, verify and scan with one exit code. `--relock` re-pins
  when nothing weakens (`--fail-on-repin` then exits 1 so the new lock is staged); `--base REF` also
  fails on a weakening since the lock at REF, unless `--accept-weakening` or `INV_ALLOW_WEAKENING=1`,
  and fails closed when REF or its lock cannot be compared (a REF with no lock is a first adoption).
- `inv lock` and `inv check` both take `--accept-weakening`, the documented spelling, and
  `--allow-weakening` as another name for it; `inv hook pretool` blocks both.
- `inv status` says per rule, in plain words, what it checks, whether a guard wraps it, the scan
  result, whether ablation proved it (`yes`, `no`, `not run`, `not needed`) and what to do next;
  `--json` keeps its fields and adds `status_text`, `checks`, `guarded`, `ablation` and `next`.
  A door-only rule (`ablation: not needed`) is never labelled `held (unproven: no --ablation)`, and
  `--ablation` no longer downgrades it for having no falsifier.
- `inv scan` with no rule naming a sink says `no door rules to check (N file(s) scanned)`.
- `[tool.invariants.scan]` takes `extend_exclude` (added to the default excludes) beside `exclude`
  (which replaces them); the lock pins the effective list.
- `inv hook pretool|stop`: Claude Code hooks that refuse a command accepting a weakening, a direct
  edit of `invariants.lock` or of the scan settings in `pyproject.toml`, a command that rewrites
  either without naming it (a project script whose text names it, a project module the script
  imports or, for pytest, a conftest.py, a `-p` plugin or a module they import whose text names
  it; `git apply`, `git stash pop`, `git checkout REF -- .`, `git restore --source`; `patch`,
  `gpatch` or `busybox patch` of a patch naming it; `git merge`, `cherry-pick`, `reset --hard`,
  `checkout REF` or `switch REF` to a commit whose copy differs; a recursive `cp`, `rsync` or
  `ditto` into the tree of a directory holding it), an edit of
  `.claude/settings.json` or `.claude/settings.local.json` (the hooks' own configuration), and
  finishing while `inv check --relock` fails (the relock re-pins only a change that weakens
  nothing; the rule file is found in the event's cwd or the nearest directory above it, and a lock
  with no rule file beside it sends the agent back too). `python -m MOD` reads MOD itself with the
  script rule (pyproject alone counts), found under cwd, `src/` or the lock's tree, as
  `python tools/x.py` is. They are an early warning (test modules and computed imports
  are not read); `inv check --base` in CI is the gate.
- `inv mcp` and `inv-mcp`: a read-only MCP server over stdio (`scan`, `check`, `status`, `explain`,
  `propose_rules`); it never writes the rule file, the lock or `allow`, and never reads a file or
  imports a module (the standard library and installed packages included) outside its `--root`
  for a rule file inside it. That holds through symlinks: under `INV_CONFINE_ROOT` a finder first
  on `sys.meta_path` refuses, before its code runs, any module of the project (a submodule, one a
  package's `__path__` leads to, a submodule of a namespace package with a directory inside the
  root, one the project's code imports) that resolves outside the root; every `module:attr`
  contract ref (pydantic included) imports through the same check; a symlinked `invariants.lock`
  that resolves outside the root is refused before it is read;
  and the scan the server runs does not follow a symlink under the source roots that resolves
  outside it, reporting it as `unresolved/outside-root` with the link's path only.
- `inv --version` (`-V`) prints `inv (invariants) 0.1.0`; a bare `inv` lists the commands (`validate`,
  `lock`, `verify`, `scan`, `init`, `check`, `hook`, `status`, `mcp`).
- Exit codes: 0 ok, 1 a check failed, 2 usage error, 3 the scan has unresolved calls, 4 the scan
  skipped an ablated `enforce` rule (3 and 4 are not passes).
- A GitHub Action (`action.yml`) and a pre-commit hook (`.pre-commit-hooks.yaml`) at the repository
  root. The action installs invariants into a venv of its own before anything of the pull request is
  installed (an optional `install` input runs after it) and runs `python -I -m invariants.cli check`
  from it, never the `inv` on `PATH` (which a pull request's console script could replace); the
  dependencies' site-packages are passed as `--site`, asked for with `python -I -S` so no `.pth` file
  runs. start.md says plainly that installing the pull request runs its build code in the gate's
  job, and recommends installing only the dependencies, from wheels.
- `--site DIR` (validate, lock, verify, check, init, status, hook): import the app's dependencies from
  DIR, after inv's own path and only where the app is imported; its `.pth` files are not run.
- `python -m invariants.cli` (and `invariants.mcp`) drop the working directory from `sys.path` before
  anything is imported, as `python -P` does, so a project's own `yaml.py`, `tomllib.py`, `json.py` or
  `jsonschema.py` is not imported by the process that decides the verdict; the MCP server runs its
  `inv` child with `-P`.
- `inv check --base` compares with the scan settings read before the child that imports the app ran
  (it read `pyproject.toml` again after it, so a module rewriting it back to the base's settings at
  import hid an added exclude); the child is refused scan settings not read before it ran.
- Text output (`inv scan`, `inv check`, lock diffs, `inv status`, errors) escapes control characters
  in paths and messages (`\n`, `\r`, `\x1b`), and a line from data that would start a GitHub Actions
  workflow command (`::error::`) is printed with a backslash first; JSON and SARIF were escaped already.
- `inv check`'s hint for an unresolved site names the spelling of the failing rule's form:
  `allow_unresolved` at the top of a `door:`/`sink:` rule, `mediation.allow_unresolved` in a long-form
  one. A `door:`/`sink:` rule now also takes `mediation.allow_unresolved` (following the old hint was
  refused as "not both" though it was given once); `door` or `allow` under `mediation` beside a
  top-level `sink:` says to move them to the top. An entry error in a top-level `allow_unresolved`
  names it as written (`allow_unresolved[0]: why must say ...`), at load.
- `inv check --base REF` marks each accepted unresolved site whose entry the lock at `REF` did not
  have (`[new since REF]`, and a count on the `ok scan` line).
- `inv status --ablation` says `stale (re-run)` for a report from another version of the rule (it
  said `no`), and for a door-only rule whose tests the report ran, what it says of them
  (`not needed (the report shows it load-bearing)`; JSON `ablation_report`).
- `inv check` (and `inv lock --check`) on a lock stale only because reviewed `allow_unresolved` sites
  moved, which weakens nothing, says to re-pin with `inv check --relock` instead of `review the
  changes, then run inv lock`.

### pytest plugin

- Installed with the package (entry point `pytest11`). Options: `--inv-rules`, `--inv-verify`,
  `--inv-ablate` (with `--inv-ablate-lenient`), `--inv-report` and `--inv-report-json`; fixtures
  `inv_registry`, `inv_ablated`, `inv_events` and `inv_scan`. With `--inv-ablate` or `--inv-verify`
  and no `--inv-rules` or `inv_rules`, it loads `invariants.yaml` (`.yml`, `.json`) from the rootdir;
  `--inv-ablate` with no rules loaded at all is a usage error (exit 4), never a green run; under
  pytest-xdist too (the controller checks a rule file before any worker starts, and exits 4 when the
  workers found no rules: it was exit 3, a worker's INTERNALERROR). With no rule file, rules the
  tests load themselves (a conftest's `inv.load`) count under `-n` as without it (the controller
  used to refuse them as no rules).
- `--inv-report-json` writes each rule's digest (`ablation.digests`: every field of the rule but its
  prose, id, rev, status and evidence). `inv status --ablation` and `inv.standing(ablation=...)`
  accept a rule's ablation results only from a report whose digest matches the rule as loaded now:
  a report for another version of the rule (its `bad`, enforcers or `falsified_by` changed since)
  or with no digest proves nothing (it used to say held, ablation proved it: yes). The digest covers
  the records of the providers the rule reads and of its targets too, so switching a provider from
  `py:app.cap_a` to `cap_b` makes an earlier report stale.
- `--inv-report` and `--inv-report-json` count the `abstain` outcome (a model call's abstain, below
  `enforce` or escalated at it, and an edge given a verdict `abstain`) next to the other four.
- Ablation re-runs each rule's `falsified_by` tests with the rule off: a falsifier is caught only when
  its rule decided something on a control run and the ablated run fails; everything else is
  reported as inconclusive with the reason. An enforcing edge's refusal of a received verdict that
  is not ok (`rule_error`, or a `Checked` `inv.model_call` did not build) is a decision of the rule
  (its event carries `refused: true`), so a falsifier asserting that refusal is caught; it was
  `inconclusive (rule_error, not violation)`, as was an escalated abstain next to any rule_error.
  It works under pytest-xdist and reaches subprocesses
  through `INV_ABLATE`. `INV_ABLATE` is read when a guard asks, not when a registry is built, so the
  default registry (built at import) honours it as the loader's log says.

### Integrations

- `integrations.pydantic`: closed reply schemas, the `pydantic` contract type, `model_of()` and
  `check_contracts()`.
- `integrations.fastapi` and `integrations.asgi`: route guards bound to the parsed parameters, and
  `application/problem+json` responses (422 reject, 202 route or escalate, 500 halt, 503 a
  fail-closed rule error) whose bodies never echo values. A `Response` a guarded handler returns
  (`JSONResponse(..., status_code=404)`) is sent as it is, never validated into the response model
  (it was a 500). It is opaque to `post_effect` rules: a clause that reads `result` is a `rule_error`
  ("the handler returned a Response") its `on_error` decides, never the Response's own attributes
  read as response-model fields (`result.status_code`, `body`, `headers`, `media_type` read the
  Response's).
- `integrations.otel`: one span per evaluation. `integrations.exporters.BufferedExporter`: a
  bounded, background exporter; a start() racing the flusher's exit after a timed-out close()
  starts a new flusher instead of leaving the events queued next with none.
- `integrations.anthropic`: Claude in a model-call body, with trace replay and recording for tests.

### Guarantees

- Nothing is checked below the API: rules name APIs, and what happens under the lowest one (SQL,
  triggers, transactions, isolation levels) is the implementation's business.
- A weakening of a pinned rule, schema, constraint, door or scan setting is never written to the
  lock, and never passes `inv check --base`, without a person accepting it.
- An unresolved call on a path to a sink, an unparseable reply and an unevaluable rule are never
  reported as a pass.
- An error about an object a target, contract or provider resolves to (no signature, no source to
  pin) names the object's type, never its repr, so its value never reaches `inv` or `inv mcp` output.

### Known limits

- Coverage is only as good as the rules: a defect no rule states is not caught.
- The scan is approximate: it types a receiver only when it is sure, does not follow dependency
  injection containers, event buses or callbacks, and cannot see callers outside the scanned roots.
- Door discovery in `inv init` is a heuristic over names and the store writes in function bodies;
  review what it proposes.
- The lock's weakening analysis is syntactic, and a behaviour change inside a function body does not
  change the lock.
- Rule text must stay within the CEL grammar both backends share; some functions exist on one
  backend only.
- Every provider bound to a name a clause reads runs on every call. A check on an API's arguments is
  not a lock: two concurrent callers can both pass.
