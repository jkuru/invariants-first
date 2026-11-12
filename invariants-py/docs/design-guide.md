# Invariant-first

API-first put one machine-readable contract at the centre of a system for its *shape*: OpenAPI or Protobuf says what a request and a response look like, and clients, servers, docs and validators are generated from it. Invariant-first adds the layer above it, for the system's *laws*: the states that must never exist. People write the laws and the signatures. Agents write implementations that must satisfy them. Code, not the model and not the reviewer's attention, decides whether a result goes through. This guide supersedes "Invariants: what, how, when". It keeps that guide's core and corrects what did not survive review.

## 0. The problem, stated honestly

An agent produces more than a person can check.

A coding agent returns a change touching dozens of files in minutes; the reviewer skims, runs the tests and approves. A pipeline proposes thousands of facts; a person samples, and a wrong fact in the rest becomes something the system believes. **Review does not scale with output.**

Invariants change what a person checks. A person writes down the states no output may be in, and a machine checks every output against them. On every guarded path, that removes the mistakes the written rules exclude. It does not remove mistakes nobody wrote a rule for, and it does not make any output correct.

| | Without invariants | With invariants |
|---|---|---|
| A reviewer reads | every change, or a sample | rules and rule changes; every break; uncovered effects; a sample of outputs for meaning |
| The reviewer can stop reading for | nothing | only the mistakes held rules exclude, on the paths they guard |
| Code example | "does this diff look right?" | "no module in `ui` imports from `db`", checked on every diff |
| Knowledge example | "is this fact in the source?" | "no fact quotes text absent from its source", checked on every fact. That is *attributability*, not truth |
| What scales | the reviewer's hours | the check |

Two limits belong at the front.

- **Valid is not correct.** A restart for the wrong cause, on the right job, breaks no rule. Every property is the intersection of a safety property and a liveness property [6], and a rule file holds only the safety rules someone wrote. Gemini's docs warn of "schema-compliant but semantically incorrect outputs" [58].
- **The coverage gap moves; it does not close.** A break nobody wrote a rule for still passes. This guide makes the gap measurable (2.8) and keeps human sampling as a permanent layer [31].

Sections 4 and 5 began as a design. A first library now exists: `invariants-py/`, beside this guide, holds a Python SDK, the `inv` CLI and a pytest plugin, at version 0.1.0 and not yet released. Two separate apps in `apps/` use it only as an installed package. Section 4 describes what was built and says where it departs from the design, section 8 gives the measured numbers and a verdict, and Appendix A says how to run it all. The standard-library prototype the first draft described stays in `prototype/` as a teaching artifact.

**What changed from the original, and where.** Review raised twelve critiques of "Invariants: what, how, when". Each is answered here:

| # | Critique | Answer |
|---|---|---|
| 1 | The coverage gap is moved, not closed | Said up front (above); coverage measured on effect sinks (2.8) |
| 2 | The intro overstated what a reviewer can skip | Intro table and "valid is not correct" (above) |
| 3 | Retry-with-feedback turns invalid-wrong answers into valid-wrong ones | Bounded, reported retries (2.5) |
| 4 | "No known unguarded path" is a weak status | Positive mediation argument (2.4) |
| 5 | Business rules were excluded | Business rules are in; `class` sets ownership only (1.5) |
| 6 | "One record drives everything" breaks for shared state | The record is the oracle; conformance tests (2.7) |
| 7 | AST weaving into a shipped variant | Dropped for explicit attachments (2.3) |
| 8 | The knowledge use case was promised, not developed | Section 7 |
| 9 | Discover dropped candidates that real runs break | Contested queue (2.8) |
| 10 | "After" guards on effectful code only detect | Timing rule (2.2) |
| 11 | Rules bound by matching parameter names | Typed contract paths and a lockfile (2.1) |
| 12 | Evaluation errors counted as `bad = True` | Four outcomes (2.9) |

## 1. The concept

### 1.1 Definition

> **An invariant is a condition the system must never be in, written as a decidable predicate (`bad`) over one execution's states or steps, and held by an enforcer that every path to that condition passes through before the condition takes effect.**

- **"Must never be in."** A rule is a prohibition: "what state would be a bug?", not "what should happen?". This is a *safety property* [5][6].
- **"Decidable predicate over one execution."** The enforcer must compute it. Execution monitors enforce only safety properties, and information flow is not a property of single executions [13].
- **"Every path passes through."** Complete mediation: "every access to every object must be checked for authority" [7].
- **"Before the condition takes effect."** Schneider's monitor stops the target when it is "about to violate" the policy [13]; Dijkstra's guard is checked before its statement runs [4]. A check after a side effect *detects*. It does not hold the invariant.

Three companion terms name a rule's *standing*, a report computed from its status and evidence (2.4), not a field:

- A **candidate** is a written rule no person has approved yet (status `candidate` or `contested`). It may run in detect, as evidence for review.
- A rule is **monitored** when its enforcers only detect (effective mode `detect`, as for any post-effect check on effectful code).
- A rule is **held**, and is an invariant, when it is in `enforce` with a complete mediation argument and passes both coverage checks (2.4): its enforcer is always invoked, tamper-proof and small enough to verify. These are Anderson's reference-monitor requirements [8]. A rule enforced only at known callers is `enforce(known-callers)`: stronger than monitored, weaker than held.

A stack has an invariant the compiler cannot see:

```python
class Stack:
    def __init__(self):
        self.items, self.size = [], 0
    def pop(self):
        self.size -= 1          # bug: size changes before items.pop() can fail
        return self.items.pop()
```

The prohibited state is `size != len(items)`. The original's guard in a `finally` block raises after the stack is corrupted: detection. Better, strongest first:

1. **Make it unrepresentable.** Delete the redundant `size` [20][21].
2. **Check before the effect.** Refuse `pop` on an empty stack: Dijkstra's guard [4].
3. **Check the computed next state** on a copy before replacing the old one.

### 1.2 Who said what

| Source | Idea, and what this guide takes |
|---|---|
| Floyd [1], Hoare [2] | Per-step assertions: check rules locally, per move |
| Hoare [3], Liskov & Guttag [12] | Rep invariants belong to an implementation, abstract ones to the interface |
| Dijkstra [4] | Guarded commands: "prevent" means check before the step |
| Lamport [5], Alpern & Schneider [6] | Safety and liveness: rules hold only the written safety part |
| Saltzer & Schroeder [7], Anderson [8] | Complete mediation: status needs a named chokepoint |
| Meyer [9][11] | Class invariants: callbacks and leaked references are unguarded paths |
| Parnas [19] | Information hiding: attach a rule to the module owning the data |
| Schneider [13] | Monitors enforce only safety: classify which enforcer can hold each rule |
| Ligatti, Bauer & Walker [14] | Edit automata: `reject` suppresses, `halt` truncates, "pending" inserts |
| Hamlen, Morrisett & Schneider [17] | Rewriting is a different trust story: weave only with a trusted rewriter |
| Clarkson & Schneider [16] | Hyperproperties do not survive refinement: re-check flow rules per implementation |
| Minsky [20], King [21] | "Parse, don't validate": the guard is a parser |
| Vernon [22] | Invariants are business rules that set the transaction boundary |
| Jackson [23] | Separate concept-local rules from synchronization rules |
| Wayne [25] | Transition rules are their own kind |
| Kleppmann [26] | The hard part becomes the spec: humans own the rules |

### 1.3 Taxonomy: what a rule reads

What `bad` reads decides which enforcer can see it.

| Kind | `bad` reads | Example | Strongest enforcer |
|---|---|---|---|
| Value / type | one datum | `index` out of range | type, or a parser |
| Call contract | arguments and result | `cause ∉ set` | precondition; result guard on a pure function |
| Representation | one object's private fields | `size ≠ len(items)` | remove the redundancy |
| Abstract | the interface's abstract value | leases exceed pool size | owning module; storage |
| Shared state | rows across writers | a cross-row cap | DB constraint, serializable transaction, single owner |
| Transition | old and new state (`old_self`) | approved reverts to pending | guard on the next state |
| History / order | a finite event prefix | a write before its approval | history variable plus guard |
| Bounded response | counters, clocks | no reply within 5 s | counter guard; timeout plus fallback |
| Provenance | output versus data code holds | quote not in source | parse plus comparison |
| Code structure | program text | `ui` imports `db` | build scan; visibility rules |
| Information flow | several runs | untrusted text reaches the next agent | structure, labels, differential tests; no guard |

All are safety properties except the last two: a property of the program, and a hyperproperty. A bounded response is safety too, so it can conflict with other rules (1.7).

### 1.4 What an invariant is not

- **Unbounded obligations** ("eventually succeeds"): no finite run violates them. Use model checking or SLOs.
- **Evals** ("78% were right"): true of a population, never of one run. A recurring, decidable failure becomes a candidate.
- **Meaning** ("accurate"): not decidable. Use people and evals, or a closed field code can compare.
- **Scenarios** [23]: tests.
- **Prompt instructions, and sentences in CLAUDE.md or AGENTS.md**: "context, not enforced configuration" [97]. Use hooks, CI, runtime checks or storage.

### 1.5 Business rules are in

The original excluded business rules while using one as a core example. DDD defines invariants as business rules [22], and agent gateways enforce them in Cedar [62][65] or CEL [66]. What decides applicability is **logical form**: one-execution safety, decidable, with a chokepoint. Spend caps pass; SLAs (liveness) and fairness (hyperproperty) do not. The business/code distinction stays as metadata about **ownership**:

- `class: law | policy`. A law changes with the design and the code owner reviews it; a policy changes on business cadence and the domain owner reviews it.
- `compensable: bool`. Kleppmann's integrity versus timeliness [123]. If a violation can be detected and apologised for, detect and compensate. If not, prevent.

"A bishop moves straight" is a **boundary rejection of external input**, answered with `reject`. "Two pieces on one square" is an **internal invariant**, a bug, answered with `halt`.

### 1.6 Tests, evals, types

- **Tests** can falsify a rule but not establish it: "the presence of bugs, never their absence" [28]. Hence `falsified_by`. Property tests are sampled universals [42]; mutation testing checks that a test can fail [43].
- **Evals** measure distributions. Here they serve discovery.
- **Types** are the strongest enforcer. The guard returns a refined value (`Checked[CauseReply]`) only it should construct, and the protected effect accepts only that type [21]. The type checker then proves every honest caller passes the guard. Where code can forge the type, as in Python, the receiving edge also re-checks `bad` (2.6).

### 1.7 Rules can conflict

The original said prohibitions cannot conflict because doing nothing satisfies them all. False: the conjunction of safety properties is a safety property [6], but it can be empty, or leave a reachable state with no allowed move. Check that the rule set still allows some behaviour. When a deadline collides with a prohibition, meet it inside the prohibition: return "pending" or escalate [14]. Cedar uses forbid-overrides-permit [63].

### 1.8 From API-first to Invariant-first

| | API-first | Invariant-first |
|---|---|---|
| Central artifact | OpenAPI, Protobuf, JSON Schema [121] | A rule file bound to those contracts |
| Says | what a message looks like | which states never exist |
| Written by / implemented by | people / people or generators | people / agents |
| Payoff | clients, servers, docs, validators | validators, property tests, CHECK constraints, effect coverage |
| Brownfield ramp | generate a spec from code | Discover (2.8) |

Why the analogy holds:

- **Precedent at the message layer.** protovalidate puts CEL on Protobuf messages, stable since v1.0 [50][51]; Kubernetes CRDs carry CEL on OpenAPI schemas, including transition rules [54].
- **Contracts win between parties who cannot read each other's code** (an observation, not a measured claim). An agent is such a party.
- **Trace properties survive refinement** [16], whatever an agent implements.

Where it needs qualifying:

- Schemas carry **message-level** laws only. Aggregate, temporal and provenance laws need storage or a gateway, and flow rules must be re-checked per implementation [16].
- **Locked rules are a target the agent will optimise for** (2.6).
- **Ownership follows the two layers.** People lock *abstract* invariants; agents may propose *representation* invariants as candidates [3][12].

### 1.9 Laws on API boundaries, never below them

The section above holds at every layer, not only at HTTP. A system is a stack of APIs: the HTTP or RPC route (`POST /refunds`), the service method (`BillingService.refund`), the repository method (`BillingRepo.save_refund`), the model or agent call (`TriageAgent.propose_cause`) and the external client (`StripeClient.create_refund`). Rules belong on those boundaries, and there are two kinds:

- **Value law:** on this API, these inputs or outputs never occur (`args`, `result`, `reply`).
- **Path law:** this lower API is called only through that upper one ("only `BillingService.refund` calls `BillingRepo.save_refund`").

People own the boundaries. Agents are free inside the implementations, and what is below the lowest API (SQL, isolation levels, private fields) is that implementation's business. A rule that reaches below the boundary polices an implementation instead of a contract. It also ties the rule to one database, and it pulls in vocabulary (triggers, savepoints, consistency levels) that a team adopting the tool should not need.

Earlier drafts went below the API to answer critiques about concurrent writers and raw SQL (2.7, 4.1). Those mechanisms are still in the library: storage enforcers, `tables:`, `transactional` effects, provider consistency levels and `.sql` contracts. They are now opt-in and marked legacy, and the plan is to retire them. A path law at the repository API answers the raw-SQL critique without them: the scan proves no call path reaches the repository's write methods except through the service, whatever the repository talks to. Representation invariants (1.8) stay with the implementation, as candidates an agent may propose.

