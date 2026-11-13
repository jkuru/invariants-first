# Invariant-first

*A handbook for software engineers: how to keep coding agents and AI features inside the laws of
your system, with a Python library called `invariants` (package `invariants-first`, command `inv`).*

**How to read this.** Chapters 1 and 2 are the short version: the idea, and what you do in your
first ten minutes. Chapters 3 to 9 explain one part each, in the order you will meet them. Chapter
10 tours the three example apps, chapter 11 is a reference to come back to, and chapter 12 says
plainly what the tool does not do. Every example is real code from the `apps/` folder of this
repository, and every output shown was produced by the library.

---

## Chapter 1. Why review stopped scaling

### 1.1 Two ways AI breaks a system

Teams now use AI in two places. Coding agents write the code, and AI features run inside the
product: a model that plans a refund, triages a failed job, or extracts facts from a document. Both
have the same problem. They produce more output than a person can check carefully.

Look at how things actually go wrong, and two patterns come up again and again.

**A coding agent takes the shortcut.** You ask for a feature, and the tests fail. The quickest way
to make them pass is often to loosen something:

- raise a validation cap from `Field(le=50_000)` to `Field(le=500_000)`;
- write a new tool that calls `StripeGateway.refund()` directly, skipping `BillingService.refund()`,
  which is where the balance check lives;
- raise a model's retry budget from 1 to 5 so a flaky prompt finally passes.

Every unit test passes. The type checker and the linter are happy. A reviewer skims a twenty-file
diff and approves it. Nothing in that diff looks wrong unless you already know which line matters.

**A model produces something plausible and wrong.** Inside the product, a planner model returns
perfectly formatted JSON:

```json
{"action": "REFUND", "customer_id": "C-1", "order_id": "O-3", "amount_cents": 45000, "reason": "damaged"}
```

It fits your schema. Every field has the right type. But `O-3` belongs to another customer, and
there is only a little left on it.

The lesson from both: **valid shape is not valid behaviour.** A schema can say "this is an integer
between 1 and 50,000". It cannot say "no more than what is left on this order", and it cannot say
"only the billing service may call the payment gateway". Those are the rules that matter, and today
they live in people's heads and in scattered `if` statements that an agent can quietly delete.

### 1.2 Shape, laws and bodies

Think of a system as three layers of agreement:

| Layer | What it says | Example | Who owns it |
|---|---|---|---|
| Shape | what inputs and outputs look like | `refund(order_id: str, amount_cents: int) -> str` | people |
| Laws | what must never happen across those APIs | a refund never exceeds what is left of the order, and only `BillingService.refund` may call `StripeGateway.refund` | people |
| Bodies | the code that satisfies both | SQL, loops, prompts, helpers, caches | agents and people |

API-first development made the first layer machine-readable: OpenAPI, Protobuf, pydantic.
Invariant-first does the same for the second layer. You write the laws down in one small file,
`invariants.yaml`, and a tool checks them on every change and, where you choose, on every call.

That changes what a reviewer has to read. Without it, you read every line an agent wrote, looking
for the one that skipped a check. With it, you read a short rule file and its diff. Agents are free
to rewrite the bodies; the tool shows that no law was loosened and no boundary was bypassed.

### 1.3 Every layer is an API, and the laws live on the boundaries

A real system is not one HTTP API with a database behind it. Every layer is an API:

```text
POST /refunds, run_ticket()                          the entry API (HTTP routes, agent graphs)
    │
    ▼
BillingService.refund(), plan_action()               the service API (business rules, model calls)
    │
    ▼
LedgerRepo.record_refund(), StripeGateway.refund()   the repository and gateway API (writes)
    │
    ▼
SQLite, Postgres, Stripe's HTTP API                  below the API: the implementation's business
```

The rule of this whole design is simple: **laws sit on API boundaries, never below them.** How a
repository stores things (SQL, an ORM, a document store, a payment provider) is that repository's
own business. A law never mentions a table, a trigger, a transaction or a private field. That keeps
rules readable, keeps them the same whatever database you use, and keeps them stable when an agent
rewrites an implementation.

### 1.4 The only two kinds of law

Because every law sits on a boundary, there are only two kinds:

1. **A value law** says what an API never accepts or returns. "A refund is never above what is left
   of the order." It is checked by a pinned schema in CI, and by a guard on every call at run time.
2. **A path law** says which API a lower one may be called through. "`StripeGateway.refund` is
   reached only through `BillingService.refund`." It is checked by reading the code, on every
   change. It costs nothing at run time.

You need both. A value law on `BillingService.refund` is a lock on the door. It is worthless if an
agent writes a new tool that walks around the door and calls the gateway directly. The path law
shows there is no other way in.

### 1.5 The three apps in this handbook

| App | What it is | What could go wrong |
|---|---|---|
| `apps/support-agent` | a tool-calling support agent: triage, a planner model, an executor, billing tools, a payment gateway and a ledger | the model over-refunds or refunds another customer's order; a coding agent adds a tool that calls Stripe directly or raises the refund cap |
| `apps/pool-service` | a FastAPI service that leases slots in worker pools, resizes pools with a two-person approval, and triages failed jobs with a model | leases exceed a pool's size; a resize without approval or more than 4x; the model blames the wrong cause |
| `apps/fact-pipeline` | a batch pipeline that extracts cited facts from documents with a model and answers questions | an invented quote; a fact believed on one source; an answer citing a chunk it never read |

All three run with a deterministic fake model, so they need no API key.

---

## Chapter 2. Your first ten minutes

### 2.1 Start in CI, change no production code

A tool that asks you, on day one, to learn a new language, write two hundred lines of YAML,
decorate fifteen functions and ship a new runtime into production does not get adopted. So adoption
comes in two steps:

- **Step 1, CI only.** Install the library as a dev dependency, like a linter. Run `inv init`. It
  finds the laws your code already has and locks them. Your production code does not import the
  library at all.
- **Step 2, at run time, one function at a time.** On the few functions that move money or call a
  model, add a decorator so a law is also checked on every call.

Most of the value of step 1 comes for free, and you can stop there.

### 2.2 Install and run

```bash
pip install 'invariants-first[pydantic,schema]'    # Python 3.11 or later; the core needs only PyYAML
cd your-project
inv init
inv check
```

`inv init` reads your source code. It parses it; it never imports or runs it. Here is what it
printed on `apps/support-agent` with no rule file yet:

