# Start in 10 minutes

Every layer of a system is an API: the HTTP route, the service method, the repository method, the
model call, the external client (Stripe, say). Pydantic and OpenAPI define the *shape* of those APIs.
The rule file defines their *laws*, and there are only two kinds:

- **a value law:** on this API, these inputs or outputs are never allowed (a pydantic limit, or a
  `bad` condition on `args` and `result`);
- **a path law:** this lower-level API may only be called through that upper-level one ("only
  `BillingService.refund` may call `BillingRepo.save_refund`").

People own the laws at the boundaries, and coding agents are free inside the implementations. The
rules never look below an API: not at SQL, triggers, transactions or private fields.

Adoption comes in two steps, and the first one changes no production code.

- **Step 1: a CI and agent check.** Install the package as a dev tool, like ruff or pytest, and never
  import it in your app. `inv init` locks the laws your code **already has**:
  - **value laws in your models:** pydantic `Field(gt=0, le=10_000)`, `max_length`, `Literal[...]`,
    required fields, including SQLModel models;
  - **path laws between your layers:** for each repository (a Repo or Store class, a store module, a
    Stripe-style gateway), the service methods that call it, with the back doors that skip them today
    listed and frozen, so a new one fails CI. It works the same over SQLAlchemy, Django, MongoDB,
    DynamoDB, an HTTP client or raw SQL.
- **Step 2: value laws at run time, only where you want them.** Once the check has earned trust, put
  `@inv.guard` on the few service methods that move money or spend quota (section 5).

## 1. Draft and lock (2 minutes)

```bash
pip install invariants-first        # a dev dependency; not on PyPI yet: pip install -e path/to/invariants-py
cd your-project
inv init
```

`inv init` reads your source code without importing it. It writes `invariants.yaml` and runs
`inv lock`. It never overwrites an existing rule file. `inv lock` does import the modules that hold
your pydantic models, to read their JSON Schema, so run it where your project's dependencies are
installed.

```text
wrote invariants.yaml
  read 3 source file(s)
  pinned 1 pydantic model(s) with 5 constraint(s)
  suggested 2 rule(s) from function preconditions (commented out; see the file)
  found no door (no function that two or more callers go through to a write)
running `inv lock` to pin them ...
wrote invariants.lock: 0 rule(s), 0 target(s), 0 provider(s), 1 contract(s), 1 schema(s)
```

The rule file it writes looks like this:

```yaml
contracts:
  # charge_id: min_length=1, charge_id: max_length=64, amount: gt=0, amount: le=10000, +1 more
  refund_request: {type: pydantic, ref: "billing.models:RefundRequest"}

rules:
#  - id: REFUND
#    guard: "py:billing.payments.refund"
#    bad:
#      - {when: "args.amount <= 0", message: "refund must be positive"}
#      - {when: "args.refunded + args.amount > args.charged", message: "refund exceeds what is left of the charge"}
#    tests: []
```

Review the file. Delete any contract or rule you don't care about, then commit both
`invariants.yaml` and `invariants.lock`. If `inv lock` fails, it is usually a model `ref` that doesn't
import from the project root. Fix the ref, or pass `--path DIR`, then run `inv lock` again.

## 2. Value laws: what the lock catches

An agent raises a limit so its failing test passes:

```text
$ inv check
inv check: invariants.yaml
  ok    rules: 0 rule(s), 2 contract(s) load
  FAIL  lock
        stale: invariants.lock does not match invariants.yaml: 1 change(s), 1 WEAKENED
          schema billing.models.RefundRequest: pydantic.properties.amount.maximum: 10000 -> 100000  WEAKENED (maximum raised: larger values pass; used by contract refund_request schema)
        review the changes, then run `inv lock --accept-weakening`
fail
```

These changes count as a weakening, because the API now accepts something it refused:

- a raised maximum or a lowered minimum, or either one removed;
- a new enum value;
- a field that is no longer required, or a field removed;
- a changed `pattern`, `format` or type.

A change that only tightens the rules passes, and so does every other edit to the code.
Running `inv check --relock` re-pins the lock by itself when no change weakens anything, so the
lock never needs hand maintenance.

## 3. Path laws: doors and back doors

Most codebases already have a service layer: eight endpoints go through
`ExpenseService.refund()`, which checks the amount before it calls the payment gateway. The weak spot
is the one support script that calls the gateway directly. `inv init` finds both:

- a **sink** is a repository write: a write method of a Repo, Store, Gateway or DAO class (or a write
  function in a `repo`/`store`/`db` module), or a function that writes through an ORM session, a
  Django manager, a Mongo collection, a DynamoDB table, a client's `create` call or raw SQL. Reads
  (`get`, `current`, `select(...)`) are not sinks;
- a **door** is a service-layer caller of a sink (a method of a `…Service` or `…Manager` class, or a
  function in a `service` module), or, where there is no service layer, the one caller most other
  functions go through;
- a **back door** is any other caller of a sink.

It proposes one rule per repository, runs the real scan on it, and freezes what it finds. A back
door is frozen with its whole call chain: if `admin_goodwill` calls `goodwill_refund`, which calls
the gateway, both go in `allow`. A rule that still fails with its back doors frozen (for example,
when the finding is inside the door itself, which `allow` cannot freeze) is written commented out,
with the reason. This is
real output from an app whose repositories use SQLAlchemy, a Django-style manager and Stripe, with
no SQL anywhere:

```text
$ inv init
  ...
  door REFUND-GATEWAY-DOOR: quota.repo.RefundGateway through quota.service.ExpenseService.refund (1 caller(s))
    found 1 back door(s) in scripts/admin.py; frozen in allow, so check passes and a new one fails
  door EXPENSE-STORE-DOOR: quota.repo.ExpenseStore through quota.service.ExpenseService.approve (1 caller(s))
    found 1 back door(s) in scripts/admin.py; frozen in allow, so check passes and a new one fails
  door USAGE-REPO-DOOR: quota.repo.UsageRepo through quota.service.QuotaService.consume or quota.service.QuotaService.new_month (2 caller(s))
    found 1 back door(s) in quota/worker.py; frozen in allow, so check passes and a new one fails
```

Each rule is a few lines, plus the back doors it found:

```yaml
  # quota.repo.RefundGateway: written by quota.repo.RefundGateway.refund
  #   1 caller(s) go through quota.service.ExpenseService.refund
  # 1 back door(s) frozen in allow (fix each one, then delete it from allow):
  #   scripts/admin.py:13 scripts.admin.goodwill_refund -> quota.repo.RefundGateway.refund reaches the sink without passing quota.service.ExpenseService.refund
  - id: REFUND-GATEWAY-DOOR
    door: "py:quota.service.ExpenseService.refund"
    sink: "py:quota.repo.RefundGateway.refund"
    allow:
      - "py:scripts.admin.goodwill_refund"
```

There is no decorator, and nothing runs in production: `inv check` reads the source. Today's code
passes, because the known back doors are frozen in `allow`. The next one fails:

```text
$ inv check   # after an agent adds scripts/refund_tool.py, which calls the gateway directly
  FAIL  scan
        bypass     REFUND-GATEWAY-DOOR scripts/refund_tool.py:4:5  scripts.refund_tool.refund_customer -> quota.repo.RefundGateway.refund reaches the sink without passing quota.service.ExpenseService.refund
        REFUND-GATEWAY-DOOR: fail (1 bypass)
```

The check fails when new code calls a sink without passing a door, whichever way it is written:
directly, through an alias or a module object, a constructor (`Repo(session).add(...)`), a local
variable, an annotated parameter (also `Annotated[Repo, Depends(get_repo)]` and an alias of it),
`await get_repo()`, `with Session() as repo`, `self.repo` (set in `__init__`, also by tuple
assignment, or a `@property` annotated `-> Repo`), `super().save()`, or a subclass of the repository
(a generic one, `class SqlRepo(BaseRepo[T])`, too). It also fails on a call hidden with
`getattr(repo, "save")`, on the sink passed as a value (`add_task(repo.save)`), on code that assigns
over a sink or the class or module that holds it, on `sys.modules` lookups, and on `__wrapped__`.
Each call site is its own finding, so two direct calls in one script are two lines. A call on a receiver the scan can't be sure of (an
unannotated parameter, say) is reported as unresolved, which also fails: the scan never guesses that
a path is safe. Reads through the repository and unit tests that call it directly are not back doors.
The scan skips `tests/`, `test_*.py`, `*_test.py`, `conftest.py` and migrations (`migrations/`,
`alembic/`), at any depth, unless `[tool.invariants.scan] exclude` in `pyproject.toml` says otherwise.