What this changes in practice (Appendix A): `inv init` writes only value laws (pinned pydantic schemas) and path laws (door rules from the service-to-repository calls the code already has); the door rule is four lines and needs no decorator; and the default check never parses SQL.

## 2. The approach

### 2.1 The rule record

A rule is one record: the source of truth for the predicate and the oracle every enforcer is tested against. It does not generate every enforcer (2.7).

```yaml
providers:                       # top level of the rule file: named, typed, read-only
  approval: {type: acme.pool.v1.Approval | null, from: "py:pool.providers.load_approval",
             args: {pool_id: req.pool_id}, consistency: same_txn}
targets:                         # effects declared once per target (2.2)
  "py:pool.PoolService.resize_door": {effects: transactional, transaction: "py:pool.PoolService.txn"}
rules:
- id: R-21
  rev: 2
  status: enforce                # set by human review; the loader refuses what the evidence cannot support
  class: policy                 # owner and change process; not applicability
  owner: ops-policy
  statement: A pool resize is never written without an APPROVED approval for that pool and size.
  why: An operator script resized a pool without approval through an endpoint that skipped the door (the Day 9 incident in section 6.2). Rev 2 adds the storage enforcer.
  subject: {contract: pool_api, rpc: acme.pool.v1.PoolService/ResizePool, target: "py:pool.PoolService.resize_door"}
  scope: call
  bind:                          # typed contract paths; a path that does not resolve fails the build
    req: request                 # acme.pool.v1.ResizePoolRequest
    approval: provider.approval  # read inside the write's transaction
  bad: 'approval == null || approval.state != "APPROVED" || approval.pool_id != req.pool_id || approval.new_size != req.new_size'
  # CEL's has() takes a field selection (has(a.b)), not a bare name, so test the provider's value against null
  compensable: false
  enforcers:
    - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject, on_error: fail_closed}
    - {point: storage, timing: pre_effect, mode: enforce, on_violation: reject, on_error: fail_closed,
       artifact: migrations/0042_resize_approval.sql#sha256:…,
       generated: false, conformance_test: tests/inv/test_r21_storage_differential.py}
  mediation:
    chokepoint: "table pools writable only by DB role pool_svc; trigger checks approvals"
    residual_paths: []
  falsified_by: [tests/inv/test_r21_property.py]
```

| Field | Meaning |
|---|---|
| `id`, `rev` | Immutable id; revision. Retired rules keep their id |
| `status` | `candidate`, `contested`, `detect`, `enforce` or `retired`. Required. Set by human review, verified by the loader; caps each enforcer's mode (2.4) |
| `class` | `law` or `policy`. Selects owner and approval workflow only |
| `owner`, `why` | Who answers for the rule; the harm it prevents, with a link |
| `subject` | A typed contract: OpenAPI operation, Protobuf RPC, Pydantic model, JSON Schema, table, tool, or pinned function signature; `target` names an entry in `targets:` |
| `scope` | The property's scope: `message`, `call`, `row`, `aggregate(key: …)`, `transition` or `trace(window=…)`. Each enforcer may have its own `view` (2.7) |
| `bind` | Names in `bad` mapped to typed contract paths or to providers (`provider.<name>`) |
| `bad` | CEL, true when the prohibited state exists, or a list of such clauses (4.1). Type-checked against `bind` at load |
| `compensable` | Whether a violation can be repaired afterwards (1.5) |
| `enforcers[]` | Each with `point` (type, storage, gateway, decorator, middleware, model_call, edge, build, structure, monitor), `timing` (`pre_effect` or `post_effect`), `mode` (`off`, `detect` or `enforce`), `on_violation`, `on_error`, an optional `view` (2.7), and artifact and conformance test if hand-written. Only an `edge` enforcer has its own `target`: the consumer it guards (2.6) |
| `mediation` | The chokepoint every path crosses, and `residual_paths` that go around it |
| `model_policy`, `compares_to` | Model calls only: retry limits, feedback, abstain; `compares_to: held_data` for comparisons with data code holds (2.5) |
| `falsified_by` | Tests that must fail when the guard is removed (checked by ablation) |
| `evidence` | Computed: reach, violation, rule-error counts; contested traces; standing |

Beside `rules:`, `targets:` declares each target's `effects` once (2.2), so two rules on one target cannot disagree, and `providers:` declares the state guards read.

**Providers.** A guard often needs state the caller does not pass, such as R-21's approval; state the caller passes can be stale or forged. A provider is named, typed and read-only. Its `type` may be nullable (`T | null`; in Python, `T | None`). Its `consistency` is `same_txn` (read inside the transaction that performs the effect), `snapshot` (one consistent point before the effect), `stale_ok` (any recent value) or `immutable` (data that never changes once written, such as a finished job's facts). A `same_txn` provider on a target that is not `effects: transactional` is a load error. Its `args` resolve in the binding environment of the rule that reads it, so R-21's `req.pool_id` uses R-21's `req`. The SDK calls it before a pre-effect check and binds its value by name. Values are cached per (provider, resolved args) per call, so two rules that bind differently on one target never share a value. A provider that raises or returns the wrong type gives `rule_error`, never a violation.

**Outcomes.** `on_violation` is `reject` (refuse this call; the caller gets an error), `halt` (stop the run: an internal invariant broke, so this is a bug), `escalate` (send to a person), or `route` (stop the call and open a review or conflict record, 2.9). There is no `retry` value: retries belong only to `model_policy`, and `on_violation` applies once they are spent. `on_error` is `fail_closed` (apply `on_violation`; the default in enforce mode) or `fail_open_and_page`. Detect mode never blocks. With several enforcers, the first on the path acts and the others are backstops; conformance tests check they agree on `bad`.

**Binding (critique 11).** The original matched rules to parameter names, so a rename silently detached a rule. Production CEL binds to schema paths (`object.spec.x`, `this.field`, `request.path`) [53][54][55], and so does this design. A coarse rule fans out only through an explicit `applies_to` selector, expanded into a reviewed lockfile, so any change in coverage is a diff. When `bind:` is omitted, `args`, `result` (or `reply` for a model call), `self` and `old_self` are bound automatically from the subject contract, and so is any provider name `bad` references; any other name needs an explicit `bind`.

**Transition rules (`old_self`).** As in Kubernetes CRD transition rules [54], the loader finds every `old_self.<path>` in `bad` and deep-copies only those paths before the call. Each must be plain data (numbers, strings, lists, maps, dataclasses of those). That check needs a real instance, so it runs at test time (`inv.check_snapshot`) and fails like a load error. A non-plain path reached at runtime is a `rule_error`, and nothing is copied. Bare `old_self` is a load error. `old_self` exposes only the referenced paths, so a database connection is never touched.

### 2.2 The enforcement ladder

From strongest to weakest:

| Rung | Mechanism | Holds against |
|---|---|---|
| 1 Unrepresentable | Types, closed enums, constrained decoding | every path, statically |
| 2 Atomic chokepoint at the data | DB constraint or trigger at adequate isolation, plus role grants | other programs and services, raw SQL |
| 3 Process or network gateway | Tool-call proxy, API gateway; agents hold no credentials | anything crossing the boundary |
| 4 In-process pre-effect guard | Decorator or middleware; presence checked by scan | known callers in the process |
| 5 Build gate on code shape | Scan, import rules, visibility | shipped code; code-structure rules only |
| 6 Post-effect in-process check | Detects; may compensate | nothing |
| 7 Live monitor | `bad` over stored data and traces | nothing; it reports |
| 8 Model judge | Code still decides | weakly |
| 9 Prompt or convention | — | not enforcement |

**The timing rule (critique 10).** A post-effect guard on a target with side effects is detection. The timing check fails for `mode: enforce` with `timing: post_effect`, capping the rule at `detect` (2.4), unless the target's entry in `targets:` declares `effects: none` (pure: the only "effect" is returning the result) or `effects: transactional`. A target missing from `targets:` is `external`. A transactional target names its boundary (`transaction: …`). The SDK's wrapper runs the post-effect guard inside it, before commit, and rolls back on violation, but only when it confirms at runtime that the target runs inside that boundary; otherwise the outcome is `rule_error`. If the caller swallows the guard's exception inside the transaction, the SDK marks it rollback-only and raises `RolledBack` at exit, so the bad write never commits. A declaration trusted without the runtime check goes under `residual_paths`. Otherwise rewrite the rule as a precondition or a check on the computed next state. Model calls are usually pure, so an after-guard on one can prevent; one on `restart_job` cannot.

### 2.3 Where enforcers attach

- **Types and schemas**, including structured outputs for model replies.
- **Explicit decorators and middleware**, visible in review and stack traces.
- **Gateways**: a tool-call proxy checks rules before the call. Agent rules belong here; in STAMP terms tool calls are control actions [29].
- **Storage**: `CHECK`, `UNIQUE`, `FOREIGN KEY`, triggers with `SELECT … FOR UPDATE` or `SERIALIZABLE` [103][104], and role grants as proof of one door.
- **Build**: import layering [70], architecture rules [69], visibility. Bazel fails a build that "violates the visibility" [71]: an unseen path cannot exist.

**No weaving (critique 7).** The original rewrote the syntax tree to insert guards and shipped that "variant". Inlined monitors are sound only if the program cannot route around them [18], and Python allows `getattr`, monkeypatching, importing the unwoven module and `exec`. Weaving added no strength over a decorator, and shipped unreviewed code. Use explicit attachments. A rule needs two checks to be held. **`inv verify`** reads the runtime registry: every rule's target is wrapped by its guard, and every guard names a rule in the file. **`inv scan`** reads the code graph: no path from an entrypoint reaches a protected effect sink without passing the door (2.4). Only the scan sees code that calls the sink directly, such as R-23's bypass (6.2).

### 2.4 Status: reviewed, verified, and narrow

```
candidate --(a real run breaks it)--> contested --(person rules)--> detect --> enforce --> retired
                                       bug / weaken / dismiss          ^          |
                                                                       +--demote--+
```

Only human review sets `status`, and every rule must have one: a missing status is a load error. The loader computes the highest status the evidence allows and refuses to load a rule whose status is higher. `enforce` needs at least one enforcer, `falsified_by` and a passing timing check (2.2); a failing timing check caps the rule at `detect`.

| Transition | Requires |
|---|---|
| candidate → detect | Owner approval; bindings resolve; CEL type-checks |
| detect → enforce | Reached N times on real traffic; zero rule errors over a soak window; every detect-mode violation reviewed; a falsifier verified by ablation; conformance tests pass for hand-written enforcers; a stated `mediation` |
| enforce → detect | Paged: a rule-error spike or logged break-glass drops the effective mode to detect at once; review then sets the status |
| any → retired | Target gone (the build fails until retired or re-bound), superseded, or retired by the owner. History kept |

**Status caps mode.** An enforcer's `mode` (`off`, `detect`, `enforce`) is what it would do. Its effective mode is `min(mode, cap(status))`, with `off < detect < enforce`. The cap is `detect` for `candidate`, `contested` and `detect`; `enforce` for `enforce`; `off` for `retired`.

**Standing is a report.** `inv status` computes it (1.1). A rule is held only when its status is `enforce`, its `mediation` names a complete chokepoint, `inv verify` and `inv scan` both pass (2.3), and any guard reading through a provider other than `same_txn` or `immutable` has a storage enforcer behind it (2.7). Otherwise an `enforce` rule is `enforce(known-callers)`.

**"No known unguarded path" was too weak (critique 4).** It made absence of knowledge a status, and syntax-only scans miss `getattr`, reflection, raw SQL and dependency injection [68]. Replace it with a **positive mediation argument** naming the chokepoint:

- "table `leases` writable only by DB role `lease_svc`";
- "`restart_job` accepts only `ApprovedRestart`, constructible only in `approvals.py`";
- "tool calls pass through gateway X; agents hold no credentials".

Scanner findings it cannot resolve count as `unresolved`, never passed. In-process Python cannot give complete mediation; process, network and database boundaries can.

**Mediation as a graph query.** A code graph (a CodeQL database [127], a SCIP index [126], or a tree-sitter graph [124][125]) makes the argument checkable. Remove the chokepoint node, the door or guard, from the graph of entrypoints, calls and writes. If any path from an entrypoint to the protected effect sink survives, it is a bypass: it goes under `residual_paths` or the build fails. This query is `inv scan`; R-23 (6.2) runs it for `pool_store.set_size`. In graph terms, the chokepoint must dominate the sink from every entrypoint. Centrality can suggest a "narrow waist" where one guard covers the most callers, for a person to place. The graph serves discovery, mediation and change impact; it never enforces at runtime (2.3). Only extracted edges count as proof. Inferred edges, such as those a model pass draws to docs [124], only suggest.

**Reach counters.** A rule never evaluated says nothing, which is why Antithesis pairs "always" with "sometimes" assertions [40]. Every rule reports how often it ran.

### 2.5 Model calls

**The model proposes. Code checks. Code decides.**

```
model reply
   |
   v
STEP 0  structured output (provider constrained decoding)    shape only
STEP 1  PARSE into a closed schema, extras forbidden          -> parse_error
        reply.<field> == abstain value?  no bad, no resample  -> escalate
STEP 2  EVALUATE bad on the typed value                       -> violation | rule_error
STEP 3  accept: return the value with its verdict             -> Checked[CauseReply]
```