```text
wrote invariants.yaml
  read 11 source file(s)
  pinned 4 pydantic model(s) with 23 constraint(s)
  suggested 1 rule(s) from function preconditions (commented out; see the file)
  door LEDGER-REPO-DOOR: support_agent.repo.LedgerRepo through support_agent.service.BillingService.cancel
       or support_agent.service.BillingService.credit or support_agent.service.BillingService.refund (3 caller(s))
  door STRIPE-GATEWAY-DOOR: support_agent.repo.StripeGateway through support_agent.service.BillingService.refund (1 caller(s))
    found 1 back door(s) in support_agent/admin.py; frozen in allow, so check passes and a new one fails
running `inv lock` to pin them ...
wrote invariants.lock: 2 rule(s), 0 target(s), 0 provider(s), 4 contract(s), 4 schema(s)
```

It found three kinds of law.

**Value laws in your pydantic models.** Every model with constraints (`Field(gt=0, le=50_000)`,
`min_length`, `Literal[...]`) becomes a contract in the rule file, with a comment listing what it
pins:

```yaml
contracts:
  # order_id: min_length=1, order_id: max_length=40, amount_cents: gt=0, amount_cents: le=50000, +2 more
  refund_tool_input: {type: pydantic, ref: "support_agent.models:RefundToolInput"}
```

Then `inv init` runs `inv lock`, which records each model's full JSON Schema in `invariants.lock`.
From now on, if anyone raises `le=50_000` to `le=500_000`, `inv check` fails.

**Path laws between services and repositories.** It found the repository classes, their write
methods, and the service methods that call them, and wrote two door rules:

```yaml
  - id: STRIPE-GATEWAY-DOOR
    door: "py:support_agent.service.BillingService.refund"
    sink: "py:support_agent.repo.StripeGateway.refund"
    allow:
      - "py:support_agent.admin.admin_goodwill"
      - "py:support_agent.admin.goodwill_refund"
```

The `allow` list is the interesting part. Section 3.5 explains it.

**Suggested guard rules.** It read the checks at the top of functions (`if amount <= 0: raise
...`, `assert state.plan is not None`), translated them into rules, and left them commented out at
the bottom of the file. Turning one on is step 2 (chapter 4).

### 2.3 What `inv check` does

`inv check` runs four checks and gives one exit code, which is what CI and agent hooks need. On the
support agent, with its finished rule file:

```text
inv check: invariants.yaml
  ok    rules: 5 rule(s), 4 contract(s) load
  ok    lock: invariants.lock matches
  ok    guards: verify: pass (3 targeted rule(s) guarded, every guard names a rule)
  ok    scan: no path skips a door or guard
pass
```

1. **rules**: the rule file is valid and every rule can actually hold (chapter 8);
2. **lock**: nothing a rule depends on has been loosened since it was locked (chapter 7);
3. **guards**: every function a rule names carries its decorator, and every decorator names a rule
   (chapter 8). Before you add any guard, this line says `skip`;
4. **scan**: no code path reaches a protected write without going through its door (chapter 3).

Commit both files, add `inv check` to CI and to your agent's hooks (chapter 9), and step 1 is done.

---

## Chapter 3. Path laws: who may call what

### 3.1 Why tests never catch a back door

A back door is a new path to a protected write that skips the check. Say an agent adds this tool:

```python
from support_agent.context import AgentContext
from support_agent.models import RefundToolInput


def vip_refund_tool(ctx: AgentContext, inp: RefundToolInput) -> str:
    return ctx.billing.stripe.refund(inp.order_id, inp.amount_cents)    # skips BillingService.refund
```

Your tests for `BillingService.refund` still pass: that code did not change. A test for the new
tool, if anyone writes one, passes too: the refund goes through. The only way to catch it is to ask
a structural question: is there any way to reach `StripeGateway.refund` that does not go through
`BillingService.refund`? That question is about the shape of the call graph, and a path law
answers it.

### 3.2 A door rule

```yaml
rules:
  - id: STRIPE-GATEWAY-DOOR
    door: "py:support_agent.service.BillingService.refund"
    sink: "py:support_agent.repo.StripeGateway.refund"
```

- The **sink** is the protected write: the lowest API that does the thing you care about.
- The **door** is the API every call to the sink must go through, because that is where the checks
  are.
- `door` and `sink` each take one `py:module.function` path, or a list.

That is a complete rule. It needs no decorator in your code and no test. It is `enforce` from the
start, because its evidence is the scan itself. Nothing runs in production: `inv check` reads your
source.

For the tool above, `inv check` fails:

```text
  FAIL  scan
        bypass     STRIPE-GATEWAY-DOOR support_agent/vip.py:6:12  support_agent.vip.vip_refund_tool ->
                   support_agent.repo.StripeGateway.refund reaches the sink without passing
                   support_agent.service.BillingService.refund
        LEDGER-REPO-DOOR: pass (no findings)
        STRIPE-GATEWAY-DOOR: fail (1 bypass)
        scanned 12 file(s), 48 function(s)
fail
```

### 3.3 How the scan reads your code

`inv scan` parses every file under the scanned folders with Python's own `ast` module. It never
imports your code, so it is safe to run on anything and needs no database or environment.

From the parsed code it builds a **call graph**: for each function, which functions it calls. Then,
for each door rule, it looks for any chain of calls that reaches a sink without passing a door.

The hard part is method calls. In `ctx.billing.stripe.refund(...)`, which `refund` is that? There
are two methods with that name in the support agent: `BillingService.refund` and
`StripeGateway.refund`. Guessing wrong in one direction hides a back door; guessing wrong in the
other cries wolf. So the scan trusts a method call only when it can be sure of the object's class:

| How the object is made | What the scan knows |
|---|---|
| `Repo(session).add(x)` | exactly `Repo` |
| `r = Repo(s)` then `r.add(x)`, where `r` is never assigned anything else | exactly `Repo` |
| a parameter annotated `r: Repo` (also `Optional[Repo]`, `Repo \| None`, `"Repo"`) | `Repo` or a subclass |
| `r = get_repo()` where `def get_repo() -> Repo` (a factory, a FastAPI dependency) | `Repo` or a subclass |
| `self.repo`, when every assignment to it in the class's methods is one of the above | `Repo` or a subclass |
| a chain such as `ctx.billing.stripe`, where `ctx: AgentContext`, `AgentContext.__init__` sets `self.billing = BillingService(...)` and `BillingService.__init__` sets `self.stripe = stripe` from a parameter annotated `StripeGateway` | `StripeGateway` or a subclass |

Each class's attributes are worked out in the module that defines the class, so imports there are
what count. When a subclass might arrive, the call also counts as reaching every subclass that
overrides the method. And one safety rule: an attribute that some code sets from outside its object
(`ctx.billing = something_else` in a function that is not one of the class's own methods, or
`setattr(ctx, "billing", ...)`) is never trusted, because it could hold anything.

A call the scan cannot be sure of, on a name that could be a sink, is reported as `unresolved`,
which fails the check. The scan never guesses that a path is safe. If your code passes repositories
around without type hints, the fix is usually one annotation.

