# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned as 0.1.0, the first release. Publishing metadata (the final package name, `project.urls`,
authors, PyPI trusted publishing) is not settled yet: see "Before publishing" in
[RELEASING.md](RELEASING.md). Contributors add their entries here.

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
  now matches `type: int` instead of being refused.
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
  refused, as in RE2.
- `int()`, `uint()` and `double()` of text take ASCII whitespace around the number only, as absl
  does on cel-expr: `int("500\u00a0")` was `500` on the subset and is a `rule_error` on both, and
  `"1\u001c"` no longer escapes as a Python `ValueError` (every conversion failure is a CelError).
- An even run of `!` or unary `-` (`!!args.s == "x"`, `--args.i`) checks its operand on cel-expr:
  cel-cpp's parser drops such a run unchecked, so the front end sends that clause re-printed
  (`!(!(args.s))`); it was `true` on cel-expr and a `rule_error` on the subset.

### The lock

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
  (no property, descriptor or `__setattr__` in its family) is narrowed like a computed `getattr`; an
  unwrap of an unknown object is reported only when a decorated function (or a method named
  `__wrapped__`) can reach the sink, and an unknown `fn.__closure__` read is `unresolved/unwrap`, not
  `forbidden/unwrap`. The sha ignores black's magic trailing comma (not a one-element tuple's), so the
  sha of a statement written with one changed once; a moved `allow_unresolved` entry with a column is
  told its new column. Shas are worked out only when shown (JSON, SARIF, a rule with
  `allow_unresolved`), from the index's own parsed trees. `inv init` counts back doors and unresolved
  sites apart, and lists the unresolved ones for review as drafted `allow_unresolved` entries (an
  empty `why` the rule file refuses until a reviewer writes it) instead of dropping the draft. A
  relative import naming no module is reported under the indexed package name.
- `inv verify` imports the app and checks that every target carries its guard and every guard
  names a rule.
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

- `inv init` says when it wrote no path law (so `inv check` will skip its scan), and when its
  `--root`/`--exclude` differ from the `[tool.invariants.scan]` settings `inv check` will read.
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
  it; `git apply`, `git stash pop`, `git checkout REF -- .`, `git restore --source`), an edit of
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
  root.

### pytest plugin

- Installed with the package (entry point `pytest11`). Options: `--inv-rules`, `--inv-verify`,
  `--inv-ablate` (with `--inv-ablate-lenient`), `--inv-report` and `--inv-report-json`; fixtures
  `inv_registry`, `inv_ablated`, `inv_events` and `inv_scan`. With `--inv-ablate` or `--inv-verify`
  and no `--inv-rules` or `inv_rules`, it loads `invariants.yaml` (`.yml`, `.json`) from the rootdir;
  `--inv-ablate` with no rules loaded at all is a usage error (exit 4), never a green run; under
  pytest-xdist too (the controller checks before any worker starts: it was exit 3, a worker's
  INTERNALERROR).
- `--inv-report-json` writes each rule's digest (`ablation.digests`: every field of the rule but its
  prose, id, rev, status and evidence). `inv status --ablation` and `inv.standing(ablation=...)`
  accept a rule's ablation results only from a report whose digest matches the rule as loaded now:
  a report for another version of the rule (its `bad`, enforcers or `falsified_by` changed since)
  or with no digest proves nothing (it used to say held, ablation proved it: yes).
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
