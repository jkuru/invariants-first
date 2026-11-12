# Invariant-first

Agents write more code than people can review. Invariant-first moves review to a small, stable
surface: you write down what an API must never do, and tools prove it holds on every change. This
library (`invariants`, CLI `inv`) does that for Python. It needs no decorators to start, and nothing
runs in production until you choose a runtime guard.

There are exactly two kinds of rule, and both sit on an API boundary:

| Kind | Says | Example | Checked by |
|---|---|---|---|
| **Value law** | what an API never accepts or returns | a refund is never above what is left of the order | the pinned schema, and a guard at run time |
| **Path law** | which API a lower one may be called through | `StripeGateway.refund` is reached only through `BillingService.refund` | `inv scan`, statically, on every change |

Nothing below the lowest API is a rule: SQL, triggers, transactions, isolation levels and private
fields are the implementation's business. Agents are free inside an implementation; people own the
boundaries.

## 1. Quick start

```bash
pip install 'invariants-first[schema]'      # Python 3.11+; the core depends only on PyYAML
cd your-project
inv init                                     # drafts invariants.yaml and invariants.lock
inv check                                    # validate, lock, verify, scan: one exit code
```

`inv init` reads your source (it never imports it) and writes two kinds of law it already finds:

- every pydantic model with constraints (`Field(gt=0, le=50_000)`, `Literal`, `constr`, ...) becomes
  a pinned contract;
- every repository it finds becomes a door rule: its write methods are the sinks, the service methods
  that call them are the doors, and today's back doors are frozen in `allow`.

Commit both files. From then on `inv check` fails when a change lets a model accept more than it does
today, or when new code reaches a repository write without passing its door. Add it to CI and to your
agent's hooks (section 6) and you are done with step one.

Step two is optional and per rule: put `@inv.guard` on a function and write its law in CEL, so it is
also checked on every call at run time (section 3).

## 2. Path laws

A door rule names the function every write must go through and the function that writes:

```yaml
rules:
  - id: REFUND-GATEWAY-DOOR
    door: "py:support_agent.service.BillingService.refund"
    sink: "py:support_agent.repo.StripeGateway.refund"
    allow:
      - "py:support_agent.admin.goodwill_refund"     # a known back door, frozen
      - "py:support_agent.admin.admin_goodwill"      # and its caller
```

That is the whole rule. It is `enforce` by default, needs no test and no decorator, and runs only in
`inv check`. `door`, `sink` and `allow` each take one `py:` path or a list.

### What the scan checks

`inv scan` parses the scanned roots with Python's `ast` and builds a call graph. For each door rule it
looks for any call path from an entrypoint to a sink that does not pass a door. It reports four kinds
of finding:

| Kind | Means | Fails `inv check` |
|---|---|---|
| `bypass` | a path of extracted calls reaches the sink around every door | yes |
| `forbidden` | a bypass idiom: `getattr` on the sink's name, `__wrapped__`, assigning over the sink or door | yes |
| `unresolved` | something the scan cannot follow: a call on a receiver of unknown type, the sink used as a value, `eval`, a computed attribute | yes (never counted as a pass) |
| `missing` | a sink, door or `allow` entry that does not exist, or an `allow` entry that no longer reaches the sink | yes |

A method call is a proven edge only when the scan is sure of the receiver's class:

| Receiver | Typed as |
|---|---|
| `Repo(session).add(x)` | exactly `Repo` |
| `r = Repo(s)` then `r.add(x)`, every assignment the same | exactly `Repo` |
| `def f(r: Repo)`, also `Optional[Repo]`, `Repo \| None`, `"Repo"` | `Repo` or a subclass |
| `r = get_repo()` where `def get_repo() -> Repo` (a factory, a FastAPI dependency) | `Repo` or a subclass |
| `self.repo`, when every assignment in the class's methods is one of the above | `Repo` or a subclass |
| `ctx.billing.refund()`, where `ctx: AgentContext` and `AgentContext` sets `self.billing = BillingService(...)` or declares `billing: BillingService` | `BillingService` or a subclass |