The scan skips test code (`tests/`, `test/`, `testing/`, `test_*.py`, `*_test.py`, `conftest.py`)
and migrations (`migrations/`, `alembic/`) at any depth: a unit test calling the repository directly
is not a back door. `[tool.invariants.scan] exclude` in `pyproject.toml` replaces that list.

### 3.4 The four kinds of finding

| Kind | What it means | Example |
|---|---|---|
| `bypass` | a chain of calls reaches the sink without passing a door | the `vip_refund_tool` above |
| `forbidden` | code uses a trick that hides a call from the scan or disarms a door | `getattr(gateway, "refund")(...)`; `door_function.__wrapped__(...)` (calls it without its guard); assigning over the sink or door |
| `unresolved` | something the scan cannot follow | a call on an untyped object; the sink passed around as a value (`TOOLS = {"vip": StripeGateway.refund}`); `eval` |
| `missing` | a name in the rule does not exist any more, or an `allow` entry is stale | a renamed door |

All four fail `inv check`. Output is plain text, JSON, or SARIF for GitHub code scanning.

### 3.5 `allow`: adopting a rule without fixing everything first

Real codebases already have back doors. In the support agent, a legacy admin page refunds through
the gateway directly:

```python
def goodwill_refund(gateway: StripeGateway, order_id: str, amount_cents: int) -> str:
    return gateway.refund(order_id, amount_cents)        # calls StripeGateway.refund directly

def admin_goodwill(order_id: str, amount_cents: int) -> str:
    return goodwill_refund(StripeGateway(), order_id, amount_cents)
```

If `inv init` wrote a strict rule, your first `inv check` would fail, and you would uninstall the
tool. Instead it runs the real scan on its proposed rule, finds this chain, and freezes it in
`allow`, with a comment saying where it is. That gives you a one-way ratchet:

1. **Today's code passes.** The frozen chain is let through.
2. **A new back door fails.** A path to the sink passes only when every function on it is in
   `allow`. So a new tool that calls `goodwill_refund` is a `bypass`, and so is new code that
   `goodwill_refund` starts calling on its way to the sink.
3. **The frozen function cannot become a hiding place.** Inside an allowed function, its plain calls
   to the sink are excused, but unwrapping a guard, monkeypatching the door and `eval` are still
   reported.
4. **Adding to `allow` needs a person.** The lock treats a new entry as a weakening (chapter 7).
5. **The list only shrinks.** When someone fixes `goodwill_refund` to go through the service, the
   check fails with `missing/allow-unused` until they delete the entry. Removing an entry is a
   tightening, so no approval is needed.

### 3.6 How `inv init` finds the doors

`inv init` proposes door rules by reading names and code. It works in four steps.

**1. Find the writes (sinks).** A function is a write when either:

- it is in a class or module named like a repository (classes ending in `Repo`, `Repository`,
  `Store`, `Storage`, `Dao`, `Gateway`, `Persistence`, `Table`, `Collection`; modules such as
  `repo`, `store`, `storage`, `dao`, `db`, `gateway`, `crud`) and its name starts with a write verb
  (`save`, `add`, `insert`, `update`, `delete`, `create`, `record`, `refund`, `charge`, `cancel`,
  `grant`, `reserve` and many more); or
- its body writes through a store client: an ORM session (`session.add`,
  `session.execute(update(...))`), a Django manager (`Order.objects.create`), a Mongo collection
  (`insert_one`), a DynamoDB table (`put_item`), a client's `create` call (`stripe.Refund.create`),
  or raw SQL (`INSERT`, `UPDATE`, `DELETE`).

It recognises a store client by its name (`session`, `db`, `conn`, `cursor`, `collection` ...) or by
its type annotation, whatever it is called: `s: Session`, `db: AsyncSession`, or an alias such as
`SessionDep = Annotated[Session, Depends(get_db)]`, even when the alias is imported from another
module. Reads (`select`, `get`) and `commit` do not count as writes.

**2. Find the doors.** For each write, the doors are, in order of preference:

- its callers in a service layer: a module named `service`, `services`, `managers`, `domain`,
  `logic`, `workflows`, `use_cases` ... (or `billing_service`), or a class named `...Service`,
  `...Manager`, `...UseCase`, `...Interactor`, `...Workflow`, `...Handler`;
- otherwise, a caller that is called only by HTTP route handlers (`@router.post`, `@app.get` ...)
  and sits between them and the write, even when only one route calls it;
- otherwise, the caller that the most other functions go through, if at least two do.

**3. Freeze today's back doors.** It runs the real scan on each proposed rule and freezes what it
finds in `allow`, with each back door's whole call chain. If the rule still fails (a write made from
module-level code, say), the rule is written commented out, with the reason.

**4. Rank and write.** It writes at most five rules (`--max-doors N` changes that, `--no-doors`
skips this step), putting first the ones whose names suggest money, quotas, approvals or inventory
(`refund`, `balance`, `ledger`, `quota`, `approval`, `order`, `stock` ...). The rest are listed in
the file as comments.

Because every proposed rule has already been through the real scan, the file `inv init` writes
passes `inv check` as written.

**When it finds nothing.** Door discovery needs a layer to lock. On the full-stack FastAPI
template, `inv init` writes no door rule, and that is correct: its routes call `crud` and write
through the session themselves, so there is no layer every write goes through yet. The file lists
the writes it found instead.

### 3.7 Graphify, measured