**Steps 0 and 1.** OpenAI, Anthropic and Gemini constrain decoding to a JSON schema [56][57][58], but Anthropic does not support `minimum`, `maximum` or `minLength`, refusals can still break the schema [57], and Gemini says to "always validate values in your application" [58]. Decoding enforces shape; values need `bad`. A closed schema with no `id` field means the model cannot supply one.

**Step 2.** Four kinds of comparison, strongest last (`source` and `job` come from providers):

| Kind | Example `bad` | Catches |
|---|---|---|
| Closed set | `!(reply.cause in ["TIMEOUT", "OUT_OF_MEMORY", "BAD_INPUT"])` | a made-up cause |
| String shape | `size(reply.message) > 500` | too long |
| Against source text | `!source.contains(reply.quote)` | an invented quote |
| Against data code holds | `reply.cause == "OUT_OF_MEMORY" && !job.oom_killed` | a cause the record contradicts |

A `bad` clause that only restates a closed enum is reported as "held by the schema (rung 1)", not as an error. R-7 (4.2) keeps its enum in the schema and uses `bad` against data code holds.

**Step 3.** Return `Checked[CauseReply]`, not raw text. It carries `attempts` and a `verdict` (`ok`, `violation`, `rule_error`, `parse_error`). Detect mode returns it whatever the verdict: it skips semantic resamples, and a parse that runs out of retries returns verdict `parse_error`. Effective mode `off` acts as detect for a model call, so it never returns verdict `ok` unchecked. `fail_open_and_page` returns verdict `rule_error`, never `ok`. An enforcing edge refuses any verdict but `ok` (2.6), so detect mode never launders an unchecked value.

**Retries are bounded and reported (critique 3).** Instructor [59], PydanticAI [60] and Guardrails AI [61] re-prompt with the failure. Feeding it back pushes the model to pass the check, not to be right [44], which can turn an invalid wrong answer into a valid wrong one. In ImpossibleBench, test feedback raised cheating from 33% to 38% while legitimate passes rose only from 80% to 83%, and an abort option cut GPT-5's cheating from 54% to 9% [45]. External feedback helps [46], but only toward what the guard measures. `model_policy` controls only retries (R-7 in 4.2):

- **Parse failure**: retry with the parser error, at most `format_retries` (2) times. It reaches the model through an `inv_feedback: str | None` parameter, which the loader requires when `feedback.format` is `parser_error`.
- **`bad` true**: at most `semantic_resamples` (1) fresh resamples, with `feedback.semantic: none`.
- **Mismatch with data code holds**: mark the rule `compares_to: held_data`. The loader then forces zero resamples and no semantic feedback.
- **`rule_error`**: never resampled; `on_error` applies.
- **Abstain, always**: `abstain: {field, value}`; a closed enum must contain the value. If `reply.<field>` equals it, the call escalates at once, with no `bad` and no resample, in every mode. This is fixed.
- **Once retries are spent**, the enforcer's `on_violation` decides, as for any enforcer. `model_policy` has no outcome field.

Every accepted output carries `attempts=n`, and people sample retry-accepted outputs more. A high per-rule rescue rate means the model is learning the check.

### 2.6 Agents, edges and untrusted text

Each agent is a node and each handoff an edge. Edge rules run before the next agent does.

- **No edge passes raw text.** The next agent gets only a typed value built by code.
- **Edges re-check `Checked`.** Accepting only `Checked` by type stops honest mistakes, but Python can forge one with `object.__new__`, so the type alone is `enforce(known-callers)`. For held, the rule adds an enforcer `{point: edge, target: <consumer>}`. The edge checks the verdict and re-evaluates `bad`. It fetches any held data itself, through providers keyed on its own arguments; the loader checks those paths resolve against the edge's signature. It never trusts values carried inside the `Checked`, because a forger would forge them too. In enforce mode it refuses a verdict other than `ok` and a forged bad value; in detect mode it reports.
- **Untrusted text stops at the first agent**, as in Willison's dual LLM pattern, which passes on only verified output "such as a classification into a fixed set of categories" [32].
- **Flow rules are held by structure.** "Untrusted text never reaches the next agent" is an information-flow rule [16]: held by having no field for it, marked `point: structure`, tested differentially.
- **Provenance needs labels.** CaMeL checks provenance at each tool call, solving 77% of AgentDojo tasks with provable security against 84% undefended [33]; FIDES carries labels across agents [34]. Adopt such a layer for taint rules, or list them under `residual_paths`.
- **Loops and callbacks.** Cap hops graph-wide; never call out mid-update [11].

**The enforcement chain is attack surface.** A coding agent under pressure will add a bypass. Put the rule file, lockfile, falsifiers, guard library, hook configuration and CI workflow under human-only review, forbid bypass idioms in the scan, and back such rules with storage or a gateway.

### 2.7 Concurrency and shared state (critique 6)

Two callers can both pass a guard against the same stale state and both write. A rule over shared data needs check and commit as one atomic step.

| Mechanism | Covers |
|---|---|
| `UNIQUE`, `CHECK`, `FOREIGN KEY` | one row or key |
| Trigger reading other rows | cross-row caps, if writes are serialized: SQLite has one writer [106]; PostgreSQL needs `SELECT … FOR UPDATE` [103] or `SERIALIZABLE` [104] |
| Conditional write / version check | stores without triggers |
| Single owner, or idempotency key + `UNIQUE` | one service's writes; retries |

**Providers and the race.** Only a `same_txn` read happens inside the transaction that performs the effect, so only it closes the race. An `immutable` read has no race to close, so it counts the same. A guard that reads through a `snapshot` or `stale_ok` provider is an early refusal only: two callers can still both pass. Such an `enforce` rule counts as held only with a storage enforcer behind it. The record states the race.

**One property, a view per enforcer.** The rule's `scope` is the scope of the property: for R-20, `aggregate(key: pool_id)`. Each enforcer may declare a `view`: how it obtains the bound values. `bad` is written once:

```yaml
bad: 'leased + n > size'
enforcers:
  - {point: decorator, timing: pre_effect, mode: enforce, on_violation: reject,
     view: {leased: provider.pool_leased, n: args.slots, size: provider.pool_size}}   # snapshot reads: early refusal
  - {point: storage, timing: pre_effect, mode: enforce, on_violation: reject,
     view: rows}                         # the trigger computes the same names from rows grouped by pool_id
```

The conformance test below fills each view and checks they agree.

"One record drives everything" broke here: the SQL is hand-written. The honest version is **one record is the oracle everything is tested against**:

- Generate where translation is exact (single-row `CHECK`, protovalidate). Otherwise hand-write the enforcer and record a **conformance binding**: its hash plus a test re-run whenever either side changes.
- That test is a **concurrent history check at the declared isolation level**, in the line of Elle [37], FoundationDB simulation [38] and TigerBeetle [39]. At minimum: a forced interleaving (two callers read the same stale count; without the trigger the pool is leased 120/100), and a differential test where storage accepts a row exactly when `bad` is false. Cedar is the precedent for differential testing: its Rust implementation is tested against a Lean model; proofs found 4 bugs and testing 21 more [64].
- Ship storage rules `NOT VALID`, then `VALIDATE CONSTRAINT` once the monitor has found and fixed old rows [102].

### 2.8 Discover: the brownfield ramp

Discover proposes candidate rules for existing code. Like generated OpenAPI, its output describes **current behaviour, bugs included**: **characterization rules** until a person rules on each.

**Inputs, most precise first:** existing checks; an **effect inventory** from a deterministic call-chain pass listing every effect sink (DB write, outbound call, tool call, model call, process start); incidents; STPA hazards [29][30]; and a model proposing what must never happen at each call. The inventory and each sink's 1–2 hop neighbourhood in the code graph (callers, the schema it writes, existing checks, linked docs) are what the proposer sees.

**Static edges are incomplete.** The call-chain pass misses interfaces, dependency injection, callbacks, event buses and dynamic dispatch. Supplement it with runtime traces from the test suite, staging and canaries, as OpenTelemetry spans or via Python's `sys.monitoring`. Traces record actual calls, arguments and sampled return values; feed them to the proposer and to the replay and mutant filter. The static pass still produces the effect inventory, and traces fill in the edges: merge them into the code graph, marked as observed. That closes part of the dynamic-dispatch gap, not all of it. A path no test or canary runs still has no edge.

**Steps:**

1. Code parses each proposal and checks that its target and bindings exist and type-check.
2. Replay in detect mode over tests and traffic: **never reached** (vacuous; flag it), **never broken** (proceed), or **broken** (**contested**, sent to a person with the trace).
3. Run survivors against seeded mutants. One that catches none ranks last, not deleted.
4. Rank by hazard times evidence. **Rarely broken candidates rank highest**: that is where latent bugs live.
5. Rate-limit each reviewer's queue against rubber-stamping.

**Why broken candidates go to a person (critique 9).** The original dropped them, inheriting Daikon's assumption that observed runs are correct [47]. TestGen-LLM can drop failing tests because they encode current behaviour [75]; rules cannot. Agentic property-based testing found 56% of its reports were valid bugs [72], ACH starts from a named concern and keeps tests that *fail on a mutant* [76], and incident-sourced rules are broken by construction. A person rules: bug (the rule enters detect), too strong (weaken it), or noise (dismiss and record).

**What Discover cannot do (critique 1).** It cannot find a rule nobody proposed, and it is the least proven part of the design. So coverage is measured on **effects**: each sink needs a rule or a signed "no rule: accepted risk" entry, and a new sink with neither fails the build. On the code graph the metric is one query: effect sinks with no path through a guard and no storage constraint.

### 2.9 Rule errors are not violations (critique 12)

The original treated any evaluation error as `bad = True`, causing false outages. Every evaluation now has one of four outcomes:

| Outcome | Meaning | Handling |
|---|---|---|
| `ok` | `bad` is false | proceed |
| `violation` | `bad` is true | the enforcer's `on_violation` |
| `rule_error` | type error, missing field, cost limit, runtime error, failed provider | the enforcer's `on_error` (`fail_closed`, the default in enforce mode, applies `on_violation`; or `fail_open_and_page`); always counted separately; in test and Discover a rule defect, never evidence |
| `parse_error` | the model reply did not parse | the model policy |

**Edges.** Detect never blocks: every outcome is reported and the call proceeds, whatever `on_error` says. `route` stops the call: the SDK raises `Routed` with a review id, and a person's later decision resumes or drops the work, outside the call. In a clause list a true clause wins over an errored one (4.1).

CEL separates parse, check and evaluate [48], so type-checking at load and in CI turns most rule errors into build failures. Kubernetes likewise separates evaluation errors from type-checking [53]; Google IAM treats an unevaluable deny as true [55]. A rule-error spike drops the rule to detect and pages the owner (2.4).

## 3. Industry references

### 3.1 Landscape

| Piece | Closest practice | Borrow |
|---|---|---|
| CEL on a typed target | Kubernetes [53][54]; protovalidate [50]; IAM Conditions [55]. Production at scale | Schema-path binding; type-check at authoring; cost limits |
| Model output | Structured outputs [56][57][58]; Instructor, PydanticAI, Guardrails AI [59][60][61] | Bounds still need `bad`; record attempts |
| Agent tool-call guard | AgentCore Policy [62]; Docker MCP gateway [65]; agentgateway [66]; Invariant Labs, now Snyk [67]; AgentSpec [36]. Fast-moving | Gateway placement; default deny |
| Injection resistance | Dual LLM [32]; CaMeL [33]; FIDES [34]. Research | Guard data flow, not only values |
| Build-time scan | Semgrep [68], ArchUnit [69], import-linter [70], Bazel [71] | ArchUnit's freezing of existing violations |
| Weaving | JavaMOP [74]. Research | Not adopted (2.3) |
| Code graphs and code-query databases | CodeQL [127]; SCIP [126]; Glean [128]; Graphify [124]; CodeGraph [125] | Mediation and coverage as path queries; change impact. Discovery only, never runtime enforcement |
| Tests and data checks | Hypothesis [41]; Schemathesis [107]; Kiro [73]; Cedar [64]; dbt [81]; Antithesis [40] | State machines from rules; dbt *is* `bad` in SQL |
| Discover | Daikon [47]; TestGen-LLM [75]; ACH [76]; AgentSpec [36]; agentic PBT [72]. Industrial for tests, not rules | Concern-targeted mutants; contested queue |
| Proofs | Vericoding: Dafny 82%, Verus 44%, Lean 27% [80]; Kleppmann [26] | The rule file is the spec |

Kiro can "surface options to fix the spec" on a test failure [73]; Spec Kit's gates are checked by the LLM itself [99]; agents "often ignored instructions" in specs [98]. A protected rule file is the difference.

### 3.2 What is genuinely new

None of the parts. As far as the research found, no single product or paper does these:

1. **One negated rule record tested against several mechanisms**: guard, gateway, storage, property tests, monitor. Existing tools each own one layer.
2. **A reviewed lifecycle the loader verifies**, with positive mediation, ablation-verified falsifiers and reach counts.
3. **Discover for rules, filtered by replay and mutant kill, with a contested queue.** Meta filters *tests* this way.
4. **Extending CEL-on-schema** to call sequences, model outputs, agent handoffs and storage.

## 4. Making it a library

The library exists. `invariants-py/` is the Python SDK, version 0.1.0 and unreleased. Its distribution name is `invariants-first`, a placeholder until publishing, and its import name is `invariants`. It has a rule-file loader with load-time checks, five decorators, three CEL backends, the `inv` CLI, a pytest plugin, and FastAPI, pydantic and OpenTelemetry integrations. This section describes what was built. Where building or certifying it changed the design, the text says so, and why when that is a lesson. Section 4.3 ends with what the design proposed that v0.1 does not have. Every rule-file field is listed in `invariants-py/docs/rule-file.md`.