A subclass may arrive through an annotation, so the call also reaches every scanned override. An
attribute that any code sets from outside its object (`obj.repo = other` in a function that is not
one of the object's methods, or `setattr(obj, "repo", ...)`) is never typed. Anything else is a
name-matched edge, reported as `unresolved`: the scan never guesses that a path is safe.

The scan skips tests (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`) and migrations
(`migrations/`, `alembic/`) at any depth, unless `[tool.invariants.scan] exclude` in `pyproject.toml`
says otherwise. Output is text, JSON or SARIF 2.1.0.

### `allow`: freezing today's back doors

`allow` is how an existing codebase adopts a door rule without fixing everything first. It works like
an architecture test's frozen violations:

- A path to the sink passes only when **every** function on it is in `allow`: the call chain that
  existed when `allow` was written. A new caller of an allowed function, or new code an allowed
  function calls on its way to the sink, is a `bypass`.
- Inside an allowed function, the ways it already reaches the sink are excused. Unwrapping a guard,
  monkeypatching the door and reflection are still reported.
- Adding an entry is a weakening the lock refuses (section 4). Removing one is a tightening.
- An entry that no longer reaches the sink fails as `missing/allow-unused` until you delete it, so
  the list only shrinks.

### How `inv init` finds doors

| Step | Rule |
|---|---|
| Sinks | A function in a class or module named like a repository (`Repo`, `Store`, `Gateway`, `DAO`, `repo`, `store`, `crud`, `db` ...) whose name is a write verb, or any function whose body writes through a store client: an ORM session, a Django manager, a Mongo collection, a DynamoDB table, a client's `create` call (`stripe.Refund.create`), or raw SQL |
| Store clients | By name (`session`, `db`, `conn`, `collection` ...) or by annotation, whatever the name: `s: Session`, `db: AsyncSession`, or an alias such as `SessionDep = Annotated[Session, Depends(get_db)]`, also when imported |
| Doors | The callers in a service layer (a module or class named like one: `service`, `OrderService`, `UseCase` ...); else a function called only by HTTP route handlers (`@router.post`, `@app.get` ...) that sits between them and the write, even one-to-one; else the caller that most other functions go through (at least two) |
| Back doors | Every other path the real scan finds, frozen in `allow` with its call chain; a rule that still fails after freezing is written commented out, with the reason |
| Ranking | At most five rules (`--max-doors N`), money, quota and approval names first; the rest are listed in the file |

Every proposed rule is run through the real scan before it is written, so the file `inv init` writes
passes `inv check` as written.

**Graphify (optional).** With a [Graphify](https://github.com/Graphify-Labs/graphify) graph at
`graphify-out/graph.json` (or `--graph PATH`; `--no-graphify` turns it off), `inv init` reads it as
plain JSON. Nodes are matched to functions by file and line. A caller of a write becomes a door
candidate, even when nothing is named Service, when it sits in its own Graphify community, every
caller of it sits in a third one (a layer between two others), and it is not an entry point (`main`,
a route handler). Call links count as callers only when Graphify tagged them `EXTRACTED`; in
practice those are plain function calls the scan already sees, so the communities are what the graph
adds. Every candidate still goes through the scan, `inv check` never reads the graph, and Graphify
is never imported or run. Graphify's MCP server (`graphify-mcp`) answers queries over the same graph
for a coding agent; `inv init` reads the file, which gives the same data with no server running.

## 3. Value laws

### Pinned schemas

A pydantic model in `contracts:` is pinned by its resolved JSON Schema. Raising a maximum, dropping a
constraint, adding an enum value or making a field optional is a weakening (section 4). This needs no
runtime code: the model already validates its input.

```yaml
contracts:
  refund_tool_input: {type: pydantic, ref: "support_agent.models:RefundToolInput"}
```

Other contract types are `python` (a module), `dataclass`, `schema` (a TypedDict or `{field: type}`)
and `openapi` (a file, pinned per operation a rule names).

### Guard rules

A guard rule checks a function's arguments (and, for a pure function, its result) on every call:

```yaml
  - id: BILL-1
    guard: "py:support_agent.service.BillingService.refund"
    statement: A refund is never for an unknown order or above what is left of it.
    bad:
      - {when: 'order == null', message: no such order}
      - {when: 'order != null && args.amount_cents > order.remaining_cents', message: refund above what is left}
    tests: ["tests/test_runtime_defects.py::test_d6_the_door_refuses_an_over_refund_whoever_calls"]
```

```python
import invariants as inv

class BillingService:
    @inv.guard("BILL-1")
    def refund(self, order_id: str, amount_cents: int, reason: str) -> str: ...
```

- **`bad` is the prohibited state**, written in [CEL](https://cel.dev). It is true when the rule is
  broken. A list of `{when, message}` clauses is easier to review than one long `||`; every clause is
  evaluated, and the report names each one that fired.
- **Names in `bad`.** `args` (the bound arguments), `result` (after the call), `self`, `old_self`
  (below), and providers by name.
- **`tests`** are the falsifiers: tests that must fail when the guard is switched off. With tests the
  rule defaults to `enforce`; without them, to `detect` (log, never block).
- **`inv init`** suggests guard rules from the preconditions at the top of a function (`if amount <= 0:
  raise ...`, `assert credit <= limit`), translated to CEL and left commented out. A module constant
  is inlined: `MAX_REFUND = 10_000` makes `if amount > MAX_REFUND` into `args.amount > 10000`, and a
  tuple constant works with `in`.

### Providers: held data

A rule often needs state the call does not carry: the order being refunded, the customer's tier. A
provider is a typed, read-only function the rule names:

```yaml
providers:
  order: {type: "py:support_agent.models.Order | null", from: "py:support_agent.service.order",
          args: {ledger: self.ledger, order_id: args.order_id}}
```

```python
@inv.provider("order")
def order(ledger: LedgerRepo, order_id: str) -> Order | None: ...
```

The loader checks the name, the parameters (exactly the keys of `args`) and the return type. A
`pre_effect` check reads its providers before the call; a `post_effect` one, after. Values are cached
per provider and arguments for one call. A provider that raises or returns the wrong type is a
`rule_error`, never a violation. Every provider a check names is called on every call.

A check is not a lock. Two concurrent callers can both pass a check against the same state; making
check and write atomic is the store's job, below the rule. A rule that knows of such a race says so in
`mediation.residual_paths`.

### Transition rules: `old_self`

A rule on a method can compare the object before and after the call: `bad: 'self.balance <
old_self.balance - args.amount'`. Only the paths `bad` reads are copied, and only plain data;
`inv.check_snapshot(target, instance)` checks that in a test. A post-effect check can enforce only on
a target declared `effects: none` (a pure function). On anything with an external effect it detects.

### Model calls and edges

A model call is an API whose output nobody wrote. `@inv.model_call` makes its reply a checked value:

```python
class CheckedPlan(inv.Checked): ...

@inv.model_call("PLAN-1", schema=Plan)                # Plan: a pydantic model with extra="forbid"
def plan_action(ctx, ticket: str, customer_id: str, inv_feedback: str | None = None) -> CheckedPlan:
    return model.complete(ticket, customer_id, feedback=inv_feedback)   # the raw reply

@inv.edge("PLAN-1")                                   # the only consumer of a CheckedPlan
def execute_plan(ctx, plan: CheckedPlan, customer_id: str) -> str: ...
```

```yaml
    model_policy:
      schema: Plan
      format_retries: 2                 # bad JSON or a schema miss: retried with the parser error fed back
      semantic_resamples: 1             # a violation: one fresh sample, never told the rule
      feedback: {format: parser_error, semantic: none}
      abstain: {field: action, value: ESCALATE_TO_HUMAN}   # the model may decline: a person decides
    enforcers:
      - {point: model_call, timing: post_effect, mode: enforce, on_violation: escalate}
      - {point: edge, target: "py:support_agent.graph.execute_plan", timing: pre_effect, mode: enforce}
```

Each reply goes through parse (closed schema, no extra fields, NaN refused), abstain, then `bad`
against held data. Retries are bounded and recorded in `attempts`. When they are spent, the
`model_call` enforcer's `on_violation` decides. The edge re-validates the value it receives against
the closed schema and re-checks `bad` on it, so a forged `CheckedPlan`, one checked for another
customer, or one changed after the planner checked it never reaches a tool.

### Outcomes and actions

Every evaluation is one of four outcomes:

| Outcome | Means |
|---|---|
| `ok` | no clause fired |
| `violation` | a clause fired |
| `rule_error` | the rule could not be evaluated (a provider failed, a field is missing); never a violation |
| `parse_error` | a model reply did not fit the schema |

An enforcer in `enforce` mode applies `on_violation` to a violation and `on_error` to a rule error:

| `on_violation` | Raises | HTTP (FastAPI/ASGI) |
|---|---|---|
| `reject` | `inv.Rejected` | 422 (configurable, often 409) |
| `halt` | `inv.Halted` (an internal invariant broke: a bug) | 500 |
| `escalate` | `inv.Escalation`, with a review opened | 202 |
| `route` | `inv.Routed`, with a review opened | 202 |

`on_error: fail_closed` (the default) acts as if violated; `fail_open_and_page` lets the call through
and pages. When no review can be opened, the call raises `inv.ReviewNotOpened` and carries the work.
Detect mode never blocks; it records. Exception messages and events never carry request or reply
values unless debug is on.

## 4. The lock

`invariants.lock` pins what each rule's meaning depends on: the rule's semantic fields, each target's
signature and resolved types, each provider's declaration, and each contract's resolved schema.
`inv lock --check` (inside `inv check`) compares the code with the lock and prints a semantic diff:

```text
invariants.lock: 1 change(s) against the stored lock
  contract refund_tool_input: properties.amount_cents.maximum: 50000 -> 500000  WEAKENED (maximum raised)
```

| Change | Judged |
|---|---|
| a lower status or mode, a removed rule, enforcer or clause, `fail_open_and_page`, more retries or resamples, a `bad` that matches fewer cases, a new door or `allow` entry, a removed sink, a looser schema | `WEAKENED`: refused |
| a tighter schema, a removed `allow` entry, a stricter `bad` | a tightening: `inv check --relock` re-pins it with no person needed |
| a body-only refactor, a docstring, prose | no change |
| anything it cannot decide | a possible weakening |

Only a person accepts a weakening, with `inv lock --accept-weakening`. Because an agent can run that
too, CI runs `inv check --base origin/main`: it compares with the lock committed on the base branch,
so a weakening the branch re-pinned itself still fails. A reviewer allows it with
`--allow-weakening` (or `INV_ALLOW_WEAKENING=1`, which the GitHub Action sets from a PR label).

## 5. How a rule earns trust

| Field | Set by | Values |
|---|---|---|
| `status` | people, in review | `candidate`, `contested`, `detect`, `enforce`, `retired` |
| effective mode | computed | `min(enforcer mode, cap(status))`: only `enforce` status can block |
| standing | computed by `inv status` | `held`, `enforce(known-callers)`, `monitored`, `candidate`, `retired` |

The loader refuses a status the evidence does not support. `enforce` needs an enforcer in mode
`enforce`, falsifiers (`tests`, except for a door rule, whose evidence is the scan), and a timing that
can actually prevent (a post-effect check enforces only on a pure function).

A rule is `held` only when it is enforced, its mediation names the chokepoint every path crosses,
`inv verify` (every target carries its guard, every guard names a rule) and `inv scan` both pass, and
it lists no residual paths. Otherwise it is `enforce(known-callers)`, with the reasons.

`pytest --inv-ablate` proves a guard is load-bearing: it switches each rule off and re-runs its
falsifiers, which must now fail because of it. A falsifier must also pass on a control run without
ablation, so a flaky test cannot make a rule look covered.

## 6. Agents and CI

| Where | Command | What it stops |
|---|---|---|
| Claude Code `PreToolUse` | `inv hook pretool` | an agent running `--accept-weakening` or `INV_ALLOW_WEAKENING`, and any direct edit of `invariants.lock` (an Edit/Write tool, or a shell redirect, `sed -i`, `cp`, `mv`, `rm` onto it) |
| Claude Code `Stop` | `inv hook stop` | finishing while `inv check` fails (it sends the agent back once, with the failure) |
| pre-commit | `inv check --relock` (hook id `inv-check`) | a commit that weakens a law; tightenings re-pin themselves |
| CI | the GitHub Action, or `inv check --base origin/main` | a pull request that weakens a law, even after re-pinning its own lock |

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                           "hooks": [{"type": "command", "command": "inv hook pretool"}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "inv hook stop"}]}]}}
```

Put `invariants.yaml`, `invariants.lock`, the falsifier tests, the hook configuration and the CI
workflow under human-only review (CODEOWNERS). The rule set is only as strong as whoever reads a
weakening diff.

## 7. Architecture

```text
invariants.yaml ──► loader ──► Registry ──► @inv.guard / @inv.model_call / @inv.edge ──► outcome ──► action
     │              (schema,     (rules,       (bind args, call providers,                 (ok, violation,  (proceed, reject,
     │               types,      targets,       evaluate CEL, record event)                 rule_error,      halt, escalate,
     │               CEL check)  providers)                                                 parse_error)     route)
     ├──► lockfile ──► inv lock / inv check (semantic diff, weakening gate, --base)
     └──► scan ──────► inv scan / inv check (ast call graph, door rules, allow)