[Graphify](https://github.com/Graphify-Labs/graphify) builds a knowledge graph of a codebase with
tree-sitter and clusters it into communities. `inv init` can read its `graphify-out/graph.json`
(`--graph PATH` names another file; `--no-graphify` turns it off) for door hints:

- graph nodes are matched to functions by file and line, so Graphify's internal ids do not matter;
- a caller of a write becomes a door candidate when it sits in a different community from the write,
  every one of its callers sits in a third community, and it is not an entry point (`main`, a route
  handler);
- graph call links count only when Graphify marked them `EXTRACTED` (parsed from the source), never
  `INFERRED` or `AMBIGUOUS`.

Every candidate still goes through the real scan, `inv check` never reads the graph, and the
library never imports or runs Graphify.

We ran Graphify 0.9.107 for real on five codebases: the three apps, the full-stack FastAPI template,
and a small three-layer shop with unconventional names (`api.py → checkout.py → ledger.py`). On all
five, the graph added no door rule that the naming heuristics missed. The reason is instructive:
Graphify's communities follow call chains, roughly one per feature, not layers. In the shop,
`handle_buy → place_order → post_charge` is one community and the refund chain is another, so no
function sits between two communities. Its `EXTRACTED` call links are plain function calls the scan
already sees; method calls come out as `INFERRED`. An early version of the community rule even
promoted a CLI `main` that writes the store directly into a door, which is why entry points are now
excluded. The real graphs are pinned in the test suite, so these results are re-checked on every
run.

Graphify's MCP server is useful in a different way: it lets a coding agent ask questions about the
code while it works ("what is the path from this tool to the gateway?"). To use it,
`pip install 'graphifyy[mcp]'` and add this to the project's `.mcp.json`:

```json
{"mcpServers": {"graphify": {"command": "graphify-mcp", "args": ["graphify-out/graph.json"]}}}
```

---

## Chapter 4. Value laws: checking what goes in and comes out

### 4.1 From step 1 to step 2

Step 1 protects the shape of your APIs and the paths between them, without touching production.
Step 2 adds checks that need live data: "no more than what is left of *this* order" can only be
checked at the moment of the call. You add them one function at a time, starting with the few that
matter most.

### 4.2 Pinned schemas

Before any decorator, the cheapest value law is the one you already wrote: a pydantic model.

```python
class RefundToolInput(Closed):
    order_id: str = Field(min_length=1, max_length=40)
    amount_cents: int = Field(gt=0, le=50_000)
    reason: str = Field(min_length=1, max_length=200)
```

The model already validates input at run time. What it cannot do on its own is stay that strict.
The lock pins its resolved JSON Schema, so raising a maximum, dropping a constraint, adding an enum
value or making a field optional is reported as a weakening (chapter 7). Besides pydantic, a
contract can be a `dataclass`, a `schema` (a TypedDict or a `{field: type}` map), a `python`
module, or an `openapi` file pinned per operation.

### 4.3 A guard rule

A guard rule checks a function's arguments on every call:

```yaml
  - id: BILL-1
    guard: "py:support_agent.service.BillingService.refund"
    statement: A refund is never for an unknown order or above what is left of the order.
    bad:
      - {when: 'order == null', message: no such order}
      - {when: 'order != null && args.amount_cents > order.remaining_cents', message: refund above what is left of the order}
    tests: ["tests/test_runtime_defects.py::test_d6_the_door_refuses_an_over_refund_whoever_calls"]
```

```python
import invariants as inv

class BillingService:
    @inv.guard("BILL-1")
    def refund(self, order_id: str, amount_cents: int, reason: str) -> str:
        refund_id = self.stripe.refund(order_id, amount_cents)
        self.ledger.record_refund(refund_id, order_id, amount_cents)
        return refund_id
```

Read it like this:

- **`guard`** names the function. The decorator on it carries the rule id, so a reviewer can see
  in the code that something checks this call.
- **`bad` describes the forbidden state, not the allowed one.** It is true when the rule is broken.
  Writing what must never happen is easier to review than writing what is allowed, and it matches
  how people state rules ("a refund is never above the balance").
- **A list of clauses** reads better than one long `||`. Every clause is evaluated, and the error
  names each one that fired.
- **`tests`** names the tests that must fail if the guard is switched off. They are the evidence
  that the guard does something (chapter 8). With tests, the rule blocks (`enforce`); without them,
  it only logs (`detect`), so you can roll a new rule out safely.

`bad` is written in **CEL**, the Common Expression Language that Kubernetes and Google APIs use for
the same job. It reads like a condition in any C-style language: `args.amount_cents > 50000`,
`size(args.items) == 0`, `reply.action == "REFUND" && planned_order == null`. CEL cannot loop
forever or change anything, which is what you want in a rule that runs on every call. Inside `bad`,
`args` is the call's arguments, `result` the return value (in a check after the call), `reply` a
model's parsed reply, `self` the object, and providers (below) appear by name. The library adds
three helpers: `inv.sum`, `inv.zip` and `inv.unique_by`.

### 4.4 The NaN trap

One case deserves its own section, because it is the kind of bug that passes review. Every
comparison with NaN is false. So this rule, which looks airtight, lets a NaN through:

```text
bad: 'reply.confidence < 0.0 || reply.confidence > 1.0'
```

NaN gets in more ways than you expect: the `NaN` token Python's `json` module accepts, the string
`"NaN"` that lax pydantic parsing converts, a clause's own `0.0 / 0.0`, a NaN inside a list. So the
library refuses non-finite numbers everywhere: in a model reply it is a parse error, and in a value a
clause reads it is a rule error. `inv.configure(allow_non_finite=True)` turns that off; you will
rarely want to.

### 4.5 Held data: providers

The order's remaining balance is not in the call's arguments. If you let the caller pass it in, the
caller can lie, or pass a stale value. So the rule reads it itself, through a **provider**: a named,
typed, read-only function.

```yaml
providers:
  order: {type: "py:support_agent.models.Order | null", from: "py:support_agent.service.order",
          args: {ledger: self.ledger, order_id: args.order_id}}
```

```python
@inv.provider("order")
def order(ledger: LedgerRepo, order_id: str) -> Order | None:
    return load_order(ledger, order_id)
```

When the rule loads, the library checks the provider's name, that its parameters are exactly the
keys of `args`, and that its return annotation matches the declared type. A check that runs before
the call reads its providers before the call; a check that runs after, after it. Values are cached
for the call. If a provider raises or returns the wrong type, the outcome is a rule error, never a
silent pass (chapter 6).

**A check is not a lock.** Two requests can both pass BILL-1 at the same moment, against the same
balance, before either writes. Making the check and the write atomic is the store's job, with its
own constraint or transaction, below the API. A rule that knows of such a race says so in
`mediation.residual_paths`, and its standing reports it honestly (chapter 8).

### 4.6 Before and after the call

A check can run before the call (`pre_effect`) or after it (`post_effect`). Before is the one that
can stop something. After can stop only a pure function, because the effect has not escaped: the
library refuses `enforce` on an after-check unless the target is declared `effects: none`. On
anything with a real effect, an after-check can only report (`detect`).

An after-check can compare the object before and after the call with `old_self`:

```text
bad: 'self.balance < old_self.balance - args.amount'
```

The library copies only the paths `bad` reads, and only plain data (numbers, strings, lists, maps,
dataclasses), never the whole object. `inv.check_snapshot(target, instance)` checks in a test that
those paths really are plain data.

### 4.7 Suggestions from your existing checks

`inv init` already read the checks at the top of your functions:

```python
MAX_REFUND = 10_000

def refund(charge_id: str, charged: int, refunded: int, amount: int) -> None:
    if amount <= 0:
        raise ValueError("refund must be positive")
    if amount > MAX_REFUND:
        raise ValueError("refund above the cap")
```

becomes a commented-out rule with `bad: 'args.amount <= 0'` and `bad: 'args.amount > 10000'`.
Parameters become `args.<name>`, an `assert` is negated, and a module constant bound once (an int,
float, string, bool or None, or a tuple, list or set of them) is replaced by its value, so raising
the constant later is a change to the rule. Anything it cannot translate faithfully (division, which
Python and CEL do differently, or a helper call) is listed as "not translated" rather than guessed.
To turn a suggestion on: uncomment it, add `@inv.guard("<id>")` to the function, write a test that
expects `inv.Rejected`, list it in `tests`, and run `inv lock`.

---

## Chapter 5. Models, agents and the edges between them

### 5.1 A model call is an API nobody wrote

A normal function's output was written by a person and reviewed. A model's output was written by
nobody. It can be malformed, it can contain fields you never asked for, it can be well-formed and
wrong, and it can decide it does not know. So a model call needs more than a guard.

### 5.2 The loop: parse, abstain, check

```python
class CheckedPlan(inv.Checked): ...

@inv.model_call("PLAN-1", schema=Plan)
def plan_action(ctx: AgentContext, ticket: str, customer_id: str, inv_feedback: str | None = None) -> CheckedPlan:
    return _model.complete(ticket, customer_id, feedback=inv_feedback)      # the raw reply text
```

```yaml
    model_policy:
      schema: Plan
      format_retries: 2
      semantic_resamples: 1
      feedback: {format: parser_error, semantic: none}
      abstain: {field: action, value: ESCALATE_TO_HUMAN}
```

The function returns the model's raw reply. The decorator does the rest, for every reply:

1. **Parse** it into the closed schema `Plan` (a pydantic model with `extra="forbid"`). Extra or
   missing fields, a value outside an enum, a wrong type or a NaN is a parse error. On a parse
   error, the model is asked again, up to `format_retries` times, and told what was wrong: the
   parser's message arrives in its `inv_feedback` parameter.
2. **Abstain.** If the reply says `action: ESCALATE_TO_HUMAN`, the model has declined. That is a
   good answer, not a failure: a review is opened for a person and nothing runs.
3. **Check `bad`** against held data. PLAN-1 checks that the plan is for the customer on the ticket,
   that a refund is for that customer's own order and no more than is left of it, and that a credit
   is within the tier's allowance. On a violation, the model gets `semantic_resamples` fresh tries.
4. When the retries are spent, the `model_call` enforcer's `on_violation` decides. For PLAN-1 that
   is `escalate`: a person gets the case.

Every attempt is recorded, and the returned `CheckedPlan` carries its `attempts` and `verdict`, so
you can count how often a reply needed rescuing.

### 5.3 Why a resample is never told the rule

Format feedback is safe to give: "field `amount_cents` is not an integer" helps the model produce
valid JSON. Semantic feedback is not. If the model is told "the refund is above what is left of the
order (3000)", the next answer just says 2999. It passes the check without being any more right. So
a semantic retry is a fresh sample with no feedback (`semantic: none`). For a rule that marks itself
`compares_to: held_data` (the reply is judged against your own records), the loader refuses any
semantic resample at all, because sampling until the model agrees with your database is not
evidence of anything.

### 5.4 Handing a checked value to the next node

In an agent graph, the planner's output goes to another node: the executor that calls the tools.
How does the executor know the plan it receives was checked, and is still the plan that was checked?

`inv.model_call` returns a `Checked` value, here a `CheckedPlan`. It carries the reply, the
verdict and the attempts, and it is immutable. The consumer is marked as an **edge**:

```python
@inv.edge("PLAN-1")
def execute_plan(ctx: AgentContext, plan: CheckedPlan, customer_id: str) -> str:
    ...
```

Python cannot make a value unforgeable: any code can build a `CheckedPlan` with `object.__new__`, or
force a field with `object.__setattr__`. So the edge does not trust what it receives. On every call
it checks the verdict, re-validates the value against the closed schema, and evaluates PLAN-1 again,
against the arguments it was given and the ledger as it is now. In the support agent, that catches:

- a forged plan that never went through the planner;
- a plan checked for customer C-1 handed to the executor for C-2;
- a plan whose amount was forced up after the check (a buggy graph-state reducer);
- a plan that was fine when made, after the ledger moved (another refund happened in between).

The edge then hands the consumer a fresh `Checked` over exactly the value it checked, so nothing can
change it between the check and the use.

---

## Chapter 6. When a rule fires

### 6.1 Why a boolean is not enough

A naive guard returns true or false. But "the rule could not be evaluated" is a third answer, and
treating it as either of the other two causes trouble. Treat it as "fine", and a broken provider
silently disables your rule. Treat it as "violated", and a database outage becomes a flood of
rejections that look like policy decisions. So there are four outcomes.

### 6.2 The four outcomes

| Outcome | Means |
|---|---|
| `ok` | no clause fired |
| `violation` | a clause fired |
| `rule_error` | the rule could not be evaluated: a provider raised, a field is missing, a NaN was read |
| `parse_error` | a model reply did not fit the schema |

A rule error is never a violation, and never a pass. What happens next is decided separately.

### 6.3 What happens on a violation

Each enforcer has an `on_violation` action, which applies when the rule's mode is `enforce`:

| `on_violation` | Raises | Use it when | HTTP status (FastAPI and ASGI) |
|---|---|---|---|
| `reject` | `inv.Rejected` | the caller should get an error | 422 (configurable; 409 is common) |
| `halt` | `inv.Halted` | an internal invariant broke: this is a bug | 500 |
| `escalate` | `inv.Escalation` | a person must decide; a review is opened | 202 |
| `route` | `inv.Routed` | send the case to a review queue and stop | 202 |

Every exception carries `.rule`, `.outcome`, `.attempts`, `.point` and `.bound` (the values `bad`
read), and `Routed` and `Escalation` carry the `.review_id`. Reviews go to the configured review
store (`inv.MemoryReviews` by default, or `inv.JsonlReviews` for a file that survives restarts). If
no review can be opened, the call raises `inv.ReviewNotOpened` (HTTP 503), which carries the work so
it is not lost.

In `detect` mode nothing is ever blocked: the outcome is recorded and logged, and the call goes on.
That is how you roll a new rule out: watch it in detect mode, then promote it.

### 6.4 What happens on a rule error

`on_error` decides:

- **`fail_closed`** (the default) blocks the call with the enforcer's `on_violation` action, as for
  a violation; FastAPI and ASGI answer 503, because it is an outage, not a decision. Use it for
  anything that moves money or grants access.
- **`fail_open_and_page`** lets the call through and calls the configured pager. Use it where an
  outage of the check must not become an outage of the product.

There is also an optional circuit breaker:
`inv.configure(error_spike=inv.ErrorSpike(errors=20, window=60.0))` demotes a rule to `detect` and
pages when it produces that many rule errors within the window, until `Registry.restore(rule_id)`.
It is off by default on purpose: if a client could trigger rule errors with bad input, an automatic
demotion would let them switch the rule off.

### 6.5 Errors never leak data

Error messages, events and logs carry the rule id, the outcome and the clause messages, but never
the request's or reply's values, and never a provider's exception text (which might contain a
connection string). A parse error says which field failed and how, not what the model wrote,
because models echo user data. `inv.configure(debug=True)` shows the values in development.

