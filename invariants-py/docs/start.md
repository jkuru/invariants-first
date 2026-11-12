# Start in 10 minutes

The quickest win is to lock the rules your code **already has**, so no change, by a person or an
agent, can loosen them without someone saying yes. You write no rules for this, and nothing new
runs in production.

- **pydantic constraints:** `Field(gt=0, le=10_000)`, `max_length`, `Literal[...]`, required fields,
  including SQLModel models.
- **Database constraints:** `CHECK`, `NOT NULL`, `UNIQUE` and `PRIMARY KEY` in your `.sql` schema files.
- **Rules you add later:** short `guard` rules on the functions that move money or spend quota.

## 1. Draft and lock (2 minutes)

```bash
pip install invariants-first        # not on PyPI yet: pip install -e path/to/invariants-py
cd your-project
inv init
```

`inv init` reads your source code without importing it. It writes `invariants.yaml` and runs
`inv lock`. It never overwrites an existing rule file.

```text
wrote invariants.yaml
  read 3 source file(s)
  pinned 1 pydantic model(s) with 5 constraint(s)
  pinned 1 SQL file(s) with 5 constraint(s)
  suggested 2 rule(s) from function preconditions (commented out; see the file)
running `inv lock` to pin them ...
wrote invariants.lock: 0 rule(s), 0 target(s), 0 provider(s), 2 contract(s), 1 schema(s)
```

The rule file it writes looks like this:

```yaml
contracts:
  # charge_id: min_length=1, charge_id: max_length=64, amount: gt=0, amount: le=10000, +1 more
  refund_request: {type: pydantic, ref: "billing.models:RefundRequest"}
  # 5 constraint(s): refunds.amount: CHECK (amount > 0); refunds.amount: NOT NULL; ...
  schema: {type: sql, ref: "schema.sql"}

rules:
#  - id: REFUND
#    guard: "py:billing.payments.refund"
#    bad:
#      - {when: "args.amount <= 0", message: "refund must be positive"}
#      - {when: "args.refunded + args.amount > args.charged", message: "refund exceeds what is left of the charge"}
#    tests: []
```

Review the file. Delete any contract you don't care about, then commit both `invariants.yaml` and
`invariants.lock`. If `inv lock` fails, it is usually a model `ref` that doesn't import from the
project root. Fix the ref, or pass `--path DIR`, then run `inv lock` again.

## 2. What it catches

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

These changes count as a weakening, because the model or table now accepts something it refused:

- a raised maximum or a lowered minimum, or either one removed;
- a new enum value;
- a field that is no longer required, or a field removed;
- a changed `pattern`, `format` or type;
- a dropped or edited SQL constraint.

A change that only tightens the rules passes, and so does every other edit to the code.

Running `inv check --relock` re-pins the lock by itself when no change weakens anything, so the
lock never needs hand maintenance.

## 3. Make the check unskippable (5 minutes)

The lockfile alone isn't enough, because an agent can run `inv lock --accept-weakening` itself.
Three layers close that gap. Each one is a single copy and paste.

**pre-commit.** Add this to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: inv-check
        name: invariants check
        entry: inv check --relock
        language: system
        pass_filenames: false
        always_run: true
```

It uses `language: system` because `inv check` imports your app to pin its models, so it must run
in your project's environment. You can also point `repo:` at this repository and use `id: inv-check`;
the hook is defined in `.pre-commit-hooks.yaml` at the repository root.

**CI: compare against the base branch.** `inv check --base origin/main` compares the rules with
the lock committed on `main`, not with the lock in the pull request. A weakening that the pull
request re-pinned itself still fails:

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
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "inv hook pretool"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "inv hook stop"}]}
    ]
  }
}
```

- **`inv hook pretool`** blocks any shell command containing `--accept-weakening`,
  `--allow-weakening` or `INV_ALLOW_WEAKENING`. The agent is told to report the weakening to
  you instead.
- **`inv hook stop`** runs `inv check --relock` when the agent tries to finish. If the check fails,
  it sends the agent back with the failure, once per stop, so it can't loop.

Both commands read Claude Code's hook JSON on stdin, and both block by exiting with code 2.

For other agents, put this in `AGENTS.md`, and rely on pre-commit and CI to enforce it:

```markdown
Run `inv check` before you finish. If it reports a WEAKENED change, fix the code so it keeps the
rule. Never run `inv lock --accept-weakening` or set INV_ALLOW_WEAKENING; tell the user which rule
your change weakens and why.
```

## 4. Your first guarded rule (when you want one)

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
arithmetic, `len`, `is None`, and `in` over a literal list. Anything else, such as division (Python
and CEL divide differently), a global constant or a helper call, is listed as not translated rather
than guessed.

## 5. When to go further

Everything else is optional, and you can add it rule by rule:

- providers that read the database;
- doors and the mediation scan, which proves no code path goes around the guard;
- transactional effects;
- model-call parsing with retries;
- `old_self` transition rules.

Start with the [README quickstart](../README.md#quickstart) and [concepts](concepts.md). The short
form and the long form pin to the same lock, so switching a rule from one to the other isn't a
change.

## Limits

- **SQL is read, not parsed.** The reader understands common `CREATE TABLE` and
  `ALTER TABLE ... ADD CONSTRAINT` shapes. DDL kept in Python strings or produced by a migration tool
  isn't pinned (`inv init` lists the Python files that contain `CREATE TABLE`). Pin a schema dump
  instead.
- **Validators are code, so they aren't pinned.** A pydantic `@field_validator` or `@model_validator`
  isn't part of the JSON Schema. `inv init` notes each model that has validators. To protect one,
  write it as a guard rule.
- **Some constraint changes are judged conservatively.** Changing `le` to `lt` with the same number
  only tightens the model, but it shows as a removed `maximum`, so the lock reports a weakening.
  That costs one `--accept-weakening`, and nothing unsafe gets through.