source ──► bootstrap + doors (+ graphify) ──► inv init (drafts the rule file, verified by scan)
```

| Module | Does |
|---|---|
| `loader.py`, `typecheck.py`, `schema/` | reads the rule file, expands the short forms, refuses what cannot hold, type-checks every clause |
| `registry.py` | loaded rules, modes, ablation, reviews, standing, `verify` |
| `guard.py`, `providers.py`, `evaluate.py` | the decorators, provider binding, the four-outcome evaluator and actions |
| `cel/` | three CEL backends behind one front end: `subset` (default, standard library), `cel-expr` (Google's C++ runtime), `celpy` (pure Python); `INV_CEL_BACKEND` picks one |
| `scan.py` | the static call graph and the path-law check |
| `lockfile.py` | pins, the semantic diff and the weakening gate |
| `bootstrap.py`, `doors.py`, `graphify.py` | `inv init`: contracts, door rules, suggested guard rules |
| `cli.py` | `inv` |
| `pytest_plugin.py` | `--inv-rules`, `--inv-verify`, `--inv-ablate`, `--inv-report` |
| `integrations/` | FastAPI and ASGI (`application/problem+json`), pydantic (closed schemas, contracts), OpenTelemetry (one span per evaluation) |

| Command | Does |
|---|---|
| `inv init` | drafts `invariants.yaml` and runs `inv lock` |
| `inv check [--relock] [--base REF]` | `validate`, `lock --check`, `verify`, `scan`, one exit code |
| `inv validate` | the JSON Schema and every load-time check |
| `inv lock [--check] [--accept-weakening]` | writes or checks the lock |
| `inv verify --import MOD` | every target guarded, every guard names a rule |
| `inv scan [--format text\|json\|sarif]` | the path-law check |
| `inv status --import MOD` | the standing of each rule, with reasons |
| `inv hook pretool\|stop` | the agent hooks |

Exit codes: 0 ok, 1 a check failed, 2 a usage error, 3 the scan has unresolved calls, 4 the scan
skipped an ablated `enforce` rule. Only 0 is a pass.

## 8. A worked example: a tool-calling agent

`apps/support-agent` is a support agent that issues refunds, credits and cancellations, with a fake
model so it needs no API key:

```text
ticket ─► triage ─► planner (@inv.model_call) ─► router ─► executor (@inv.edge) ─► tool ─► BillingService (@inv.guard) ─► StripeGateway / LedgerRepo
                         │ abstain or retries spent
                         └─► human handoff (a review is opened)
