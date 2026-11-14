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

### Run time

- `@inv.guard("ID")` checks a target's rules before (`pre_effect`) or after (`post_effect`) the
  call, sync or async; outcomes are `ok`, `violation`, `rule_error` and `parse_error`, a rule error
  is never a violation, and `on_error` decides it. Errors (`Rejected`, `Halted`, `Escalation`,
  `Routed`) never carry the values a rule read.
- `@inv.model_call` parses a model reply into a closed schema (a dataclass, a `{field: type}` map or
  a pydantic model with `extra="forbid"`), retries a format failure with the parser's message,
  resamples a reply that breaks a rule without showing the model the rule (never when the rule
  compares the reply with held data: `compares_to: held_data`), and turns an abstention into a
  review. It returns an `inv.Checked` value.
- `@inv.edge` on the consumer of a reply re-validates the schema and re-checks `bad` against state as
  it is now (`inv.Checked` is forgeable, so the edge never trusts it).
- Providers are read at the enforcer's timing; `old_self` rules compare a target's state before and
  after the call.
- Registries: `inv.load()` installs a rule file in the current registry; `inv.isolated()`,
  `inv.using()`, `inv.capture()` and `inv.observe()` cover tests, and `inv.configure()` the
  production wiring (the event sink's size, the review store, a pager, an exporter, an error-spike alarm). Hand-offs go to a review store (`MemoryReviews`,
  `JsonlReviews`, or your own `ReviewStore`).
- Standing is computed, not claimed: `inv.standing()` gives `held` only when the rule is enforced,
  its chokepoint is named, no residual path is listed, and verify and the scan pass; otherwise
  `enforce(known-callers)`, `monitored` or `candidate`, with the reasons.

### CEL

- Two backends behind one front end, so a rule parses the same everywhere: `subset` (the default,
  standard library only, with a cost budget) and `cel-expr` (Google's C++ runtime, from the `cel`
  extra). `INV_CEL_BACKEND=subset|cel-expr|auto` or `backend=` picks one; `auto` prefers cel-expr.
- The extensions `inv.sum`, `inv.zip` and `inv.unique_by`; timestamps and durations; NaN and
  Infinity are rule errors unless allowed. The shared conformance suite runs against every
  installed backend.

### The lock

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
  a module a scanned package does not hold is `unresolved/unknown-module`, never third-party. A
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
- Reviewed exceptions: `mediation.allow_unresolved` (short form `allow_unresolved:`) accepts an
  unresolved finding a person has read, one site at a time: `{at: "path:line[:col]", code, sha,
  why}`, where `sha` (in `--format json`) hashes the finding's code, function and source line. An
  accepted finding is shown (`accepted` in text, JSON, a SARIF suppression with the `why`, `inv
  status`, MCP `explain`) and never fails the rule; an edit above the site keeps the entry (reported
  as moved), an edit of the site does not; an entry that matches nothing is
  `missing/allow-unresolved-unused`. `bypass` and `forbidden/*` findings can never be accepted. A new
  entry, or one that drops its column, is a lock weakening (`--accept-weakening`); removing one is a
  tightening.