The doors are pinned by name. A new service method that calls the repository without passing the
door that checks the rule (a `bulk_refund` that skips `refund`) is a back door too. If it is a real
door, add it to `door:`; the lock reports that as a weakening, so a person accepts it. The lock also
reports adding an `allow` entry and deleting the rule as weakenings. When you fix a back door, delete
its `allow` entry: that is a tightening and needs no one's approval. An `allow` entry that no longer
matches a back door fails the check until you delete it, so a regression in that function is caught
again.

`allow` is not a door. A path to the sink passes only when every function on it is in `allow`, so a
new caller of an allowed function, or new code inside one that reaches the sink, is a bypass.

How `inv init` finds them. A function is a repository write (a sink) when its class or module is
named like a repository (`Repo`, `Store`, `Gateway`, `DAO`, `repo`, `store`, `crud`, `db` ...) and
its name is a write verb, or when its body writes through a store client: an ORM session, a Django
manager, a Mongo collection, a DynamoDB table, a client's `create` call, or raw SQL. A receiver is a
store client by its name (`session`, `db`, `conn` ...) or by its annotation, whatever it is called:
`s: Session`, `db: AsyncSession`, or an alias such as `SessionDep = Annotated[Session, Depends(get_db)]`
imported from another module. The doors are the callers in a service layer (a module or class named
like one), else a function called only by HTTP route handlers (`@router.post`, `@app.get` ...) that
sits between them and the write, even when one route calls it, else the caller most functions go
through. On the full-stack FastAPI template this still writes no door rule, correctly: its routes
call `crud` and write through the session themselves, so there is no layer to lock yet.