```

`inv init` on its code, before any rule was written by hand, found the 4 tool and plan schemas (23
constraints), the two repository door rules with the 3 tools as callers, and the one real back door,
a legacy admin refund and its caller, frozen in `allow`. Three runtime laws were then added: BILL-1
and BILL-2 on the billing doors, and PLAN-1 on the planner and its executor. Twelve seeded defects
are each caught, and safe changes (a new tool through the door, a tighter cap) pass:

| # | Defect | Caught by |
|---|---|---|
| 1 | a new tool calls `StripeGateway.refund` directly | `inv scan`: bypass |
| 2 | `StripeGateway.refund` put in the tool registry | `inv scan`: the sink used as a value |
| 3 | the refund cap raised from 50,000 to 500,000 | the lock (and `--base` when the branch re-pinned it) |
| 4 | `semantic_resamples` raised from 1 to 5 | the lock |
| 5 | a new tool routed through the frozen back door | `inv scan`: an allowed back door is not a door |
| 6 | the model over-refunds, uses another customer's order, or over-credits | PLAN-1 (one resample, then a person); BILL-1 and BILL-2 at the door for any caller |
| 7 | invalid JSON, an unknown tool name, an extra field | the closed schema: at most 3 attempts |
| 8 | the model answers `ESCALATE_TO_HUMAN` | abstain: a review is opened |
| 9 | a forged plan, or one checked for another customer | the executor edge |
| 10 | the plan changed after the check, or the ledger moved since | the executor edge re-checks what it receives |
| 11 | a coding agent deletes `@inv.guard("BILL-1")` | `inv verify` |
| 12 | a coding agent deletes rule BILL-2 | the lock |

Two more apps live in `apps/`: `pool-service` (FastAPI and SQLite: leases, approved resizes, a model
triage step) and `fact-pipeline` (a batch pipeline extracting cited facts with a model).

## 9. Limits

- **Coverage is only as good as the rules.** A defect no rule states is not caught. Count your effect
  sinks; each one without a rule is a gap or a decision to accept the risk.
- **The scan is approximate and conservative.** It does not follow dependency-injection containers,
  event buses or callbacks, or callers outside its roots. What it cannot resolve fails as
  `unresolved`; it never passes a path it is unsure of. Raw SQL from code that never calls the
  repository is invisible to it: database permissions stop that.
- **Door discovery is a heuristic.** It needs a layer to lock: a codebase whose routes write through
  the session directly (the full-stack FastAPI template, for one) gets no door rule. Review what
  `inv init` proposes.
- **A check is not a lock.** Concurrent callers can both pass; atomicity is the store's.
- **`inv.Checked` is forgeable** in Python, so every edge re-validates and re-checks what it receives.
- **CEL is a subset.** Rules stay within the grammar the three backends share. NaN and Infinity are
  `rule_error`s by default.
- **The lock's weakening analysis is syntactic.** What it cannot decide counts as a possible
  weakening. A behaviour change inside a function body does not change the lock.
- **Python only.** There is no SDK for other languages yet.

## 10. Performance

Guard overhead per call, one `pre_effect` decorator enforcer, no providers, Apple M4 Max, Python 3.14:

| Backend | 1 clause p50 | 1 clause p99 | 4 clauses p50 | 4 clauses p99 |
|---|---|---|---|---|
| `subset` (default) | 7.1 µs | 8.4 µs | 22.7 µs | 28.3 µs |
| `cel-expr` | 9.6 µs | 10.7 µs | 22.0 µs | 27.9 µs |
| `celpy` | 86 µs | 100 µs | 830 µs | 892 µs |

Providers add their own cost on every call. `inv scan` indexes about 1,850 functions in about a
second.

## Appendix: the repository

| Path | Holds |
|---|---|
| `invariants-py/` | the library, its tests (about 3,940, on three CEL backends) and docs |
| `invariants-py/docs/` | `start.md` (10-minute start), `rule-file.md` (every field), `concepts.md`, `scan.md`, `lockfile.md`, `api.md`, `cel.md`, `cookbook.md`, `evaluation.md` |
| `apps/support-agent/` | the tool-calling agent above, with its 12 seeded defects |
| `apps/pool-service/`, `apps/fact-pipeline/` | two more example apps |
| `action.yml`, `.pre-commit-hooks.yaml` | the GitHub Action and the pre-commit hook |

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e 'invariants-py[dev,cel]'
cd invariants-py && python -m pytest tests -q && python -m mypy
cd ../apps/support-agent && pip install -e '.[dev]' && ./ci.sh
```