---

## Chapter 7. The lock: making rules hard to loosen quietly

### 7.1 An agent's favourite shortcut

Everything so far is only as strong as the rule file. An agent that cannot make a test pass can edit
the rule instead: raise a number, delete a clause, add itself to `allow`, set a rule to `detect`.
Each of those is a one-line diff that looks like housekeeping. The lock exists so that every such
change is named, explained and stopped.

### 7.2 What the lock pins

`inv lock` writes `invariants.lock`, which records what each rule's meaning depends on:

- each rule's meaningful fields: `bad`, the enforcers with their modes and actions, the door and
  sink, `allow`, the model policy;
- each guarded function's signature and resolved types (with the fields of every model a signature
  names);
- each provider's declaration and signature;
- each contract's resolved schema.

It deliberately ignores what does not change meaning: a function body (refactor freely), comments,
docstrings, the prose fields of a rule (`statement`, `why`), whitespace and order. A body-only
refactor never makes the lock stale.

### 7.3 How a change is judged

`inv lock --check` (inside `inv check`) compares the code and rule file with the lock and names each
difference. Raising the refund cap from 50,000 to 500,000 prints:

```text
  FAIL  lock
        stale: invariants.lock does not match invariants.yaml: 1 change(s), 1 WEAKENED
          schema support_agent.models.RefundToolInput: pydantic.properties.amount_cents.maximum: 50000 -> 500000
            WEAKENED (maximum raised: larger values pass; used by contract refund_tool_input schema)
        review the changes, then run `inv lock --accept-weakening`
```