At most five door rules are written, money, quota and approval names first (`--max-doors N` changes
it); the rest are listed in the file. `--no-doors` skips this step.

**Graphify (optional).** If the project has a Graphify
graph at `graphify-out/graph.json`, `inv init` reads it as JSON (`--graph PATH` or `--graphify PATH`
names another file, `--no-graphify` turns it off). Its communities help find doors where nothing is
named Service: a caller of a write is a door candidate when it sits in its own community, every one
of its callers sits in a third community (so it is a layer between two others), and it is not an
entry point (`main`, a route handler). Its call links count as callers only when Graphify tagged them `EXTRACTED` (parsed from
the source), never `INFERRED` or `AMBIGUOUS`. Every rule it suggests is still checked by the real
scan before it is written, and `inv check` never reads the graph. Measured on five codebases, the
graph added one door and no rule the heuristics missed: its communities follow call chains rather
than layers ([evaluation](evaluation.md)).

Graphify also ships an MCP server, which lets a coding agent query the graph while it works (the
shortest path from a new tool to the gateway, what a change touches). It is separate from `inv`;
to use it, `pip install 'graphifyy[mcp]'` and add it to the project's `.mcp.json`:

```json
{"mcpServers": {"graphify": {"command": "graphify-mcp", "args": ["graphify-out/graph.json"]}}}
```

