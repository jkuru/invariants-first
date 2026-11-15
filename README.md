# invariants-first

**A ratchet for your API rules. A change can tighten them freely, but loosening one, by a person or a
coding agent, fails CI until a reviewer says yes.**

Coding agents make tests pass by relaxing whatever is in the way: they raise a limit, widen a type,
or call the repository directly instead of going through the service. Each diff looks reasonable, so
it gets through review. invariants-first fails the build on exactly those changes.

## How the ratchet works

1. **Lock what you already have.** You don't write any rules to start. `inv init` reads your code:
   your pydantic `Field(le=10_000)`s and `Literal`s become value laws, and your service layer becomes
   path laws ("only `ExpenseService.refund` calls `RefundGateway`"). It writes `invariants.yaml` and
   `invariants.lock`, and freezes today's back doors so the check passes on day one.
2. **Tightening goes through.** Add a law or narrow a limit, re-pin with `inv lock`, commit.
3. **Loosening needs a person.** `inv check --base main` compares every law with the base branch. A
   raised limit, a widened schema or a new call that skips a service fails, even if the agent re-pinned
   the lock to hide it. A reviewer accepts it on purpose (for example with a pull-request label);
   the author or agent never can.

An agent raises a limit to make its test pass, then re-pins the lock itself. CI still catches it:

```text
$ inv check --base main
  FAIL  since main
        1 change(s) since main weaken a rule:
          schema quota.models.UsageEvent: pydantic.properties.units.maximum: 10000 -> 100000  WEAKENED (maximum raised: larger values pass)
        a person must accept these: re-run with --accept-weakening or INV_ALLOW_WEAKENING=1, set by a reviewer, never by the change's author or agent
```

A new script that calls the payment gateway directly fails the same check:

```text
  FAIL  scan
        bypass     REFUND-GATEWAY-DOOR scripts/refund_tool.py:5:5  scripts.refund_tool.refund_customer -> quota.repo.RefundGateway.refund reaches the sink without passing quota.service.ExpenseService.refund
```

## Two kinds of law, on API boundaries

Every layer of a system is an API: the HTTP route, the service method, the repository method, the
model call, the payment client. The ratchet holds two kinds of law on those boundaries:

- **Value laws:** inputs or outputs an API never allows ("a refund never exceeds the charge").
- **Path laws:** which API may call which ("only `BillingService.refund` calls `RefundGateway.refund`"),
  whatever sits behind the repository (SQLAlchemy, Django, MongoDB, DynamoDB, Stripe or raw SQL).

People own the laws at the boundaries; agents are free inside the implementations. For what CI can't
see, decorators check model replies and tool calls at run time, `pytest --inv-doors` checks at run time
that every sink is reached through its door (catching `getattr`, containers and callbacks the static
scan can't follow), and a pytest plugin proves each check does real work by switching it off and
watching a test fail.

The path-law scan is quiet on day one: a call it can't follow warns, and only one a change adds since
the base branch fails.

## Try it

```bash
cd invariants-py && python -m venv .venv && . .venv/bin/activate
pip install -e ../packages/invariants-cel -e ../packages/invariants-scan -e ../packages/invariants-schema-diff -e .
cd /path/to/your/app && inv init     # commit invariants.yaml and invariants.lock
inv check --base main                # in CI and pre-commit
```

The [10-minute guide](invariants-py/docs/start.md) adds the pre-commit hook, the GitHub Action and
Claude Code hooks that make the check hard to skip.

## What's in this repository

| Path | What it is |
|---|---|
| [`invariants-py/`](invariants-py) | The Python library and `inv` CLI (package `invariants-first`, import `invariants`). Start with its [README](invariants-py/README.md). |
| [`packages/invariants-cel`](packages/invariants-cel) | The CEL engines and the type check (import `invariants_cel`), usable on their own. |
| [`packages/invariants-scan`](packages/invariants-scan) | The path-law scan and `inv-scan` (import `invariants_scan`), usable on its own. |
| [`packages/invariants-schema-diff`](packages/invariants-schema-diff) | Is a JSON Schema or CEL edit a weakening? (import `invariants_schema_diff`; depends on `invariants-cel`). |
| [`apps/pool-service`](apps/pool-service) | Example: a FastAPI and SQLite lease service with an LLM triage step. |
| [`apps/fact-pipeline`](apps/fact-pipeline) | Example: a batch pipeline that extracts cited facts with a model (a deterministic fake by default). |
| [`apps/support-agent`](apps/support-agent) | Example: a tool-calling support agent with 12 seeded agent defects, each caught. |
| [`Invariant_first.md`](Invariant_first.md) | The design, for engineers: the two kinds of law, how each is checked, the architecture and the limits. |
| [`Invariants_what_how_when.md`](Invariants_what_how_when.md), [`makeInvariantSimple.md`](makeInvariantSimple.md) | The original concept notes. |
| `action.yml`, `.pre-commit-hooks.yaml` | The GitHub Action and the pre-commit hook, usable from other repositories as `jkuru/invariants-first`. |

The four packages are released together at one version ([`RELEASING.md`](invariants-py/RELEASING.md)).

## Status

Alpha (0.1.0), not yet on PyPI. Each app's `ci.sh` runs every check against the library in this
checkout:

```bash
cd invariants-py && python -m venv .venv && . .venv/bin/activate
pip install -e ../packages/invariants-cel -e ../packages/invariants-scan -e ../packages/invariants-schema-diff -e '.[dev,cel]'
python -m pytest tests -q
../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh && ../apps/support-agent/ci.sh
```

Built by Claude, Gemini and Jerry Kuru.

Licensed under Apache-2.0; see [`LICENSE`](LICENSE) (each package carries the same file) and [`NOTICE`](NOTICE).