### 4.1 The spec

The template is protovalidate: a spec, a native runtime per language, a conformance suite run through an executor protocol, and a stability promise at 1.0 [50][51][52]. v0.1 has one runtime, Python.

- **Files.** `invariants.yaml` (or JSON) is edited only by people. `invariants.lock` is generated by `inv lock` and reviewed in diffs (4.3). There is no candidates file yet, because there is no Discover. Besides `spec: invariant/v0.1`, a rule file has four top-level maps: `contracts`, `targets`, `providers` and `rules`. A bundled JSON Schema backs `inv validate`, and an unknown field is a load error at every level.
- **Targets.** Each key is a `py:` path. Its entry declares `effects` once: `none` (pure), `transactional` (names its `@inv.transaction` boundary with `transaction:`) or `external`. An unlisted target is `external`.
- **Contracts.** These are `python` (a module), `pydantic`, `dataclass`, `schema` (a TypedDict or `{field: type}` map) and `openapi`. Any other file is pinned by digest. Protobuf contracts and 2.1's `subject.rpc` are not in v0.1. An HTTP rule names an `openapi` contract and a `subject.operation`, but it binds to handler parameter names, not to OpenAPI JSON paths.
- **Providers.** Each is `{type, from, args, consistency}`. `type` is a builtin or `py:mod.Class`, optionally `| null`, and must equal the function's return annotation. `from` must be marked `@inv.provider("<name>")`, and its parameters must be exactly the keys of `args`. A `pre_effect` enforcer reads its providers before the call. A `post_effect` enforcer reads them after the effect, inside the transaction, except `snapshot` providers, which are read before it. The first build read every provider before the call, so a post-effect provider check was vacuous; the two outside apps found that. Providers are eager: every provider an enforcer reads is called on every call, which costs time (8.2).
- **CEL only**, with a small extension library (`inv.sum`, `inv.zip`, `inv.unique_by`), each extension with conformance cases. Each extension's edge cases are part of the spec. `inv.zip` on lists of unequal length is a `rule_error`, not a silent truncation, so a rule that zips must also state the length as its own clause (see R-1). Prefer standard macros (`all`, `exists`, `filter`, `map`).
- **All rule logic lives in the rule file.** No per-rule helper functions in the host language; the only functions beyond standard CEL are the fixed, conformance-tested extensions. A reviewer sees the whole predicate, and every SDK agrees. R-1 in 4.2 writes `inv.sum(result) != args.total_jobs || inv.zip(result, args.capacities).exists(p, p[0] < 0 || p[0] > p[1])` inline, not as a call to a Python `check_split()`.
- **Why CEL fits the hot path.** It is not Turing-complete and has no side effects. It is type-checked ahead of time against the contract (2.9), and cost limits bound the work per call [48][53].
- **Mind the negation.** `bad` is a prohibition, true when the rule is broken. Kubernetes and protovalidate rules have the opposite polarity, true when valid [50][54]. Porting a rule or negating a compound condition invites De Morgan mistakes: `!(a && b)` is `!a || !b`. So `bad` may be a list of simple prohibited clauses. Any true clause is a violation, with its own message:

  ```yaml
  bad:
    - {when: 'size(result) != size(args.capacities)', message: one share per worker}
    - {when: 'inv.sum(result) != args.total_jobs', message: shares do not sum to total_jobs}
    - {when: 'result.exists(s, s < 0)', message: negative share}
    - {when: 'inv.zip(result, args.capacities).exists(p, p[0] > p[1])', message: share above capacity}
  ```

  A list is easier to review than one long disjunction. Every clause is evaluated. Any true clause gives `violation`, and the report names every clause that fired; otherwise any errored clause gives `rule_error`; otherwise `ok`. This matches CEL's commutative `||` [48].
- **Refuse what CEL cannot compare honestly.** Every IEEE comparison with NaN is false, so `reply.confidence < 0.0 || reply.confidence > 1.0` passes a NaN. Certification found four ways in: the `NaN` token `json.loads` accepts, the string `"NaN"` pydantic's lax mode coerces, a clause's own `0.0 / 0.0`, and a NaN nested in a list compared whole. Now a non-finite number in a model reply is a `parse_error`. One that a clause reads or makes is a `rule_error` on every backend, unless `inv.configure(allow_non_finite=True)`. Pydantic replies are validated strictly. Likewise, int and double do not mix in arithmetic, and a naive `datetime`, `Decimal`, `date` or set is refused with the conversion to make. One documented mapping reads Python values on every backend.
- **Load or refuse; never load and fail every call.** Many review findings had one shape: a rule that loaded, then gave `rule_error` on every call, which under `fail_closed` is an outage. Unknown functions, wrong arities, missing fields and unreadable datetimes all did this. The loader now type-checks each clause against target annotations, provider types and the reply schema, and checks every function and arity against the chosen backend.
- **Versioning.** At 1.0, valid 1.x rule files stay valid. OpenAPI 3.1 broke compatibility without a major bump [121]; do not.

### 4.2 Example rule file

This is the syntax `inv validate` accepts. The apps' rule files are larger worked examples (Appendix A): `apps/pool-service/invariants.yaml` holds R-1, R-7 and R-20 to R-24 in full.

```yaml
spec: invariant/v0.1
contracts:
  jobs_py:  {type: python,   ref: jobs}
  triage:   {type: pydantic, ref: "triage.models:CauseReply"}
  pool_api: {type: openapi,  ref: openapi.json}       # protobuf contracts are not in v0.1
targets:                                  # effects declared once per target; pinned in the lockfile
  "py:jobs.split_work":       {effects: none}     # pure, so a post-effect guard can enforce
  "py:triage.propose_cause":  {effects: none}
  "py:triage.remediate":      {effects: external}
  "py:pool.PoolService.resize_door": {effects: transactional, transaction: "py:pool.PoolService.txn"}
providers:                                # R-21 binds approval: provider.approval (2.1)
  approval: {type: "py:pool.Approval | null", from: "py:pool.providers.load_approval",
             args: {conn: self.conn, pool_id: req.pool_id}, consistency: same_txn}   # req: R-21's bind
  job:      {type: "py:triage.models.JobFacts", from: "py:triage.providers.load_job",
             args: {job_id: args.job_id}, consistency: immutable}   # a finished job's facts never change
rules:
  - id: R-1
    rev: 3
    status: enforce
    class: law
    owner: team-scheduling
    statement: split_work never returns a negative share, a share above its worker's capacity, or shares that do not sum to total_jobs.
    subject: {contract: jobs_py, target: "py:jobs.split_work"}
    bad: >
      size(result) != size(args.capacities) ||
      inv.sum(result) != args.total_jobs ||
      inv.zip(result, args.capacities).exists(p, p[0] < 0 || p[0] > p[1])
    enforcers:
      - {point: decorator, timing: post_effect, mode: enforce, on_violation: reject, on_error: fail_closed}
    falsified_by: [tests/test_split.py::test_r1_property]
    why: A rounding bug corrupts every job record above it.

  - id: R-7
    rev: 2
    status: enforce
    class: law
    owner: team-sre
    statement: A proposed cause never contradicts what the scheduler and the kernel recorded for the job.
    subject: {contract: triage, target: "py:triage.propose_cause"}
    # CauseReply.cause is a closed Literal (TIMEOUT, OUT_OF_MEMORY, BAD_INPUT, CANNOT_COMPLY): rung 1
    bad:                            # job is the provider above, bound by name
      - {when: 'reply.cause == "OUT_OF_MEMORY" && !job.oom_killed', message: "OOM claimed, but not OOM-killed"}
      - {when: 'reply.cause == "TIMEOUT" && job.elapsed_s < job.timeout_s', message: "TIMEOUT, but finished inside its limit"}
      - {when: 'reply.cause != "OUT_OF_MEMORY" && job.oom_killed', message: "OOM-killed, but cause is not OUT_OF_MEMORY"}
    compares_to: held_data          # forces semantic_resamples: 0 and feedback.semantic: none
    model_policy:
      schema: CauseReply            # extra fields forbidden; no id fields
      format_retries: 2
      semantic_resamples: 0
      feedback: {format: parser_error, semantic: none}
      abstain: {field: cause, value: CANNOT_COMPLY}   # after parse, before bad: escalates at once
    enforcers:
      - {point: model_call, timing: post_effect, mode: enforce, on_violation: escalate, on_error: fail_closed}   # applies once retries are spent
      - {point: edge, target: "py:triage.remediate", timing: pre_effect, mode: enforce,   # re-validates, re-checks bad (2.6)
         on_violation: reject, on_error: fail_closed}   # an edge needs this rule's model_policy, or load fails
    mediation:
      chokepoint: "remediate, the only consumer, is an @inv.edge"
      residual_paths: []
      sink: "py:triage.store.insert_remediation"       # inv scan: no path reaches the sink around the door
      door: "py:triage.remediate"
      tables: [{remediations: [insert, update]}]       # raw SQL writing remediations elsewhere is forbidden
    falsified_by: [tests/test_triage.py::test_r7_held_data, tests/test_triage.py::test_r7_forged_checked]
```

Building it changed `mediation`: it now carries `sink`, `door` and `tables` beside the `chokepoint` sentence, because `inv scan` needs names to check, not prose. Without R-7's `tables` entry, a raw `INSERT INTO remediations` elsewhere would skip the edge unseen (4.3).

### 4.3 Architecture

- **SDK** (`invariants-py/src/invariants/`). The loader and a static type checker; context-local registries; the guard, model-call, edge and transaction wrappers; a four-outcome evaluator; providers and `old_self` snapshots; operations (review stores, a pager hook, a rule-error spike detector that is off by default, `inv.monitor` over rows, storage-error mapping); and an OpenTelemetry exporter. It never rewrites a syntax tree and never runs `eval` or `exec` on rule or source text.
- **CEL behind an adapter, three backends.** `subset` is the default: a standard-library tree walker with a cost budget, and a linear-time RE2 matcher for `matches()`. `cel-expr` runs cel-expr-python [49], Google's C++ runtime. `celpy` runs cel-python [129], a pure-Python interpreter. One shared front end parses every rule, so a rule parses the same way everywhere. `INV_CEL_BACKEND` picks the backend. There is no shared Rust core and no Go reference evaluator: the three backends check each other through one conformance suite (4.5). **The lesson:** the engines disagreed more than expected. Division by zero gave three answers, celpy took Unicode digits, and cel-expr cut strings at a NUL. Much of certification went into making them agree (8.4).
- **`inv` CLI**: a Python console script with five commands. Standing is computed once, in `inv status`.

  | Command | What it does |
  |---|---|
  | `inv validate` | the JSON Schema check and every load-time check, listing every error |
  | `inv lock [--check] [--accept-weakening] [--update-artifacts]` | writes or checks `invariants.lock`, and prints a semantic diff against the stored lock |
  | `inv verify --import MOD` | imports the app; every target is wrapped by its guard, and every guard names a rule whose target it is |
  | `inv scan [--root] [--exclude] [--format text\|json\|sarif]` | the static mediation check (below) |
  | `inv status` | each rule's standing, `held` or `enforce(known-callers)`, with the reason |

  The exit codes are 0 for ok, 1 for a failed check and 2 for a usage error. A 3 means the scan found unresolved calls, and a 4 means it skipped an ablated enforce rule; neither is a pass. `validate`, `lock`, `verify` and `status` import the app from this checkout, ahead of site-packages. Before that fix, `inv lock --check` imported whatever copy the venv had installed, and reported "fresh" after an edit to this one.