| Change | Judged |
|---|---|
| a lower status or mode, a removed rule, enforcer or clause, `fail_open_and_page`, more retries or resamples, a new door or `allow` entry, a removed sink, a looser schema, a `bad` that matches fewer cases | **weakening**: refused |
| a tighter schema, a removed `allow` entry, a `bad` that matches more cases | **tightening**: re-pinned automatically by `inv check --relock` |
| something it cannot decide | treated as a possible weakening, never as safe |

The interesting case is a changed `bad`. The lock parses both versions and works out whether the new
condition matches more cases or fewer. `args.amount > 1000000` to `args.amount > 2000000` matches
fewer cases, so the rule now forbids less: a weakening. Rewrites that mean the same (swapped
operands, regrouping, De Morgan, splitting one clause into two) are recognised as equivalent. It is
careful with types: `approved == true` to `approved == 1` is not treated as the same, because in CEL
`true` and `1` are different values and the new rule would never fire.

### 7.4 Who may accept a weakening

A weakening is sometimes right: the business really did raise the cap. Then a person accepts it:

```bash
inv lock --accept-weakening
```

But an agent can type that too. So CI does not trust the branch's own lock. It runs:

```bash
inv check --base origin/main
```

which compares the rule file with the lock committed on the base branch. A weakening the branch
accepted and re-pinned itself still fails there. A reviewer who agrees allows it explicitly with
`--allow-weakening`, or by adding a label that the GitHub Action turns into `INV_ALLOW_WEAKENING=1`.

---

## Chapter 8. How a rule earns trust

### 8.1 Status, mode and standing

Three words describe a rule, and they mean different things:

| Word | Set by | Values | Means |
|---|---|---|---|
| status | a person, in review | `candidate`, `contested`, `detect`, `enforce`, `retired` | how far the rule has been agreed |
| effective mode | computed | `off`, `detect`, `enforce` | what it does right now: the lower of the enforcer's mode and what the status allows, so only an `enforce` rule can block |
| standing | computed by `inv status` | `held`, `enforce(known-callers)`, `monitored`, `candidate`, `retired` | how much you can rely on it |

The loader refuses a status the evidence does not support. A rule can be `enforce` only if it has an
enforcer in mode `enforce`, tests that fail without it (a door rule's evidence is the scan instead),
and a timing that can actually stop something (section 4.6).

A rule is **held** only when it is enforced, its mediation names the chokepoint every path crosses,
`inv verify` and `inv scan` both pass, and it lists no residual paths. Otherwise it is
`enforce(known-callers)`: it stops the callers it knows about, and `inv status` lists the reasons it
is not held.

### 8.2 `inv verify`: is every rule wired?

A rule that names a function without a decorator protects nothing. `inv verify --import your.app`
imports the app and checks both directions: every function a rule targets carries its guard, and
every guard in the code names a rule in the file. Deleting `@inv.guard("BILL-1")` makes
`inv check` fail.

### 8.3 `pytest --inv-ablate`: does the guard do anything?

A rule can be wired and still useless, if no test would notice it disappearing. Ablation checks
that. `pytest --inv-ablate` turns each rule off, everywhere including subprocesses, and re-runs the
tests in its `tests` list. Each of those tests must now fail, and fail because of that rule. A test
must also pass on a normal run first, so a flaky test cannot make a rule look covered. The report
says, per rule, whether it is load-bearing.

In the support agent, BILL-1, BILL-2 and PLAN-1 are each load-bearing: switch one off and its tests
fail.

---

## Chapter 9. Making the check unskippable

### 9.1 How an agent gets around the lock

Even with a lock, an agent under pressure has moves:

- run `inv lock --accept-weakening` itself, or pass `--allow-weakening`;
- set `INV_ALLOW_WEAKENING=1` in a command;
- edit `invariants.lock` by hand, with a file-editing tool or a shell redirect;
- stop working while `inv check` is still red, and leave it for someone else.

None of these are exotic. They are the obvious next step for a tool that was told to "make the tests
pass".

### 9.2 Four layers

| Where | What runs | What it stops |
|---|---|---|
| the agent, before each tool call | `inv hook pretool` (a Claude Code `PreToolUse` hook) | a command that accepts or allows a weakening (`--accept-weakening`, `--allow-weakening`, `INV_ALLOW_WEAKENING`); any direct edit of `invariants.lock` (an Edit or Write tool on it, symlinks resolved, or a shell command naming it that is not clearly read-only); an edit of `[tool.invariants]` in `pyproject.toml` or a shell command that writes `pyproject.toml` |
| the agent, when it tries to finish | `inv hook stop` (a `Stop` hook) | finishing while `inv check` fails: the agent is sent back once, with the failure |
| each commit | `inv check --relock --fail-on-repin` (the pre-commit hook `inv-check`) | a commit that weakens a law; a tightening is re-pinned, and the commit fails once so the new lock is staged |
| each pull request | the GitHub Action, or `inv check --base origin/main` | a weakening, even one the branch re-pinned itself; a missing base ref or an unreadable base lock fails too |

The Claude Code configuration:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                           "hooks": [{"type": "command", "command": "inv hook pretool"}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "inv hook stop"}]}]}}
```

The matcher must include the file-editing tools, or the hook never sees an edit to the lock.

The two agent hooks are an early warning against an agent's shortcuts, not the gate: a text check of
a shell command can be talked past, and an agent may run without them. The gate is the last layer,
`inv check --base` in CI, which compares with the lock on the base branch whatever the branch did.

Last, protect the protection: put `invariants.yaml`, `invariants.lock`, the tests named in `tests`,
the hook configuration and the CI workflow under required human review (a CODEOWNERS entry). The
rule set is only as strong as the person who reads a weakening diff.

---

## Chapter 10. The three apps, and what was measured

### 10.1 The support agent

```text
ticket ─► triage ─► planner (@inv.model_call) ─► router ─► executor (@inv.edge) ─► tool ─► BillingService (@inv.guard) ─► StripeGateway / LedgerRepo
                         │ the model abstains, or retries are spent
                         └─► human handoff (a review is opened)
