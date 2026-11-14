# Security policy

## Supported versions

Security fixes go into the latest released minor version.

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub's "Report a vulnerability" button (private vulnerability reporting) on this repository. Include:

- the affected version, and the CEL backend if one is involved
- a minimal rule file and code that reproduce the problem
- what you expected to happen, and what happened instead

We aim to acknowledge a report within 5 working days and to agree a disclosure date with you.

## Scope

These are security issues:

- A way for rule text, or a value reaching a rule, to execute code. The evaluator must never `eval` or `exec`.
- A way past a guard in enforce mode that the documentation does not list as a limitation.
- HTTP problem responses leaking error text, provider failures or evaluated values.
- A rule escaping its cost budget on the `subset` backend, or the fixed comprehension
  iteration budget (9,999 per clause) on `cel-expr`.
- A model reply that makes `@inv.model_call` raise anything other than its documented outcomes
  (`Rejected`, `Halted`, `Escalation`, `Routed`, `ReviewNotOpened`) or a returned `Checked`, or makes
  `inv.parse` raise anything other than `ParseError`.
- A value that passes an `@inv.edge` while it does not fit the rule's closed schema.
- A NaN or ±Infinity that gets past a clause in enforce mode with the default
  `allow_non_finite=False`: in a model reply, pydantic schemas and strings such as `"NaN"` included
  (`inv.parse` must refuse it, and `@inv.model_call` must never return verdict ok holding one), in a
  value a rule reads, or made by the clause itself (`0.0 / 0.0`, an overflow, `double("NaN")`): the
  clause must be a `rule_error`, on every backend.
- A value a rule reads becoming a different value on its way to CEL: a naive datetime read as if it
  were in some timezone, a `Decimal` rounded to a double, a `date` turned into a timestamp. The
  mapping in `invariants/cel/values.py` refuses these (a `LoadError` or a `rule_error`), on every
  backend.
- A pydantic reply schema accepting a value a strict schema refuses (`"0.5"` or `true` for a float)
  unless its `model_config` sets `strict=False`.
- A request value (a map key, a regex pattern, a string) appearing in `rule_error` text, events,
  exception messages or logs while debug is off.
- A model reply value, or a key the reply made up, appearing in `parse_error` text in exceptions,
  `Outcome.error`, attempt records, events or logs while debug is off. (It is meant to reach the
  model, through `ParseError.feedback` and `inv_feedback`.)
- An exception message from user code (a property, a mapping lookup, a provider) appearing in
  outcome errors, events or logs while debug is off (`INV_DEBUG` unset, `inv.configure(debug=False)`).
- A way for `inv mcp` (or `inv-mcp`) to read a file or import a module outside its `--root`,
  through a symlink included, to write the rule file, the lock or `allow`, or to put an object's repr
  in its output.
- A route or escalate whose review is silently dropped: a store failure must raise `ReviewNotOpened`
  carrying the hand-off, or reach the configured `review_fallback`.
- A way for code imported by `inv check`, `inv lock`, `inv validate`, `inv init` or the stop hook to
  change whether a change is judged WEAKENED, other than by describing its own signatures and
  schemas: those commands import the app only in a child process, and the process that decides
  never imports the project and reads the rule file, the locks and the scanned sources before the
  child runs.

The trust boundary of the lock: it trusts the code it imports to describe itself. A module that
reports a false signature or schema at import (or rewrites the installed tools for a later CI step)
is outside what the lock can check; that is why the guarded, contract and model modules and the CI
workflow need a code owner, like the rule file and the lock (docs/start.md, docs/lockfile.md "What
the lock trusts"). The same holds for the pull request's code that runs in the CI job before the
gate: installing the project runs its build backend, `setup.py` or console scripts. The bundled
GitHub Action runs invariants from a venv of its own, made before anything is installed, as
`python -I -m invariants.cli` (never the `inv` on `PATH`), and `python -m invariants.cli` drops the
working directory from `sys.path`; a way for a pull request to change the verdict of that action
without its code running before the gate (for example through a file name, which `inv` prints with
control characters and a leading `::` escaped) is in scope, code run by installing the project in
the gate's job is not (docs/start.md).

Operational hazards, by design:

- `INV_ABLATE` together with `INV_ABLATE_TESTING=1` turns the named rules' in-process guards off in
  that process. The pytest plugin sets both for falsifier runs and their subprocesses (an `inv scan`
  or the app), so the SDK cannot refuse it outside pytest; it logs at ERROR instead, and when the
  `invariants` logger has no real handler (the app configured no logging) it also writes that line
  to stderr. `PYTEST_CURRENT_TEST` alone does not count as a pytest run. Never set
  `INV_ABLATE_TESTING` in a deployed environment, and alert on the `invariants` logger's ERROR records.
- Rule-error spike demotion (`inv.configure(error_spike=...)`) is off by default. When on, enough
  rule errors switch a rule to detect; if clients can cause rule errors, they can use it to turn
  enforcement off.

- On the `cel-expr` backend the C++ RE2 library writes an invalid regular expression, which can be
  request data when a clause matches against a pattern taken from the request, straight to the
  process's stderr (and absl may print a logging banner there). The library cannot redirect it.
  Do not take `matches()` patterns from request data on `cel-expr`, or treat its stderr as
  holding request data.
- `inv.configure(allow_non_finite=True)` lets rules read and make NaN as an IEEE number (including
  `0.0 / 0.0` in the clause); a bound written as `x < lo || x > hi` is then false for NaN. Use
  `!(x >= lo && x <= hi)`.
- A pydantic model with `strict=False` in its `model_config` gets pydantic's lax coercion for
  model replies (`"0.5"` becomes `0.5`); non-finite values are still refused.

These are known limitations, not vulnerabilities: a forged `inv.Checked` whose value fits the closed schema and passes `bad` (an `@inv.edge` re-validates and re-checks both, and can check no more), work outside comprehensions on `cel-expr` (linear in the input, not budgeted: bound untrusted input sizes), and the call-graph approximations of `inv scan`. All are documented in the README and the pages under `docs/`.

Operational note: with `debug=True` (or `INV_DEBUG=1`) exception messages, which may hold secrets, go into outcome errors, events and logs. Use it only while debugging. `inv.JsonlReviews` writes the values a rule read (arguments, replies) to its file; protect that file as you protect the review store.