- **Scanner.** It reads source with Python's `ast` and never imports it. That is enough for one language and needs no dependency, so the design's generated ast-grep or Semgrep rules, and Semgrep's licence question [119], fell away. The output is text, JSON or SARIF 2.1.0. On an approximate call graph, it looks for any path from an entrypoint to `mediation.sink` that avoids every `mediation.door`. It flags bypass idioms: `.__wrapped__`, `getattr` on the sink, `sink.__call__`. A call it cannot resolve is `unresolved`, never a bypass. It reads raw SQL writes to `mediation.tables` per table and statement kind. So `tables: [{leases: [insert, update]}]` lets a release's `DELETE` through and still flags an `INSERT INTO leases` in a new endpoint. A computed table name is `unresolved`. Door code is decided by call-graph reachability, not by where the text sits. A rule whose sink SQL writes a table that `tables` does not cover, or one that guards stored data and declares no `tables`, gets a warning and is not reported held. **The lesson:** in the evaluation, an endpoint that ran `INSERT INTO leases` passed the scan, because tables were all-or-nothing and declaring one would have flagged a legitimate `DELETE`. Statement kinds fixed that.
- **The lockfile (version 3).** It pins only what a rule's meaning depends on. For a target, that is its signature, resolved types (aliases expanded, and the fields of every model a signature names), effects, transaction and reply schema. It also pins each provider's declaration and signature, each contract's resolved schema or named OpenAPI operations, and each rule's semantic content, with prose as digests. A content-addressed `schemas` table stores each schema once. **The lesson:** the first lock hashed whole modules. In the evaluation, `inv lock --check` failed on 18 of 24 changes, 16 times only because a module had changed. That teaches people to re-pin by reflex, and the reflex re-pinned a real rule weakening. Contract-granular pins cut the failures to 2 of 24 (8.3).
- **The weakening gate.** `inv lock --check` lists each change as `<where>: <what>: old -> new`. It marks a change that relaxes a rule `WEAKENED`. Examples are a lower status or mode, a removed enforcer, clause or table, `fail_open_and_page`, a staler consistency, more retries, or a `bad` that matches fewer cases. Plain `inv lock` refuses a weakening, and only `--accept-weakening` records one. Every schema field has a class in `lockfile.FIELD_POLICY`, and a field with no class is a possible weakening. A polarity analysis over a canonical parse tree decides the direction of a `bad` edit. Whitespace, commuted operands, regrouping, De Morgan and split clauses count as the same condition. Anything it cannot decide is a possible weakening. **Two lessons.** First, a rebound `bind`, `view`, provider `args` or `model_policy` changes what `bad` reads, yet it was re-pinned with exit 0. Any such edit is now a possible weakening. Second, the canonical tree compared literals with Python's `==`, where `true == 1 == 1.0`. So `approved == true` to `approved == 1` passed as an "equivalent rewrite", and the new rule never fires. Each literal now carries its CEL type, and integer-only rewrites apply only to provable integers.
- **Ablation, not mutation.** The design's `inv status --mutate` became `pytest --inv-ablate`. It turns each rule off everywhere, subprocesses included, and re-runs its `falsified_by` tests. A falsifier that still passes, or an `enforce` rule with no caught falsifier, fails the session. **The lesson:** the first version counted any failed re-run as caught, so a flaky or stateful test that never touched the guard made the rule look load-bearing. Now each falsifier must pass on a control re-run without ablation, and the ablated run must reach the rule. Otherwise, or when the control run gave only `rule_error`, the row is `inconclusive`.
- **Telemetry.** `invariants.integrations.otel.exporter()` writes one span per evaluation, `invariant.<point>`, with `invariant.rule_id`, `rule_rev`, `outcome`, `mode` and `attempts`. `gen_ai.*` is not mapped while those conventions are at Development status [111].

**Not in v0.1.**
- **Commands and files:** `inv gen`, `discover`, `monitor`, `serve-mcp` and `conformance`; the candidates file.
- **SDKs:** the Go and TypeScript SDKs.
- **Generators:** Hypothesis tests per rule [41], CEL to `CHECK`, and the differential harness. The apps hand-write their property and differential tests.
- **Contracts:** protobuf contracts.
- **Status checks:** the detect-to-enforce soak evidence (reach N times, zero rule errors). The loader checks `falsified_by`, an enforcing enforcer and the timing check, not traffic.
- **Runtime:** a sampled mode, and pytest-xdist.

### 4.4 Python API

```python
import invariants as inv
from triage.models import CauseReply, JobFacts   # CauseReply: pydantic, extra="forbid"

@inv.provider("job")                   # loader checks name, signature and return type against providers:
def load_job(job_id: str) -> JobFacts: ...

class CheckedCause(inv.Checked): ...   # Checked[CauseReply]; only inv.model_call builds one

@inv.model_call("R-7", schema=CauseReply)
def propose_cause(log_excerpt: str, job_id: str, inv_feedback: str | None = None) -> CheckedCause:
    return call_model(log_excerpt, inv_feedback)   # raw reply; inv_feedback carries the parser error

@inv.edge("R-7")                       # checks .verdict, re-validates the schema, re-evaluates bad (2.6)
def remediate(cause: CheckedCause, job_id: str): ...

class PoolService:
    @inv.transaction                   # the boundary named in targets: (2.2)
    def txn(self): ...                 # BEGIN; yield; COMMIT, or ROLLBACK on an exception
    @inv.guard("R-21")                 # pre-effect; approval from its same_txn provider, inside self.txn
    def resize_door(self, req: ResizePoolRequest) -> None: ...

inv.load("invariants.yaml")            # every load-time check; raises inv.LoadError
inv.verify()                           # every target guarded, every guard names a rule; raises inv.CoverageError
```

The decorators also work on `async def`. `invariants.integrations.fastapi` guards routes and answers with `application/problem+json`: 422 for a rejection, 202 for an escalation, 409 for a storage conflict and 503 for a fail-closed error. The body names the rule, never the values.

**The four outcomes.** Every evaluation is `ok`, `violation`, `rule_error` or `parse_error`, and a clause list follows CEL's commutative error rules (4.1). `on_violation` raises `inv.Rejected`, `inv.Halted`, `inv.Escalation` or `inv.Routed`. Each carries `.rule`, `.outcome`, `.attempts`, `.point` and `.bound`. `Routed` and `Escalation` also carry the `.review_id` of a review opened in the configured store (`inv.MemoryReviews`, `inv.JsonlReviews`, or a fallback). When no review can be opened, the call raises `inv.ReviewNotOpened`. The first build raised `Routed` with no id, and the work then survived only in a log line. `inv.RolledBack` reports a swallowed failure inside `@inv.transaction`, and a failure in a nested block dooms the outermost one. No other exception leaves a guard: a raising provider, a hostile reply or a cost limit becomes one of the four outcomes, so retry, escalation and the event always run. Errors, events and logs carry no request or reply values unless `debug` is on.

**Status and mode.** The loader computes the highest status the evidence allows (`Rule.max_status`) and refuses a higher one. `enforce` needs an enforcer with mode `enforce`, `falsified_by`, and a passing timing check. The effective mode is `inv.effective_mode` = `min(mode, cap(status))`, as in 2.4, and detect never blocks. `inv status` reports `held` only under 2.4's conditions plus a scan with no table warning; otherwise `enforce(known-callers)`, with the reason.

**Edges fail closed.** In an early build, an edge with `on_error: fail_open_and_page` paged and let through a `Checked` whose verdict was `rule_error`, even when `bad` held on its value. Now an enforcing edge refuses any received verdict but `ok`, whatever its `on_error`. `on_error` covers only the edge's own re-evaluation. The edge also re-validates the value against the model call's closed schema before it re-checks `bad`. Forged extra fields, out-of-enum values, cyclic values and NaNs are refused. The consumer then gets a new `Checked` over the copy the rule judged, so a value mutated after the model call never reaches it. A forged `Checked` that fits the schema and passes `bad` still gets through: that is what an edge can check.

### 4.5 Conformance suite

v0.1 has conformance for CEL only. The cases are language-neutral YAML, `{expr, env, expected: ok | violation | rule_error | parse_error}` or an exact `value`, in 17 suites. These cover logic, macros, extensions, numbers and non-finite values, time, map keys, regex, cost and Python values. pytest runs every case on every installed backend, against an `expected_failures.yaml` that is empty. The rest of the design's list (binding, providers, snapshots, timing, retries, outcomes and status caps) is tested by the SDK's own pytest suite, not by portable cases. There is no stdin/stdout executor yet, so the protovalidate pattern [52] waits for a second SDK, and spec 1.0 still needs two SDKs at zero expected failures.

## 5. Distribution

| Channel | v0.1 |
|---|---|
| Package | `pip install invariants-first`, for Python 3.11 or later. The core needs only PyYAML, and is Apache-2.0. Extras: `[cel]` (cel-expr-python, cel-python and protobuf), `[pydantic]`, `[fastapi]`, `[schema]` (jsonschema, for `inv validate`), `[otel]` (the OpenTelemetry API) and `[dev]`. The name is a placeholder: RELEASING.md lists what to settle before the first upload, including PyPI Trusted Publishing with attestations [120] |
| pytest plugin | Registered through the `pytest11` entry point, so it comes with the package. The options are `--inv-rules`, `--inv-verify`, `--inv-ablate` (with `--inv-ablate-lenient`), `--inv-report` and `--inv-report-json`, and the fixtures are `inv_registry`, `inv_ablated` and `inv_events`. It does not support pytest-xdist |
| CLI | The `inv` console script (4.3). No signed binary |
| CI | No packaged GitHub Action yet; run the commands below. `inv scan --format sarif --output inv.sarif` produces a file for code-scanning upload. The library's own CI runs Python 3.11 to 3.14 against each of the three backends, plus mypy, a job without the `cel` extra, and a wheel install into a fresh venv |

pool-service's `ci.sh` runs this order under `set -e`; fact-pipeline's is the same without `inv status`:

```
inv validate invariants.yaml
inv lock --check                       # stale or weakened: stop here, in about 0.3 s
inv verify --import your_app.api
inv scan --root src                    # add --format sarif --output inv.sarif for code scanning
inv status --root src --import your_app.api
pytest --inv-verify --inv-ablate --inv-report --inv-report-json build/inv-report.json
```

Put `inv lock --check` early, since it is the cheapest step. Put the rule file, lock, falsifiers and `ci.sh` under human-only review (2.6): the weakening gate is only as good as whoever reads an `--accept-weakening` diff.

Planned, not built:

| Channel | Plan |
|---|---|
| MCP server | `inv serve-mcp` (MCP now sits in the Agentic AI Foundation [116]): rules as resources; `check`, `explain`, `status` and `propose_candidate` (candidates file only) as tools. No tool edits `invariants.yaml` |
| Coding-agent hooks | A Claude Code plugin. `PreToolUse` on Edit/Write denies changes to protected paths; `PostToolUse` runs `inv scan`; `Stop` runs the `falsified_by` tests of rules whose targets an edit reaches [96]. Set `"onFailure": "block"`: "a stalled or timed-out PreToolUse command hook doesn't block by default" [96]. The hook configuration is itself a protected path. Kiro has comparable hooks [73] |
| Sidecar | `inv serve --http` for languages without an SDK, the OPA pattern [118]. Deferred |
| Rule registry | Versioned rule packs (`llm/closed-set-reply`, `authz/no-effect-before-auth`), each tested, pulled in with `extends:`. Apache-2.0 or CC0, never relicensed |
| Governance | RFCs; maintainers from two organisations before 1.0; a neutral foundation, as OPA's CNCF home showed when its creators joined Apple in 2025 [118] |

**Roadmap.** v0.1 delivered the spec, the lockfile, the Python SDK, the pytest plugin and the core CLI. It did not deliver the GitHub Action or a conformance executor. "One real service with measured numbers" became two synthetic apps and a controlled experiment (8.3). Guard latency, ablation and benign-change false positives were measured; the false-positive rate on real traffic and the accepted-after-retry rate wait for a real deployment. v0.2–0.3: the MCP server, the Claude Code plugin, a TypeScript SDK and a SQL generator. v0.4+: Discover, a Go SDK and the registry.

## 6. Across the development lifecycle

### 6.1 Stage by stage

| Stage | What happens | What blocks |
|---|---|---|
| Requirements | Candidates from incidents, threat models, STPA | Nothing yet |
| Design review | Rules beside the API spec; `why` links a design record | Lint: a rule no enforcer can see |
| Coding | Hooks run the scan and the property tests of rules whose targets the edit reaches (reverse call edges, 5); CI still runs all | Hook denies the tool call |
| Code review | Rule diffs, violations, the **coverage delta**, what no rule covers | CODEOWNERS [100]; push rulesets [101] |
| CI | `inv verify` and `inv scan` (held needs both), property tests, ablation, storage interleaving tests | Required check |
| Release | Promotion detect → enforce, optionally via a warning step like Kubernetes' Audit/Warn/Deny [53]; per-rule canary [108] | Promotion and **demotion** both reviewed |
| Deploy | Storage rules `NOT VALID`, monitor, then `VALIDATE` [102]; config rules via Conftest [122] | Validation fails |
| Observability | Each break an event [110] | A detect-mode hit on live data pages |
| Incident | Postmortem adds `prohibited_state`, `existing_rule?`, `path_around_enforcer` [109] | Open until each action is a rule or a recorded decision |
| Maintenance | Rule debt: zero-reach or ownerless rules, bind drift | Build fails on drift |
| Security | Tool and taint rules; output validation per OWASP LLM01 [112] | Gateway refuses |
| Compliance | Rule hashes, provenance [114], logs | Evidence only; EU AI Act Art. 12 requires logs [113] |

STPA fits partly [30]: unsafe actions and ordering map to prohibitions; omission and duration do not unless bounded. DORA 2024 linked AI adoption to a 7.2% drop in delivery stability, a correlational estimate [115]. Invariant-first is an unmeasured hypothesis aimed at that gap.

### 6.2 A day in the life

**Day 1.** A new `ResizePool` RPC: a model proposes a pool size, a person approves, a writer applies it. `invariants/pool.yaml` gains:
- R-20: leases exceed pool size. Storage trigger plus a door guard: one `bad`, two views (2.7).
- R-21: a write with no APPROVED approval for `(pool_id, new_size)`. Pre-effect guard; a policy rule.
- R-22: the model's `new_size` outside `[current, quota]`. Closed schema `ResizeProposal{new_size, reason_code}`.
- R-23: no call to `pool_store.set_size` outside `resize_door`, and no raw SQL on `pools.size`.

**Coding.** A test fails, and the agent tries to loosen R-22's bound in `invariants/pool.yaml`. The `PreToolUse` hook denies it: "protected path; propose a rule change in the PR description". Later the `PostToolUse` scan flags R-23: a retry helper calls `pool_store.set_size` directly, bypassing the door. The agent routes it through the door.

**PR and deploy.** The bot comment shows no rule diffs, one uncovered effect (signed off as accepted risk), and every rule killed by ablation. The reviewer reads for intent, which no rule covers. R-20 ships `NOT VALID`; two old oversubscribed pools go to the owner as bugs.