```

The graph registers its nodes as function values, as LangGraph does, and the executor dispatches
through a tool registry. The rule file started as what `inv init` wrote (section 2.2); three runtime
laws were then added: BILL-1 and BILL-2 on the billing doors, and PLAN-1 on the planner and its
executor. Twelve defects were seeded, each as a test. The ones a coding agent would make copy the
app, make the change, and run `inv check` in a fresh process:

| # | Defect | Caught by |
|---|---|---|
| 1 | a new tool calls `StripeGateway.refund` directly | the scan: bypass |
| 2 | `StripeGateway.refund` put in the tool registry | the scan: the sink used as a value |
| 3 | the refund cap raised from 50,000 to 500,000 cents | the lock; and `--base` when the branch re-pinned it |
| 4 | `semantic_resamples` raised from 1 to 5 | the lock |
| 5 | a new tool routed through the frozen back door | the scan: an allowed back door is not a door |
| 6 | the model over-refunds, uses another customer's order, or over-credits | PLAN-1 (one resample, then a person); BILL-1 and BILL-2 at the door for any caller |
| 7 | invalid JSON, an unknown tool name, an extra field | the closed schema: at most 3 attempts, then a person |
| 8 | the model answers `ESCALATE_TO_HUMAN` | abstain: a review is opened |
| 9 | a forged plan, or a plan checked for another customer | the executor edge |
| 10 | the plan changed after the check, or the ledger moved since | the executor edge checks again |
| 11 | a coding agent deletes `@inv.guard("BILL-1")` | `inv verify` |
| 12 | a coding agent deletes rule BILL-2 | the lock |

All twelve are caught, and the safe changes pass: a new tool that goes through the service, and a
tighter cap, which `--relock` re-pins with nobody's approval.

### 10.2 The pool service

A FastAPI and SQLite service with nine rules, all `enforce`:

- **R-1**: `split_work` returns one non-negative share per worker, none above its capacity, summing
  to the total: an after-check on a pure function.
- **R-7**: a model's proposed cause for a failed job never contradicts what the kernel recorded
  (an OOM claim needs `oom_killed`): a model call and an edge.
- **R-20**: leases never exceed a pool's size: a guard reading held data, and a path law on the
  lease writer. Its residual path says plainly that two concurrent leases can both pass the check;
  the store's own triggers refuse the second, below the API.
- **R-21, R-22**: a resize needs an approval by someone other than the proposer, and never grows a
  pool more than 4x: guards on the resize door.
- **R-23**: pool sizes are written only through the doors: a path law.
- **R-24, H-1, H-2**: bounds on pool size, lease size and pool ids, the last two on the HTTP routes.

### 10.3 The fact pipeline

A batch pipeline with ten rules. A fact needs an evidence span (K-1). A quote must sit at its
source offsets and the source must be current (K-2: a model call). A predicate must be in the
vocabulary (K-4). A fact's `valid_to` is never before its `valid_from` (K-7). A contradiction on a
single-valued property must have an owned conflict record (K-11: routed to a person). A fact is
believed only with two independent trusted sources (K-12). No id is ever supplied by the model
(K-18). An answer never cites a chunk it did not retrieve (K-19). Two rules are deliberately
`detect`: K-15 (no live fact resting only on a deleted source), which is a post-condition of an
external effect and so can only report, with a nightly monitor reconciling; and K-16 (no believed
fact citing a stale source), still being watched before it is promoted.

### 10.4 The measured experiment

Before the support agent existed, 24 changes were made to the other two apps, 16 defects and 8 safe
changes, and the same properties were also written as plain `if` checks plus tests, for comparison.

- **All 8 defects a rule covered were caught.** Four of them would have escaped the plain checks: two
  back doors (the scan), one rule weakened together with its test (the lock, for a reviewer who reads
  the diff), and one broken binding (a load-time check).
- **None of the 8 defects no rule covered were caught** by the library; ordinary tests caught three.
  It does not replace tests.
- After the lock was changed to pin contracts instead of whole modules, only 2 of the 24 changes
  made the lock fail, both for real reasons, and 1 of 8 safe changes needed a re-pin.

One author wrote the apps, the rules and the changes, so read these as a direction, not a rate. The
full method is in `docs/evaluation.md`.

---

## Chapter 11. Reference

### 11.1 The pieces

```text
invariants.yaml ──► loader ──► registry ──► @inv.guard / @inv.model_call / @inv.edge ──► outcome ──► action
                    (checks      (rules,      (binds args, calls providers,              (ok, violation,  (proceed, reject,
                     everything   targets,     evaluates CEL, records an event)            rule_error,      halt, escalate,
                     at load)     providers)                                               parse_error)     route)