This package ships a second, read-only MCP server, `inv-mcp`, over the laws themselves (scan, check,
status, explain, and `inv init`'s proposals as a dry run). [agents.md](agents.md) shows both in one
`.mcp.json` and the flow they support: the agent proposes, a person approves and locks.

## 4. Make the check unskippable (5 minutes)

The lockfile alone isn't enough, because an agent can run `inv lock --accept-weakening` itself.
Three layers close that gap. Each one is a single copy and paste.

**pre-commit.** Add this to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: inv-check
        name: invariants check
        entry: inv check --relock --fail-on-repin
        language: system
        pass_filenames: false
        always_run: true
```

When nothing weakens a rule but the lock is stale (a tighter cap, a new rule), the hook re-pins
`invariants.lock` and fails the commit once with "invariants.lock re-pinned; review and
`git add invariants.lock`", so the new lock is reviewed and goes into the same commit. A
weakening fails the commit and leaves the lock alone.

It uses `language: system` because `inv check` imports your app to pin its models, so it must run
in your project's environment. You can also point `repo:` at this repository and use `id: inv-check`;
the hook is defined in `.pre-commit-hooks.yaml` at the repository root.

**CI: compare against the base branch.** `inv check --base origin/main` compares the rules with
the lock committed on `main`, not with the lock in the pull request. A weakening that the pull
request re-pinned itself still fails. The check fails closed: a base ref that does not exist (not
fetched, a typo), a checkout that is not a git repository, or a lock on the base that this
invariants cannot read or compare (damaged, or an older or newer lock version) fails the step, and
so do a lock replaced by a symlink and a base lock the pull request moved or removed. Only a base
with no `invariants.lock` at that path (and none moved away) passes, as a first adoption (the whole
lock is reviewed in that pull request). A reviewer can accept a base lock that cannot be compared with the same
`--allow-weakening` or `INV_ALLOW_WEAKENING=1` that accepts a weakening; nothing accepts a missing
base ref.

```text
$ inv check --base main
  FAIL  since main
        1 change(s) since main weaken a rule:
          schema billing.models.RefundRequest: pydantic.properties.amount.maximum: 10000 -> 100000  WEAKENED (...)
        a person must accept these: re-run with --allow-weakening or INV_ALLOW_WEAKENING=1, set by a reviewer ...
```

On GitHub, use the bundled action after installing your project:

```yaml
on: pull_request
jobs:
  invariants:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13"}
      - run: pip install -e . invariants-first
      - uses: jkuru/invariants-first@main    # action.yml at the repository root; pin a tag once released
        with:
          accept-label: accept-weakening     # a reviewer adds this label to accept a weakening
```

The action fetches the base branch and fails with an error if the fetch fails or the ref is still
missing afterwards, rather than skipping the comparison.

Labels can be added by anyone with write access, and that includes an agent holding a token. Also
make a person own the rule files with a `CODEOWNERS` line, and require code-owner review on `main`:

```text
invariants.yaml  @your-org/payments-owners
invariants.lock  @your-org/payments-owners
```

**Claude Code.** Add this to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "inv hook pretool"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "inv hook stop"}]}
    ]
  }
}
```

- **`inv hook pretool`** blocks any shell command containing `--accept-weakening`,
  `--allow-weakening` or `INV_ALLOW_WEAKENING`, or an abbreviation of either flag of five
  characters or more (`--accept-w`; `inv` itself takes no abbreviated options). The agent is told
  to report the weakening to you instead.
  It also blocks any direct edit of `invariants.lock`: an edit or write tool on it (symlinks are
  resolved first), or a shell command that names it and is not clearly read-only. The command is
  split into words as the shell would, so `"invariants".lock`, `invariants\.lock` and globs such as
  `inv*.lock` or `*.lock` name it too. Read-only means `cat`, `less`, `head`, `tail`, `grep` and
  the like, `git diff|show|log|status|blame`, and `inv check|lock --check|status|scan|validate`, with
  no redirect into a file, no `--output`, `--output=` or `-o`, no `git -c` and no `GIT_*` variable
  before `git` (a pager or external diff runs code). `git add invariants.lock` is allowed too, so
  `inv lock && git add invariants.lock` is fine: only `inv lock` and `inv check --relock` write the
  lock. A command the hook cannot read is blocked when it mentions `lock` or `pyproject`: one that
  does not parse, or uses `$(...)`, backticks, `${...}`, `eval`, `sh -c`, `xargs` or `python -c`;
  a writing command with an expanded name (`$f`) is blocked when the command mentions the lock,
  `invariants`, `pyproject` or `toml`. The hook also blocks an edit that changes the
  `[tool.invariants]` tables of `pyproject.toml` (the scan roots and excludes) and any shell
  command that writes `pyproject.toml`, by the same rules.
- **`inv hook stop`** runs `inv check --relock` when the agent tries to finish. If the check fails,
  it sends the agent back with the failure, once per stop, so it can't loop.

Both commands read Claude Code's hook JSON on stdin, and both block by exiting with code 2.

The hooks are an early warning against an agent's shortcuts, not the gate. A text check of a shell
command can always be talked past (a script file that writes the lock, a name read from a file),
and an agent may run without hooks. The gate is `inv check --base` in CI, with the rule files owned by a
person: it compares with the lock on the base branch, whatever happened on the way.

For other agents, put this in `AGENTS.md`, and rely on pre-commit and CI to enforce it:

```markdown
Run `inv check` before you finish. If it reports a WEAKENED change, fix the code so it keeps the
rule. Never run `inv lock --accept-weakening` or set INV_ALLOW_WEAKENING; tell the user which rule
your change weakens and why.
```

## 5. Step 2: a value law on one API (when you want one)

`inv init` turns `if ...: raise` and `assert` preconditions into suggested rules. A suggested rule
keeps the check even if a later edit deletes the `if`. To turn one on:

1. Uncomment the rule.
2. Put `@inv.guard("REFUND")` on the function, with `import invariants as inv`, and call
   `inv.load("invariants.yaml")` once at startup.
3. Write a test that calls the function with bad input and expects `inv.Rejected`. List it under `tests:`.
4. Run `inv lock`.

```yaml
rules:
  - id: REFUND
    guard: "py:billing.payments.refund"
    bad:
      - {when: "args.amount <= 0", message: "refund must be positive"}
      - {when: "args.refunded + args.amount > args.charged", message: "refund exceeds what is left of the charge"}
    tests: [tests/test_payments.py::test_over_refund_is_rejected]
```

That is the whole short form:

- `guard` names the function, and the rule checks its arguments before it runs.
- `tests` lists the proof.
- Until a rule names a test, it only logs. Once it names one, it blocks with `inv.Rejected`.
  `pytest --inv-ablate` then checks that the test fails when the guard is switched off.
- Deleting `tests:`, removing the decorator or loosening `bad` all fail `inv check`.

Only conditions over the function's parameters are translated: comparisons, `and`, `or`, `not`,
arithmetic, `len`, `is None`, and `in` over a literal list. A parameter tested for truth is
translated by its annotation, because CEL has no truthiness: with `name: str` (or bytes, a list, a
dict or a set), `if not name: raise` becomes `size(args.name) == 0`; with `ok: bool` it is
`!args.ok`; with `name: Optional[str]` (or a `None` default) it is
`args.name == null || size(args.name) == 0`. A truth test on anything else (an unannotated or `int`
parameter, a call) is not translated. Anything else, such as division (Python
and CEL divide differently) or a helper call, is listed as not translated rather than guessed. A
module-level constant is inlined: with `MAX_REFUND = 10_000`, `if amount > MAX_REFUND: raise`
becomes `args.amount > 10000`, and with `CURRENCIES = ("usd", "eur")`,
`if currency not in CURRENCIES: raise` becomes `!(args.currency in ["usd", "eur"])`. So raising the
constant later is a change to the rule file. A constant is an int, float, str, bool or None, or a
tuple, list or set of them. A name that is bound more than once, rebound with `global`, imported, a
loop variable or anything else is not translated.

A door rule and a guard rule combine: give a rule both `guard:` and `sink:`, and the
guarded function is the door. The rule then checks the arguments at run time and the scan proves no
path skips it.

## 6. When to go further

Everything else is optional, and you can add it rule by rule:

- value laws on model calls (`@inv.model_call`) and the tool calls an agent makes;
- `result` checks on a service method (`timing: post_effect`);
- providers that read current state for a rule (`provider.pool_leased`);
- `old_self` transition rules.

Start with the [README quickstart](../README.md#quickstart) and [concepts](concepts.md). The short
form and the long form pin to the same lock, so switching a rule from one to the other isn't a
change.

Earlier versions also had rules below the API (pinned `.sql` constraints, `tables:`, storage
enforcers, `transactional` effects, provider consistency levels). They were removed. A rule file
that still uses one fails to load with a message that says what to do, and an old lock shows each
removed pin once as a weakening (see [lockfile.md](lockfile.md#upgrading-from-a-release-with-rules-below-the-api)).

## Limits

- **Sinks are found by name and by the writes in their bodies.** A repository whose class or module
  name gives no hint (not Repo, Store, Gateway, DAO, `repo`, `store`, `db` ...) and whose writes go
  through a client the list doesn't know is not found. Write that rule by hand: name the repository
  method as the `sink` and the service method as the `door`. Discovery reads function bodies only to
  find sinks; the check itself is only about which API calls which.
- **The chosen door is a heuristic.** Service-layer callers are doors; without a service layer, it is
  the caller most other functions go through. Check each rule, and check that what `allow` calls a
  back door really is one.
- **The scan is static.** It does not follow calls through dependency injection containers, event
  buses or callbacks. Where it can't tell, it reports unresolved instead of passing.
- **Validators are code, so they aren't pinned.** A pydantic `@field_validator` or `@model_validator`
  isn't part of the JSON Schema. `inv init` notes each model that has validators. To protect one,
  write it as a guard rule.
- **Some constraint changes are judged conservatively.** Changing `le` to `lt` with the same number
  only tightens the model, but it shows as a removed `maximum`, so the lock reports a weakening.
  That costs one `--accept-weakening`, and nothing unsafe gets through.