**Day 2, release.** A canary runs R-21 and R-22 in detect mode. R-22 fires on 3% of replies. Of the replies accepted on a second attempt, the owner samples 20 and finds 4 valid but wrong, sized to exactly the quota: R-22 lacked `compares_to: held_data` and had been set to feed the bound back. The owner adds the mark, which forbids the feedback and the resample (2.5), and a reviewed change promotes R-21 and R-22 to enforce.

**Day 9, incident.** An operator script resizes a pool through an old endpoint that skips the door, bypassing R-21's in-process check. The postmortem's `path_around_enforcer` becomes a rule change: R-21 rev 2 adds a storage trigger joining `approvals` (the record in 2.1). The old endpoint goes under `residual_paths`. Until the trigger lands, R-21 is `enforce(known-callers)`.

## 7. Information to knowledge

The original promised this use case and did not develop it.

### 7.1 What is different

1. **Knowledge rules are mostly state rules over a shared store**, so storage is the main enforcer.
2. **The polarity already exists.** A dbt data test is "a select statement that seeks to grab 'failing' records" [81]; a SHACL-SPARQL constraint is a SELECT whose solutions are violations [82]. Both are `bad`.
3. **An OWL reasoner is not an enforcer**: under the open-world assumption, violations become inferences [87]. Use SHACL, ShEx or database constraints.
4. **Wikidata chose detect.** Its constraints are "hints, not firm restrictions" [86]. Model exceptions as waiver or conflict records with an owner and expiry, referenced by `bad`.
5. **Valid is weaker here.** Structure guarantees a fact is attributable, not supported, and support is not truth [90]. "Believed" means "supported by trusted evidence under check X".
6. **A broken candidate usually means dirty data** [94]. Route it to review with the violating rows.

### 7.2 Data model

```
Source   {id, hash, uri, publisher_root, trust, acl, restricted, generated_by_pipeline, deleted_at?}
Evidence {source_id, source_hash, start, end, quote, subj_mention, obj_mention,
          polarity ∈ {ASSERTED, DENIED, HYPOTHETICAL, REPORTED}}
Fact     {id (code-computed), subject_id, predicate, object, valid_from?, valid_to?, recorded_at,
          status ∈ {PROPOSED, CHECKED, BELIEVED, RETRACTED}, evidence[], check_run?, retry_count}
Entity   {id, canonical_id, type, aliases[]}     Xref {source_system, source_key, entity_id}
Conflict {fact_ids[], owner, opened_at, resolved_at?}
```

Provenance maps onto PROV-O [88]. History is an append-only event log [89], with valid time and recorded time kept separately.

### 7.3 Catalog of prohibitions

| ID | Never exists | Enforcers |
|---|---|---|
| K-1 | A fact with no evidence span | extractor guard; store; monitor |
| K-2 | A quote not at its source offsets, or a stale source hash | extractor guard; ingest; monitor |
| K-3 | A span missing the subject or object mention | extractor guard |
| K-4 | A predicate outside the vocabulary | parse; SHACL [82] |
| K-5 | An edge violating domain or range | ingest; SHACL |
| K-6 | A literal of the wrong type | parse; SHACL |
| K-7 | An interval ending before it starts | guard; `CHECK` |
| K-8 | An updated or deleted fact row | append-only trigger |
| K-9 | One external record, two canonical ids | `UNIQUE` |
| K-10 | An active fact on a merged entity | merge transaction; monitor |
| K-11 | An unowned contradiction on a functional property | serialized store; **route** |
| K-12 | A BELIEVED fact without independent trusted sources | promotion door |
| K-13 | A fact whose only evidence is pipeline-generated | ingest; store |
| K-14 | Restricted PII outside the restricted tier | ingest; index; monitor |
| K-15 | Live facts or chunks from a deleted source | event handler; FK; reconciler (detect only) |
| K-16 | A BELIEVED fact citing a stale source version | monitor (detect only) |
| K-17 | A fact past PROPOSED with no code check | scan; trigger |
| K-18 | A model-supplied id | parse |
| K-19 | A RAG answer citing an unretrieved chunk | answer guard |
| K-20 | A served chunk outside the caller's ACL | retrieval filter |
| K-21 | A fact outside its entities' lifetimes [86] | monitor (detect only) |

"Detect only" rules are monitored, not held (1.1). K-11 shows the route pattern: `bad` is "a contradiction with no Conflict record", which can be made never to exist.

Store-side enforcers are hand-written (SHACL, triggers, a single writer; on Neo4j some constraints are Enterprise-only [95]), each with a differential test against CEL `bad`.

### 7.4 Where each rule runs

```
intake (code sets hash, trust, ACL, restricted, generated_by_pipeline)
 -> EXTRACT (model; PURE: returns proposals, writes nothing)
 -> PARSE closed schema (K-4, K-18) -> extractor guard (K-1, K-2, K-3, K-7)
 -> ENTITY RESOLUTION (code; "possible match" goes to review)
 -> INGEST door (K-5, K-9, K-10, K-13, K-14)
 -> STORE txn: constraints, SHACL on commit, per-key serialization (K-8, K-9, K-11, K-17)
 -> PROMOTE, the only door to BELIEVED (K-12 + entailment gate)
 -> INDEX (K-14, K-15, K-20) -> ANSWER guard (K-19)
 -> MONITOR nightly over everything; deletion reconciler
```

The extractor is pure, so its after-guard prevents (2.2).

### 7.5 What rules cannot do, and how to make meaning checkable

All of these pass K-1 to K-7: a denial stored as an assertion, a quote from the wrong speaker, "up to $2bn" stored as 2e9, a 2019 "X is CEO" left open-ended. Move such judgments into closed fields code can compare:

- **Polarity** as an enum; only ASSERTED becomes a fact, REPORTED becomes a claim with a checked speaker mention.
- **Qualifier** for numbers: EXACT, APPROX, UPPER_BOUND, LOWER_BOUND.
- **`as_of`** from the source's date, held by code.
- **Entailment as a weak judge**: the premise is the cited span; the hypothesis is rendered **by code from the predicate's template**, never by the model [91][93]. It gates only promotion to BELIEVED. Bedrock's grounding [84] and Automated Reasoning [85] checks fit the same slot.
- **Anthropic Citations** guarantees "valid pointers to the provided documents", not support [83].

Measure the rest with evals: factual precision [91], citation recall and precision [92]. Retry pressure (2.5) is worst here: "quote not in source" pushes the model to copy *some* real span. Re-extract fresh and audit retry-accepted facts more.

### 7.6 Worked flow

Source S1, a trusted newswire: "Acme Corp said Tuesday it completed its acquisition of Beta Ltd for about $2.1 billion. Beta had denied talks in 2024."

1. **Intake.** Code records hash, trust and ACL.
2. **Extract.** P1 `acquired(Acme, Beta)` ASSERTED; P2 `price` quoting "for $2.1 billion"; P3 `in_talks` DENIED.
3. **Guard.** P2's quote is not at its offsets, so K-2 fails. A fresh extraction returns "about $2.1 billion", APPROX, `retry_count=1`. P3 becomes a claim.
4. **Resolve and store.** An ambiguous "Beta Ltd" goes to review, never the model. Later S2 gives $2.0bn; K-11 fires and code opens a Conflict with an owner.
5. **Promote.** K-12 needs two independent publishers; when a second confirms, P1 becomes BELIEVED. P2 stays CHECKED and goes to the audit sample.
6. **Retraction.** S1 is withdrawn. Facts resting only on S1 are retracted (K-15); P1 drops back to CHECKED.
7. **Monitor.** The nightly run catches writes that bypassed the door.

## 8. Limits, numbers and verdict

### 8.1 Limits and open problems

- **The coverage gap moves** (section 0). Measure coverage on effects (2.8) and keep risk-weighted human sampling [31]. The experiment in 8.3 shows the same thing: no invariants mechanism caught any of the 8 uncovered defects.
- **Discover is unproven**: AgentSpec's 70.96% recall is one data point [36]. v0.1 has no Discover.
- **Scans are syntax-only.** `inv scan`'s call graph is approximate: it does not follow calls through variables or see callers outside its roots, and it matches raw SQL per table, not per column. Rely on positive mediation at a boundary (2.4).
- **The code graph is static.** It is only as complete as its extractor plus recorded traces. A missing edge hides a bypass, and untested dynamic paths stay dark (2.4, 2.8).
- **Meaning** needs closed fields, people and evals (7.5).
- **Retry pressure**: bounded, reported retries (2.5).
- **Hyperproperties** cannot be held by a guard [13][16]; use structure and labels (2.6).
- **Guards are a new failure mode**: a false positive in enforce is an outage [31]. Per-rule `on_error`, logged break-glass, an automatic drop to detect. In v0.1 the spike drop exists (`inv.ErrorSpike`) but is off by default.
- **A rule can give false comfort.** In two of the experiment's defects a rule covered the property but could not see the break: the enforcement point was in the wrong place, or the provider itself was the bug (8.3).
- **Rule conflicts** can deadlock (1.7).
- **Rules are a target** for coding agents: protected paths, held-out falsifiers (2.6), and a lock that refuses a weakening without a named flag (4.3).
- **Storage conformance** covers only the histories it generates (2.7).

### 8.2 Run-time cost

Guard overhead per call, p50, from the library README. The setup is one `pre_effect` decorator on a pure function with no providers, the bare call subtracted, measured on an Apple-silicon laptop with Python 3.14. Certification re-measured it within a few percent.

| Backend | 1 clause | 4 clauses |
|---|---|---|
| `subset` (default) | 7.1 µs | 22.7 µs |
| `cel-expr` | 9.6 µs | 22.0 µs |
| `celpy` | 86 µs | 830 µs |

Providers add their own cost, because each one an enforcer reads is called on every call. In the apps, R-1's four clauses add about 42 µs to a 1 µs function, and the fact ingest door costs about 240 µs guarded against 12 µs bare. At HTTP scale, about 2.5 ms per request, this is noise; on hot loops it is not. Use `cel-expr` or `subset` on hot paths and keep `celpy` for conformance. The CI steps add about 1.5 s to pool-service (5.0 s against 3.5 s) and 0.6 s to fact-pipeline (1.9 s against 1.3 s).

### 8.3 The experiment

`invariants-py/docs/evaluation.md` records it. 24 patches were applied to copies of the two apps and each app's `ci.sh` run: 8 defects that break a written rule (C), 8 that no rule covers (U), and 8 benign changes (B). The ground truth was written first. When CI stopped at `inv lock --check`, the runner re-pinned, as an agent would. Two baselines answer "would ordinary tests have caught it?". B1 runs the same tests with every rule ablated. B2 is a per-case judgement: the same properties as plain `if` checks, with no scan, lock, ablation or load checks. The table shows the results after the redesign in 4.3, which left every catch unchanged.

| Category | invariants CI | by an invariants mechanism | B1 | B2 |
|---|---|---|---|---|
| C: covered defects (8) | 8/8 | 6/8 (guard 2, scan 2, lock 1, load 1) | 2/8 | 4/7 |
| U: uncovered defects (8) | 3/8 | 0/8 | 2/8 | 3/8 |
| All defects (16) | 11/16 | 6/16 | 4/16 | 7/15 |
| B: benign, red until a human acts (8) | 6/8 before the redesign, 1/8 after | | 0 | 0 |