- Narrowing the scan can prove: a `getattr` with a computed name on an instance (or the class) of a
  class whose whole family the scan sees, where no attribute is named like a sink or door and none
  can reach a sink without a door, or on an object outside the scanned code (`getattr(logging,
  level)`), is not reported; reading only a namespace's names (`globals().keys()`, `name in
  globals()`) is not reflection. Reflection elsewhere is still reported wherever it is (a module
  that imports nothing can still be handed the sink). On jupyter_server's session door this, with
  the unwrap change below, took 1 forbidden and 19 unresolved findings to 13 unresolved; on
  google-adk's credential door 2 forbidden and 76 unresolved to 69 unresolved (besides 258
  imports of `google.*` packages the roots lack).
- `forbidden/unwrap` ("calls a guarded function without its guard") is reported only when what is
  unwrapped can be a guarded door (a door or target of a rule, or a function with an inv guard);
  any other unwrap that bears on the rule is `unresolved/unwrap`, and `hasattr(f, "__wrapped__")` or
  an unwrap of a standard-library or third-party object is not reported.
- A write of a package's `__path__` (`__path__.append`, `pkgutil.extend_path`) is
  `unresolved/reflection`: its submodules may come from any directory. `sys.path` changed through a
  name bound to it (`p = sys.path; p.insert(0, d)`) is reported too. Name-matched alternatives are
  followed in sorted order, so a scan gives the same paths on every run.

- A function used as a value is an entrypoint: one that can reach a sink without passing a door (a
  door's own helper included) and is taken as a value outside the doors and allowed functions (a
  thread target, a callback, a dict entry, an alias, a bound method) is reported where it escapes; an
  allowed back door taken as a value is a new caller, a `bypass`. A name bound to several functions
  (a conditional import, a star import re-exporting another one) calls each of them.
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
- `inv verify` imports the app and checks that every target carries its guard and every guard
  names a rule.
- `inv status` reports each rule's standing with its reasons.

### CLI

- `inv init` drafts `invariants.yaml` from the laws the code already has (constrained pydantic
  models as contracts, service-to-repository door rules with today's back doors frozen in `allow`,
  and function preconditions translated to CEL, written commented out), then locks it. It never
  overwrites a rule file, and reads a Graphify `graph.json` for door hints (`--graph`); every
  proposed door is checked by the scan.
- `inv check` runs validate, `lock --check`, verify and scan with one exit code. `--relock` re-pins
  when nothing weakens (`--fail-on-repin` then exits 1 so the new lock is staged); `--base REF` also
  fails on a weakening since the lock at REF, unless `--allow-weakening` or `INV_ALLOW_WEAKENING=1`,
  and fails closed when REF or its lock cannot be compared (a REF with no lock is a first adoption).
- `inv hook pretool|stop`: Claude Code hooks that refuse a command accepting a weakening, a direct
  edit of `invariants.lock` or of the scan settings in `pyproject.toml`, a command that rewrites
  either without naming it (a project script whose text names it, `git apply`, `git stash pop`,
  `git checkout REF -- .`, `git restore --source`), an edit of `.claude/settings.json` or
  `.claude/settings.local.json` (the hooks' own configuration), and finishing while `inv check`
  fails. They are an early warning; `inv check --base` in CI is the gate.
- `inv mcp` and `inv-mcp`: a read-only MCP server over stdio (`scan`, `check`, `status`, `explain`,
  `propose_rules`); it never writes the rule file, the lock or `allow`, and never reads a file or
  imports a module (the standard library and installed packages included) outside its `--root`
  for a rule file inside it.
- `inv validate`, `inv lock`, `inv verify`, `inv scan`, `inv status`, and `inv --version`.
- Exit codes: 0 ok, 1 a check failed, 2 usage error, 3 the scan has unresolved calls, 4 the scan
  skipped an ablated `enforce` rule (3 and 4 are not passes).
- A GitHub Action (`action.yml`) and a pre-commit hook (`.pre-commit-hooks.yaml`) at the repository
  root.

### pytest plugin

- Installed with the package (entry point `pytest11`). Options: `--inv-rules`, `--inv-verify`,
  `--inv-ablate` (with `--inv-ablate-lenient`), `--inv-report` and `--inv-report-json`; fixtures
  `inv_registry`, `inv_ablated`, `inv_events` and `inv_scan`.
- Ablation re-runs each rule's `falsified_by` tests with the rule off: a falsifier is caught only when
  its rule decided something on a control run and the ablated run fails; everything else is
  reported as inconclusive with the reason. It works under pytest-xdist and reaches subprocesses
  through `INV_ABLATE`.

### Integrations

- `integrations.pydantic`: closed reply schemas, the `pydantic` contract type, `model_of()` and
  `check_contracts()`.
- `integrations.fastapi` and `integrations.asgi`: route guards bound to the parsed parameters, and
  `application/problem+json` responses (422 reject, 202 route or escalate, 500 halt, 503 a
  fail-closed rule error) whose bodies never echo values.
- `integrations.otel`: one span per evaluation. `integrations.exporters.BufferedExporter`: a
  bounded, background exporter.
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