invariants.yaml ──► lockfile ──► inv lock, inv check   (what is pinned, and the weakening gate)
source code     ──► scan ──────► inv scan, inv check   (the call graph and the path laws)
source code     ──► bootstrap, doors, graphify ──► inv init   (drafts the rule file, checked by the scan)
```

| Module | Does |
|---|---|
| `loader.py`, `typecheck.py`, `schema/` | reads the rule file, expands the short forms, refuses what cannot hold, type-checks every clause |
| `registry.py` | loaded rules, modes, ablation, reviews, standing, `verify` |
| `guard.py`, `providers.py`, `evaluate.py` | the decorators, provider binding, the evaluator and the actions |
| `cel/` | three CEL engines behind one parser: `subset` (the default, standard library only), `cel-expr` (Google's C++ runtime), `celpy` (pure Python); `INV_CEL_BACKEND` picks one |
| `scan.py` | the call graph and the path-law check |
| `lockfile.py` | the pins, the semantic diff and the weakening gate |
| `bootstrap.py`, `doors.py`, `graphify.py` | `inv init` |
| `cli.py` | the `inv` command |
| `pytest_plugin.py` | `--inv-rules`, `--inv-verify`, `--inv-ablate`, `--inv-report` |
| `integrations/` | FastAPI and ASGI (`application/problem+json`), pydantic (closed schemas, contracts), OpenTelemetry (one span per evaluation) |

### 11.2 Commands

| Command | Does |
|---|---|
| `inv init` | drafts `invariants.yaml` from the laws the code already has, then runs `inv lock` |
| `inv check [--relock] [--base REF]` | rules, lock, guards and scan, with one exit code |
| `inv validate` | the JSON Schema and every load-time check |
| `inv lock [--check] [--accept-weakening]` | writes or checks the lock |
| `inv verify --import MOD` | every target guarded, every guard names a rule |
| `inv scan [--format text\|json\|sarif]` | the path-law check |
| `inv status --import MOD` | each rule's standing, with reasons |
| `inv hook pretool\|stop` | the agent hooks |

Exit codes: 0 ok, 1 a check failed, 2 a usage error, 3 the scan has unresolved calls, 4 the scan
skipped an ablated `enforce` rule. Only 0 is a pass.

### 11.3 The rule file, in one page

```yaml
spec: invariant/v0.1
contracts:                                   # schemas pinned by the lock
  refund_tool_input: {type: pydantic, ref: "support_agent.models:RefundToolInput"}
targets:                                     # each guarded function's effects, declared once
  "py:support_agent.service.BillingService.refund": {effects: external}    # or none (pure)
providers:                                   # held data rules may read
  order: {type: "py:support_agent.models.Order | null", from: "py:support_agent.service.order",
          args: {ledger: self.ledger, order_id: args.order_id}}
rules:
  - id: STRIPE-GATEWAY-DOOR                  # a path law (short form)
    door: "py:support_agent.service.BillingService.refund"
    sink: "py:support_agent.repo.StripeGateway.refund"
    allow: ["py:support_agent.admin.admin_goodwill", "py:support_agent.admin.goodwill_refund"]
  - id: BILL-1                               # a value law (short form)
    guard: "py:support_agent.service.BillingService.refund"
    bad: 'order != null && args.amount_cents > order.remaining_cents'
    tests: ["tests/test_runtime_defects.py::test_d6_the_door_refuses_an_over_refund_whoever_calls"]
```

The short forms expand into the long form, which has every field: `status`, `subject`, `scope`,
`bind`, `enforcers` (each with `point`, `timing`, `mode`, `on_violation`, `on_error`, `view`),
`mediation` (`chokepoint`, `residual_paths`, `sink`, `door`, `allow`), `model_policy`,
`compares_to`, `falsified_by`. PLAN-1 in `apps/support-agent/invariants.yaml` is a full example.
Every field is described in `docs/rule-file.md`. Because the lock pins the long form, switching a
rule between the short and long forms is not a change.

### 11.4 Performance

Guard overhead per call, for one check before the call with no providers, on an Apple M4 Max with
Python 3.14:

| CEL engine | 1 clause, median | 1 clause, p99 | 4 clauses, median | 4 clauses, p99 |
|---|---|---|---|---|
| `subset` (default) | 7.1 µs | 8.4 µs | 22.7 µs | 28.3 µs |
| `cel-expr` | 9.6 µs | 10.7 µs | 22.0 µs | 27.9 µs |
| `celpy` | 86 µs | 100 µs | 830 µs | 892 µs |

Providers add their own cost on every call. `inv scan` reads about 1,850 functions in about a
second.

---

## Chapter 12. What it does not do

A tool that hides its limits gets trusted where it should not be. These are the honest ones.

1. **It only catches what a rule covers.** In the experiment, no defect outside a rule was caught.
   List the places where your system writes or spends; each one without a rule is either a gap or a
   decision to accept the risk.
2. **A provider is trusted.** If the provider that loads an order has a bug and returns the wrong
   balance, the guard reasons over the wrong balance. Test providers like any other code.
3. **A check is not a lock.** Concurrent callers can both pass a check before either writes.
   Atomicity belongs to the store.
4. **The scan is careful, not all-seeing.** It does not follow dependency-injection containers,
   event buses, callbacks, or callers outside the folders it scans. What it cannot follow on a path
   to a sink it reports as unresolved, never as safe. Raw SQL from code that never calls the
   repository is invisible to it; database permissions are what stop that.
5. **Door discovery is a heuristic.** It needs a layer to lock, and it reads names. Review what
   `inv init` proposes.
6. **`Checked` values can be forged in Python.** That is why every edge checks again.
7. **CEL is a common subset.** Rules use the grammar all three engines share, and a few functions
   exist only on some engines; the loader tells you.
8. **The lock reads rules, not behaviour.** It judges what a rule says, conservatively. A behaviour
   change inside a function body does not change the lock; that is what the runtime guards and tests
   are for.
9. **Python only, for now.**

---

## Appendix A. Words used in this handbook

| Word | Meaning |
|---|---|
| value law | a rule about what an API accepts or returns |
| path law | a rule about which API another may be called through |
| door | the API every call to a sink must pass |
| sink | the protected write |
| back door | a path to a sink that skips every door |
| `allow` | the back doors that existed when the rule was adopted, frozen |
| guard | a decorator that checks a value law on every call |
| provider | a typed, read-only function that gives a rule data the call does not carry |
| `bad` | the forbidden state, in CEL |
| edge | the consumer of a model's checked output, which checks it again |
| the lock | `invariants.lock`: what each rule's meaning depends on, pinned |
| weakening | a change that lets a rule forbid less |
| held | a rule that is enforced, mediated, verified and scanned, with no known way around it |
| ablation | switching a rule off to prove its tests notice |

## Appendix B. The repository

| Path | Holds |
|---|---|
| `invariants-py/` | the library, its test suite (about 3,950 tests, run on all three CEL engines) and its docs |
| `invariants-py/docs/` | `start.md` (the ten-minute start), `rule-file.md` (every field), `concepts.md`, `scan.md`, `lockfile.md`, `api.md`, `cel.md`, `cookbook.md`, `evaluation.md` |
| `apps/support-agent/` | the tool-calling agent and its twelve defects |
| `apps/pool-service/`, `apps/fact-pipeline/` | the other two apps |
| `action.yml`, `.pre-commit-hooks.yaml` | the GitHub Action and the pre-commit hook |

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e 'invariants-py[dev,cel]'
cd invariants-py && python -m pytest tests -q && python -m mypy
cd ../apps/support-agent && ./ci.sh
```