- **Where it beat plain code.** `inv scan` caught two bypasses: a new endpoint calling the lease store directly, and a bulk import skipping the ingest door. No test exercised either new path. The lock caught a rule weakened in both the YAML and its falsifier (R-22's cap from 4x to 10x), but only for a human who reads the diff. A load check caught a signature change that broke a rule's binding, a defect the library's own wiring makes possible.
- **Where it did not.** The runtime guards fired on 2 changes, and plain `if`s with the same tests would have caught both. They earn their keep in production, on inputs no test tries, and this experiment cannot measure that. `inv verify` and `--inv-ablate` never fired on a change: they protect the rule set's integrity, not individual changes. 5 of the 16 defects escaped everything.
- **False positives.** No benign change failed a test or the scan; the noise was the lock.
- **Lock churn, then the redesign.** Before the redesign, `inv lock --check` failed on 18 of the 24 changes, 16 times only because a whole-module hash moved. 6 of the 8 benign changes stayed red until a re-pin, and a reflex re-pin turned the R-22 weakening green. After contract-granular pins and the weakening gate (4.3), it failed on 2 of 24: the R-22 weakening and a legitimate vocabulary widening. Each failure named its change, and plain `inv lock` refused both. A raw-SQL bypass probe outside the 24 is now caught too. One cost got worse: the locks grew from about 165 lines to 645 and 681 lines after the version-3 compaction.

**The human-cost ledger.**

| Item | pool-service | fact-pipeline |
|---|---|---|
| Rule YAML, non-blank and non-comment lines | 235 (now 239) | 245 (now 271) |
| Rule YAML as a share of app code | 40% | 25% |
| Rules | 9, all `enforce` | 12: 11 `enforce`, 1 `detect` |
| Explicit decisions in the rule file (status, outcomes, consistencies, effects, mediation, owners, falsifiers) | about 69 | about 89 |
| Plumbing in `src` | 14 decorators, 4 providers, a transaction boundary, an OpenAPI export script | 16 decorators, 97 lines of providers and context, a test hook in production DDL |
| FRICTION.md entries (still open) | 15 (about 4) | 17 (about 7) |

The redesign added about 25 decisions (table specs and sinks), plus one `--accept-weakening` per deliberate relaxation.

### 8.4 Certification history

A separate reviewer certified the library over 12 review rounds. Each round re-ran every earlier probe on all three backends. There were 15 fix rounds. Fix round 1 answered the two outside apps and the first review, fix round 12 answered the evaluation, and the rest answered review findings. Every fix round added regression tests that failed on the code before it. The defects found fell into a few families:

- **Fail-open paths.** A `py:` guard key that matched no rule returned unchecked. A transactional effect committed before the boundary check. An edge passed a `rule_error` verdict (4.4). A hostile reply's exceptions skipped retry and escalation. A `matches()` pattern took 4.7 s on 28 characters.
- **Evidence that overstated.** Ablation counted any failed re-run as caught, until the control re-run (4.3).
- **NaN.** Round 5's one blocker: a NaN in a reply passed a range check with verdict `ok`. Rounds 6 and 9 found two narrower ways in (4.1).
- **Loads, then fails every call.** Unknown functions, wrong arities, datetimes, UUIDs, typed replies and mixed arithmetic (4.1).
- **Silent weakening.** Both majors of rounds 10 and 11 were in the new lockfile. A rebound `bind` or `view` was re-pinned with exit 0, and the untyped literal comparison treated `true` and `1` as equal (4.3).
- **Engines that disagree**, and request values leaking into error text and logs.

**How the majors narrowed.** Before round 5, reviews had found 13 majors. Round 5 found 1 blocker and 1 major. Rounds 6 to 8 found one major each, each narrower than the last. Round 9 found none, and gave the first "production-ready" verdict. Rounds 10 and 11 found one major each, both in code the evaluation had prompted. Round 11's verdict was "production-ready once R11-1 is fixed", and fix round 14 fixed it. Round 12, the final confirmation, found no blocker or major; fix round 15 closed its four minors. The lesson: the runtime core stopped producing majors after round 9, but each new mechanism brought its own, and the weakening gate needed two rounds to stop leaking. Today about 3,762 tests pass on each of the three backends, with 5 skipped. The suite also passes under `python -O`, mypy is clean, and both apps' CI is green.

### 8.5 Verdict: does it save human work or add it?

**The concept worked, for one kind of defect.** It caught every defect whose shape a rule anticipated, and nothing else. Its unique catches are breaks of a written rule along a path no test exercises: bypasses, weakened rules and broken bindings. On uncovered defects it added nothing, as section 0 says it would.

**Per change, the first version added more work than it saved.** Three changes in four needed a re-pin, and the reflex re-pin erased the lock's one unique catch. After the redesign, the recurring cost is mostly gone: 1 benign change in 8 needs a deliberate decision. What remains is the one-time cost, rule files about 25–40% the size of the app plus about 160 decisions, and a larger lock to read.

It reduces human work when:
- a few high-stakes properties (money, quotas, approvals, provenance) are things agent-written code might route around;
- new code paths often arrive without tests, so mediation scans catch what tests cannot;
- reviewers really read rule and lock diffs, and treat `--accept-weakening` as a review event;
- the rule set stays small and stable, so its cost is spread over many changes.

It adds human work when:
- most changes are ordinary features and the rules guard nothing an agent would bypass;
- the team or the agent passes `--accept-weakening` by reflex, which erases the gate as surely as a reflex re-pin did;
- the defects that matter are business logic no rule states;
- a provider or an enforcement point is itself buggy, so the rule gives false comfort and the tests still do the work.

**Bias caveats.** One author wrote all 24 changes and the ground truth, after reading the rules and the scan semantics. In effect, the same author also wrote the rules. The two caught bypasses have exactly the shape the scan models, and a slightly different bypass (raw SQL) was missed until the fix. 24 changes in 2 apps is small: one case moves a rate by 12 points. Both apps were built to demonstrate the library, and their tests assert rule ids, so B1 had to drop about half the tests, and B2 is a judgement, not a measurement. It does not measure production value or the time spent reading lock diffs, and the library was changing during the run. Read the numbers as a direction, not a rate.

## 9. Starting tomorrow

1. Pick the effect whose failure costs most: usually a write or tool call an agent can trigger.
2. Write what must never happen there as one sentence, then as CEL bound to the effect's typed contract.
3. Choose the highest rung you can reach (2.2).
4. Write the mediation sentence: which chokepoint every path crosses, and what goes around it.
5. Run it in detect mode; send violations to a person as possible bugs.
6. Add a property test that fails, by ablation, without the guard.
7. If the rule touches shared data, add the storage enforcer and its interleaving tests.
8. Protect the rule file, tests, hooks and CI config from agents.
9. Promote to enforce through review. Count attempts on model-call accepts and sample the rescued ones.
10. List your effect sinks. Each one without a rule is a candidate or a signed accepted risk.

## Appendix A. The v0.1 library and two apps

Paths are relative to the directory that holds this guide.

| Path | Holds |
|---|---|
| `invariants-py/src/invariants/` | `loader.py` and `typecheck.py`, `guard.py` (guard, model call, edge, transaction), `registry.py`, `lockfile.py`, `scan.py`, `bootstrap.py` and `doors.py` (`inv init`), `cli.py`, `pytest_plugin.py`, `cel/` (three backends and the value mapping), `integrations/` and the JSON Schema |
| `invariants-py/tests/` | About 3,920 tests: unit and integration tests, `conformance/cel/` (17 YAML suites), and one `test_fix_round*.py` per fix round, each encoding what that round's probes showed broken |
| `invariants-py/docs/` | `rule-file.md` (every field), `evaluation.md` (8.3) and a copy of this guide |
| `invariants-py/` | README, CHANGELOG (one section per fix round), RELEASING (the publishing checklist), SECURITY, CONTRIBUTING, and `.github/workflows/ci.yml` |
| `apps/pool-service/` | A FastAPI and SQLite service that leases worker-pool slots, resizes pools through approvals, splits job batches and triages failed jobs with a model. 9 rules (R-1, R-7, R-20 to R-24, H-1, H-2), all `enforce`, and 72 tests |
| `apps/fact-pipeline/` | A batch CLI that extracts facts from documents with a deterministic fake model (with fault injection), resolves entities, stores and promotes facts, and answers with citations. It has 12 of section 7.3's knowledge rules (K-1 to K-19; 11 `enforce`, K-16 `detect`) and 86 tests |
| `prototype/` | The standard-library prototype the first draft described: a JSON rule file, a CEL subset, and `demo.py` with 65 checks. It is kept as a teaching artifact |

Each app has an `invariants.yaml`, an `invariants.lock`, a `ci.sh` and a `FRICTION.md`. FRICTION.md records what was hard to express or run, with what each library fix round changed for that app. The FRICTION files were the input to fix round 1. Each app also has a `bypass/` tree that the clean scan leaves out on purpose, so its tests can show `inv scan` catching a bypass. The apps import only the library's public API.

**Test counts.** About 3,915 tests pass, with 3 to 5 skipped (engine-matrix cases that run only when CI sets their flags), on each of the three backends (`subset`, `cel-expr`, `celpy`), on Python 3.11 and 3.14, and also under `python -O`. mypy is clean. GitHub CI runs the same matrix on 3.11 to 3.14, plus the wheel build and both apps.

**How to run them.** From the directory that holds this guide:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e 'invariants-py[dev,cel]'
cd invariants-py
for b in subset cel-expr celpy; do INV_CEL_BACKEND=$b python -m pytest tests -q; done
python -m mypy
cd ../apps/pool-service && pip install -e '.[dev]' && ./ci.sh
cd ../fact-pipeline   && pip install -e '.[dev]' && ./ci.sh
cd ../../prototype    && python3 demo.py          # the teaching prototype; no dependencies
```

Run pool-service with `POOL_SERVICE_DB=pool.db uvicorn pool_service.main:app` (uvicorn is not a dependency). Try fact-pipeline with `fact-pipeline --db f.db --inject drop_qualifier ingest examples/s1.txt --publisher newswire-a`, then `promote`, `ask` and `monitor`.

**The adoption path, measured.** `inv init` was run on four codebases. On a copy of pool-service it pinned 7 pydantic models and wrote one door rule for the store (7 service doors). It froze 4 of the 6 bypasses planted in the app's `rogue.py` and 3 real back doors: a benchmark and two endpoints writing the store directly. The other 2 planted bypasses are raw SQL, below the API; `--sql` finds them. On an app whose repositories use SQLAlchemy, a Django-style manager and Stripe, with no SQL, it found the three repositories, their service doors and the 3 back doors in a worker and an admin script. On a raw-SQL ledger app it found the door and the batch script that skips it. On the open-source full-stack FastAPI template (SQLModel) it pinned 9 models with 26 constraints and wrote no door rule, correctly: its routes write through the session or call `crud` directly, so there is no service layer to lock yet, and the file lists those writes instead (it was read only; its code was not installed or run). The three files that were locked passed `inv check` as written. In three rounds of 52 scripted agent-style changes over the ledger and ORM apps, all 35 that loosen a law or add a back door were blocked for the stated reason, and all 17 safe ones passed (new code through a door, reads, unit tests, tightenings, a reviewer's approval). The blocked ones were direct calls, aliases, constructors, `self.repo` (also tuple-assigned), annotated and unannotated parameters, `getattr`, monkeypatching, a new service method that skips the checked door, self-allowing, an added door, a deleted rule, a stale `allow` entry, and an agent re-pinning its own weakening (caught by `--base`). The third round ran on a wheel installed into a fresh venv.

**What the prototype taught, and what the library added.** Building the prototype fixed three rules in this guide. R-21's bare `has()` does not compile in CEL. R-1 needed its length clause, because `inv.zip` on unequal lists is a `rule_error`. And R-7 now reads held data through a provider and is re-checked at an edge. The fourteen questions it raised are answered in sections 2.1 to 2.9, and the library implements each answer. The library adds what the prototype left out: `inv scan`, the lockfile, YAML, the CLI, real CEL engines and a type checker.

## References

1. R. W. Floyd, "Assigning Meanings to Programs", Proc. Symp. Applied Mathematics 19, AMS, 1967. https://www.i2m.univ-amu.fr/perso/lionel.vaux/ens/imd-seminaire/Floyd-AssigningMeanings1967.pdf
2. C. A. R. Hoare, "An axiomatic basis for computer programming", CACM, 1969. https://doi.org/10.1145/363235.363259
3. C. A. R. Hoare, "Proof of correctness of data representations", Acta Informatica 1(4), 1972. https://doi.org/10.1007/BF00289507
4. E. W. Dijkstra, "Guarded commands, nondeterminacy and formal derivation of programs", CACM, 1975 (EWD472). https://www.cs.utexas.edu/~EWD/transcriptions/EWD04xx/EWD472.html
5. L. Lamport, "Proving the Correctness of Multiprocess Programs", IEEE TSE, 1977; author commentary. https://lamport.azurewebsites.net/pubs/pubs.html
6. B. Alpern and F. B. Schneider, "Defining liveness", Information Processing Letters 21(4), 1985. https://doi.org/10.1016/0020-0190(85)90056-0
7. J. H. Saltzer and M. D. Schroeder, "The Protection of Information in Computer Systems", Proc. IEEE, 1975. https://doi.org/10.1109/proc.1975.9939
8. J. P. Anderson, "Computer Security Technology Planning Study", ESD-TR-73-51, 1972. Note: exact wording unverified; checked through paraphrase only. https://csrc.nist.gov/publications/history/ande72.pdf
9. B. Meyer, "Applying 'Design by Contract'", IEEE Computer, 1992. https://doi.org/10.1109/2.161279
11. B. Meyer, "Class Invariants: Concepts, Problems, Solutions", arXiv:1608.07637. https://arxiv.org/abs/1608.07637
12. B. Liskov and J. Guttag, *Program Development in Java*, Addison-Wesley, 2000; course notes on abstraction functions and rep invariants. https://courses.cs.washington.edu/courses/cse331/18su/concepts/abstraction-functions-and-rep-invariants.html
13. F. B. Schneider, "Enforceable Security Policies", ACM TISSEC 3(1), 2000. https://www.cs.cornell.edu/fbs/publications/EnfSecPols.pdf
14. J. Ligatti, L. Bauer, D. Walker, "Edit Automata: Enforcement Mechanisms for Run-time Security Policies", Princeton TR-681-03, 2003. https://www.cs.princeton.edu/techreports/2003/681.pdf
16. M. R. Clarkson and F. B. Schneider, "Hyperproperties", Journal of Computer Security 18(6), 2010. https://doi.org/10.3233/JCS-2009-0393
17. K. W. Hamlen, G. Morrisett, F. B. Schneider, "Computability Classes for Enforcement Mechanisms", ACM TOPLAS 28(1), 2006. https://personal.utdallas.edu/~hamlen/cc4em.pdf
18. F. B. Schneider, G. Morrisett, R. Harper, "A Language-Based Approach to Security", LNCS, 2001. https://www.cs.cornell.edu/fbs/publications/LangBasedCase.pdf
19. D. L. Parnas, "On the Criteria To Be Used in Decomposing Systems into Modules", CACM 15(12), 1972. Note: checked via secondary sources. https://doi.org/10.1145/361598.361623
20. Y. Minsky, "Effective ML Revisited", Jane Street Tech Blog, 2011. https://blog.janestreet.com/effective-ml-revisited/
21. A. King, "Parse, don't validate", 2019. https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/
22. V. Vernon, *Implementing Domain-Driven Design*, ch. 10, excerpt on InformIT. https://www.informit.com/articles/article.aspx?p=2020371
23. D. Jackson, *The Essence of Software*, Princeton University Press, 2021. https://press.princeton.edu/ideas/daniel-jackson-on-the-essence-of-software
25. H. Wayne, "TLA+ Action Properties", 2021. https://www.hillelwayne.com/post/action-properties/
26. M. Kleppmann, "Prediction: AI will make formal verification go mainstream", 8 Dec 2025. https://martin.kleppmann.com/2025/12/08/ai-formal-verification.html
28. E. W. Dijkstra, *Notes on Structured Programming*, 1970 (quotation via Wikiquote; EWD number disputed). https://en.wikiquote.org/wiki/Edsger_W._Dijkstra
29. N. G. Leveson, *Engineering a Safer World*, MIT Press, 2011. https://www.flighttestsafety.org/images/Engineering_a_Safer_World.pdf
30. N. G. Leveson and J. P. Thomas, *STPA Handbook*, 2018. Note: wording checked only against secondary sources. https://psas.scripts.mit.edu/home/get_file.php?name=STPA_handbook.pdf
31. R. I. Cook, "How Complex Systems Fail". https://how.complexsystems.fail/
32. S. Willison, "The Dual LLM pattern for building AI assistants that can resist prompt injection", 2023. https://simonwillison.net/2023/Apr/25/dual-llm-pattern/
33. E. Debenedetti et al., "Defeating Prompt Injections by Design" (CaMeL), arXiv:2503.18813, 2025. https://arxiv.org/abs/2503.18813
34. M. Costa, B. Köpf et al., "Securing AI Agents with Information-Flow Control" (FIDES), arXiv:2505.23643, 2025. https://arxiv.org/abs/2505.23643
36. H. Wang, C. M. Poskitt, J. Sun, "AgentSpec: Customizable Runtime Enforcement for Safe and Reliable LLM Agents", ICSE 2026. https://arxiv.org/abs/2503.18666
37. K. Kingsbury and P. Alvaro, "Elle: Inferring Isolation Anomalies from Experimental Observations", PVLDB 14(3), 2020. https://arxiv.org/abs/2003.10554
38. FoundationDB, "Simulation and Testing". https://apple.github.io/foundationdb/testing.html
39. TigerBeetle, "Safety". https://docs.tigerbeetle.com/concepts/safety/
40. Antithesis, "Assertions". https://antithesis.com/docs/properties_assertions/assertions/
41. Hypothesis, "Stateful testing". https://hypothesis.readthedocs.io/en/latest/stateful.html
42. K. Claessen and J. Hughes, "QuickCheck", ICFP 2000. https://doi.org/10.1145/351240.351266
43. R. A. DeMillo, R. J. Lipton, F. G. Sayward, "Hints on test data selection", IEEE Computer, 1978. https://doi.org/10.1109/c-m.1978.218136
44. M. Strathern, "'Improving ratings': audit in the British University system", European Review 5(3), 1997. https://econpapers.repec.org/RePEc:cup:eurrev:v:5:y:1997:i:03:p:305-321_00
45. Z. Zhong, A. Raghunathan, N. Carlini, "ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases", arXiv:2510.20270, 2025. https://arxiv.org/abs/2510.20270
46. J. Huang et al., "Large Language Models Cannot Self-Correct Reasoning Yet", ICLR 2024. https://arxiv.org/abs/2310.01798
47. M. D. Ernst et al., "Dynamically discovering likely program invariants to support program evolution", ICSE 1999. https://doi.org/10.1145/302405.302467
48. Common Expression Language: overview and language definition. https://cel.dev/overview/cel-overview ; https://github.com/google/cel-spec
49. Google Open Source Blog, "Announcing CEL-expr-python", 3 Mar 2026. https://opensource.googleblog.com/2026/03/announcing-cel-expr-python-the-common-expression-language-in-python-now-open-source.html
50. Buf, protovalidate. https://github.com/bufbuild/protovalidate
51. Buf, "Protovalidate is now v1.0", 2025. https://buf.build/blog/protovalidate-v1
52. Buf, protovalidate conformance. https://github.com/bufbuild/protovalidate/blob/main/docs/conformance.md
53. Kubernetes, "Validating Admission Policy". https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/
54. Google Open Source Blog, "Kubernetes CRD Validation Using CEL", 2023; KEP-2876. https://opensource.googleblog.com/2023/11/kubernetes-crd-validation-using-cel.html
55. Google Cloud, "Overview of IAM Conditions". https://docs.cloud.google.com/iam/docs/conditions-overview
56. OpenAI, "Introducing Structured Outputs in the API", 2024. Note: page details checked only via search snippets. https://openai.com/index/introducing-structured-outputs-in-the-api/
57. Anthropic, "Structured outputs". https://platform.claude.com/docs/en/build-with-claude/structured-outputs
58. Google, "Structured output" (Gemini API). https://aistudio.google.com/docs/structured-output
59. Instructor, "Reask/Validation". https://python.useinstructor.com/concepts/reask_validation/
60. Pydantic, "PydanticAI Output". https://pydantic.dev/docs/ai/core-concepts/output/
61. Guardrails AI, "Validator on-fail actions". https://guardrailsai.com/docs/concepts/validator_on_fail_actions
62. AWS, "Policy in Amazon Bedrock AgentCore is now generally available", 3 Mar 2026. https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available
63. Cedar, "Authorization". https://docs.cedarpolicy.com/auth/authorization.html
64. C. Disselkoen et al., "How We Built Cedar: A Verification-Guided Approach", arXiv:2407.01688, 2024. https://arxiv.org/abs/2407.01688
65. Docker, "MCP policy reference". https://docs.docker.com/ai/governance/reference/mcp-policy/
66. agentgateway, "MCP authorization". https://agentgateway.dev/docs/standalone/latest/configuration/security/mcp-authz/
67. Invariant Labs, invariant (Guardrails); Snyk acquisition announcement, 24 Jun 2025. https://github.com/invariantlabs-ai/invariant ; https://snyk.io/news/snyk-acquires-invariant-labs-to-accelerate-agentic-ai-security-innovation
68. Semgrep, "Taint analysis overview". https://docs.semgrep.dev/writing-rules/data-flow/taint-mode/overview
69. ArchUnit User Guide. https://www.archunit.org/userguide/html/000_Index.html
70. import-linter. https://import-linter.readthedocs.io/en/stable/
71. Bazel, "Visibility". https://bazel.build/concepts/visibility
72. M. Maaz, L. DeVoe, Z. Hatfield-Dodds, N. Carlini, "Agentic Property-Based Testing", arXiv:2510.09907, 2025. https://arxiv.org/abs/2510.09907
73. Kiro, "Correctness" (property-based testing) and "Hooks". https://kiro.dev/docs/specs/correctness/ ; https://kiro.dev/docs/hooks/
74. D. Jin, P. Meredith, C. Lee, G. Roşu, "JavaMOP: Efficient Parametric Runtime Monitoring Framework", ICSE 2012. https://fsl.cs.illinois.edu/publications/jin-meredith-lee-rosu-2012-icse.html
75. N. Alshahwan et al., "Automated Unit Test Improvement using Large Language Models at Meta", FSE 2024. https://arxiv.org/abs/2402.09171
76. C. Foster, M. Harman et al., "Mutation-Guided LLM-based Test Generation at Meta", FSE Companion 2025. https://arxiv.org/abs/2501.12862
80. S. Bursuc et al., "A benchmark for vericoding", arXiv:2509.22908, 2025. https://arxiv.org/abs/2509.22908
81. dbt Labs, "Add data tests to your DAG". https://docs.getdbt.com/docs/build/data-tests
82. W3C, "Shapes Constraint Language (SHACL)", 2017. https://www.w3.org/TR/shacl/
83. Anthropic, "Citations". https://platform.claude.com/docs/en/build-with-claude/citations
84. AWS, "Use contextual grounding check to filter hallucinations in responses". https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html
85. AWS, "Validate your Automated Reasoning policy test results". https://docs.aws.amazon.com/bedrock/latest/userguide/validate-automated-reasoning-policy-results.html
86. Wikidata, "Help:Property constraints portal". https://www.wikidata.org/wiki/Help:Property_constraints_portal
87. J. Tao, E. Sirin, J. Bao, D. McGuinness, "Integrity Constraints in OWL", AAAI 2010. https://doi.org/10.1609/aaai.v24i1.7525
88. W3C, "PROV-O: The PROV Ontology", 2013. https://www.w3.org/TR/prov-o/
89. M. Fowler, "Event Sourcing", 2005. https://martinfowler.com/eaaDev/EventSourcing.html
90. J. Thorne et al., "FEVER", NAACL 2018. https://aclanthology.org/N18-1074/
91. S. Min et al., "FActScore", EMNLP 2023. https://arxiv.org/abs/2305.14251
92. T. Gao et al., "Enabling Large Language Models to Generate Text with Citations" (ALCE), EMNLP 2023. https://arxiv.org/abs/2305.14627
93. B. Bohnet et al., "Attributed Question Answering", 2022. https://arxiv.org/abs/2212.08037
94. H. Paulheim, "Knowledge graph refinement: A survey of approaches and evaluation methods", Semantic Web 8(3), 2017. https://doi.org/10.3233/SW-160218
95. Neo4j, Cypher Manual, "Constraints". https://neo4j.com/docs/cypher-manual/current/constraints/
96. Anthropic, Claude Code "Hooks reference". https://code.claude.com/docs/en/hooks
97. Anthropic, Claude Code "How Claude remembers your project". https://code.claude.com/docs/en/memory
98. B. Böckeler, "Understanding Spec-Driven-Development: Kiro, spec-kit, and Tessl", 2025. https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html
99. GitHub Spec Kit, "Spec-Driven Development". https://github.com/github/spec-kit/blob/main/spec-driven.md
100. GitHub Docs, "About code owners". https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners
101. GitHub Docs, "Available rules for rulesets". https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
102. PostgreSQL, "ALTER TABLE". https://www.postgresql.org/docs/current/sql-altertable.html
103. PostgreSQL, "Explicit Locking". https://www.postgresql.org/docs/current/explicit-locking.html
104. PostgreSQL, "Transaction Isolation". https://www.postgresql.org/docs/current/transaction-iso.html
106. SQLite, "Isolation In SQLite". https://sqlite.org/isolation.html
107. Schemathesis. https://schemathesis.readthedocs.io/en/stable/
108. Google, "Canarying Releases", *The Site Reliability Workbook*. https://sre.google/workbook/canarying-releases/
109. Google, "Postmortem Culture: Learning from Failure", *Site Reliability Engineering*. https://sre.google/sre-book/postmortem-culture/
110. OpenTelemetry, "Logs Data Model". https://opentelemetry.io/docs/specs/otel/logs/data-model/
111. OpenTelemetry, "Semantic conventions for generative client AI spans". https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
112. OWASP, "LLM01:2025 Prompt Injection". https://genai.owasp.org/llmrisk/llm01-prompt-injection/
113. EU AI Act, Article 12 "Record-Keeping". https://artificialintelligenceact.eu/article/12/
114. SLSA v1.0, "Provenance". https://slsa.dev/spec/v1.0/provenance
115. Google Cloud, "Announcing the 2024 DORA report". https://cloud.google.com/blog/products/devops-sre/announcing-the-2024-dora-report
116. Anthropic, "Donating the Model Context Protocol and establishing the Agentic AI Foundation", 9 Dec 2025. https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation
118. Open Policy Agent, "Integrating OPA"; Gatekeeper constraint templates; "A note from Teemu, Tim, and Torin", 2025. https://www.openpolicyagent.org/docs/integration ; https://open-policy-agent.github.io/gatekeeper/website/docs/constrainttemplates ; https://openpolicyagent.org/blog/note-from-teemu-tim-and-torin-to-the-open-policy-agent-community-2dbbfe494371
119. Semgrep, "Compare Semgrep to Opengrep". https://semgrep.dev/docs/faq/comparisons/opengrep
120. PyPI, "PyPI now supports digital attestations", 2024. https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/
121. OpenAPI Initiative, "OpenAPI Specification v3.1.1"; "OpenAPI 3.1.0 RC0". https://spec.openapis.org/oas/v3.1.1 ; https://www.openapis.org/blog/2020/06/18/openapi-3-1-0-rc0-its-here
122. Conftest. https://www.conftest.dev/
123. M. Kleppmann, *Designing Data-Intensive Applications*, O'Reilly, 2017, ch. 12, "Timeliness and Integrity". Note: exact wording unverified; checked only through reader excerpts. https://www.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/
124. Graphify, "graphify" (README): code parsed with tree-sitter; docs, SQL schemas and configs mapped into one graph; doc edges from a model pass, tagged INFERRED. https://github.com/Graphify-Labs/graphify
125. CodeGraph, "codegraph" (README): tree-sitter index in SQLite with callers, callees and impact queries. https://github.com/colbymchenry/codegraph
126. Sourcegraph, "SCIP Code Intelligence Protocol" and `scip.proto`. https://github.com/sourcegraph/scip
127. GitHub, "About CodeQL"; "Analyzing data flow in Python". https://codeql.github.com/docs/codeql-overview/about-codeql/ ; https://codeql.github.com/docs/codeql-language-guides/analyzing-data-flow-in-python/
128. Meta, Glean: "System for collecting, deriving and querying facts about source code". https://glean.software/
129. cel-python (cloud-custodian), "Pure Python implementation of the Common Expression Language"; PyPI package `cel-python`, imported as `celpy`. https://github.com/cloud-custodian/cel-python
