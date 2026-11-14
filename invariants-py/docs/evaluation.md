# Evaluation: did invariants catch agent-style defects, and what did it cost?

Date: 2026-11-10. Library: invariants 0.1.0. Apps: [apps/pool-service](../../apps/pool-service)
(FastAPI) and [apps/fact-pipeline](../../apps/fact-pipeline) (batch CLI). This is a small
experiment on two synthetic apps, and the same author wrote both the changes and (in effect) the
rules. Read the [limitations](#limitations) before quoting any of the numbers below.

The experiment was run twice: once on the original library, and again after the lockfile was
redesigned in response to what the first run found. This page leads with the current results. The
[original run](#before-the-original-run) is kept in full below, because it is the evidence the
redesign answers.

## Short answer

The concept works for one kind of defect, and its recurring cost is now small.

- **Catches.** invariants CI caught all 8 defects that break a written rule, and 3 of the 8 that no
  rule covers (those 3 by ordinary tests). 6 of the 16 defects were caught by an invariants
  mechanism (runtime guard 2, `inv scan` 2, lockfile 1, load check 1). Over plain `if` checks plus
  the same tests, the gain is the 2 door bypasses `inv scan` found, the rule-weakening edit the
  lockfile refused, and a broken rule binding the load check reported. It caught nothing a rule did
  not cover.
- **Cost.** `inv lock --check` failed on 2 of the 24 changes (P04, a real weakening, and F12, a
  legitimate rule edit), and 1 of the 8 benign changes turned CI red (F12) until someone made a
  deliberate decision. Both failures name exactly what changed. A plain `inv lock`, the reflex
  re-pin, refuses both: a weakening needs `--accept-weakening`.
- **One-time cost.** About 240 to 270 rule lines per app (239 and 271 non-blank, non-comment
  lines) and about 180 explicit human decisions across the two apps.

So invariants is worth it for a few high-stakes properties (money, quotas, approvals, provenance)
that new code might route around, where reviewers read rule and lock diffs. It is not a substitute
for tests: the business-logic defects no rule states were caught by ordinary tests or by nothing.

## Current results (after the lock redesign)

### What changed in the library

The first run found three problems: the lock pinned whole modules, so almost every edit made it
stale; a reflex re-pin silently accepted a weakened rule; and raw SQL around a door was invisible.
The library changed as follows (the git history has the details):
- **Contract-level pins.** The lock pins each contract instead of whole modules: target signatures
  and effects, provider signatures, resolved reply and contract schemas (in one shared table, lock
  version 3), named OpenAPI operations, resolved types, and each rule's semantic content.
- **A weakening gate.** `inv lock --check` names each change as old -> new and marks rule
  weakenings WEAKENED. `bad` is compared as a canonical tree, so whitespace, commuted operands and
  split clauses are not changes. Any edit that changes what `bad` reads (a `bind`, `view`,
  provider, `model_policy` or artifact edit) counts as a possible weakening. `inv lock` refuses to
  write a weakening without `--accept-weakening`.
- **Raw SQL.** `mediation.tables` takes statement kinds. A rule that protects stored data without
  `mediation.tables` gets a scan warning, and `inv status` does not report it held. The sink's own
  SQL counts toward the held standing.

The two apps changed only their rule files and locks:
- **pool-service.** R-20 declares `tables: [{leases: [insert, update]}]`, R-21 and R-22 declare
  `tables: [{pools: [update]}]`, and R-7 declares `tables: [{remediations: [insert, update]}]`.
- **fact-pipeline.** K-4, K-7, K-11 and K-18 gain the ingest door's sink, door and
  `tables: [{facts: [insert, update]}, {evidence: [insert]}]`. K-16 gains K-12's `fact_status`
  mediation.
- **Locks.** Both locks were migrated. The migration ran on the old rule files first, so the rule
  edits show up as a reviewed semantic diff.

### Method of the re-run

Fresh copies of the updated apps were made, each with a pristine copy beside it. The 24 patches,
the runner and the B1 deselect lists were used unchanged (see [Method](#method)). The runner's
"re-pin" step still runs a plain `inv lock`, the reflex a code-writing agent would use. The re-run
was repeated after the last library change, with identical results.

One runner artefact needed handling: the P04 patch now applies its YAML hunk at an offset, so
`patch` leaves an `invariants.yaml.orig` backup, which tripped the runner's reverse check for P05 to
P12. The file was deleted and P05 to P12 were re-run. They reversed cleanly with identical results,
and every copy matched its pristine copy afterwards.

### Lock failures and false positives

| | Original run | Current |
|---|---|---|
| `inv lock --check` failed (of 24) | 18 | **2** (P04, F12) |
| ... of which only a whole-module hash changed | 16 | **0** |
| Benign changes red on the first CI run (of 8) | 6 | **1** (F12) |
| Benign changes with a test or scan failure | 0 | 0 |
| Plain `inv lock` (reflex re-pin) accepted a weakening | yes (P04 went green) | **no**: refused for P04 and F12 |

The original write-up estimated that pinning at signature granularity would leave about 3 lock
failures (P04, F04, F12). The re-run has 2, because F04 still stops one step earlier, at
`inv validate`.

Both remaining failures say exactly what changed:
- **P04:** `rule R-22: bad: '... > 4 * old_self.sizes[...]' -> '... > 10 * old_self.sizes[...]'
  WEAKENED (the condition now matches fewer cases)`, refused on a plain re-pin. The message also
  notes that the statement text was edited.
- **F12:** `rule K-4: bad: '!(... in [...])' -> '!(... in [..., "founded_in"])' WEAKENED`. The
  `FactReply` change is reported once, with its three users:
  `schema fact_pipeline.models.FactReply: pydantic.properties.predicate.enum added: (none) ->
  ["founded_in"] (used by target ...accept_fact types.return, target ...propose_fact reply_schema,
  contract fact_reply schema)`. That is the schema change the original lock missed.

With `--accept-weakening` (checked by hand on separate copies), `ci.sh` goes green for both. P09
to P11 and F09 to F11, the benign refactors and features, stay green: the lock pins resolved types
and the fields of every model a signature names, but not function bodies.

### Catch rates

| Category | Original: invariants CI | Current: invariants CI | Current: by an invariants mechanism | B1 (mechanical) | B2 (plain code + tests) |
|---|---|---|---|---|---|
| C: covered defects (8) | 8/8 (7/8 if re-pinned unread) | **8/8** (8/8 with a plain re-pin; 7/8 only with `--accept-weakening` unread) | 6/8 (guard 2, scan 2, lock 1, load 1) | 2/8 | 4/7 |
| U: uncovered defects (8) | 3/8 | **3/8** | 0/8 | 2/8 | 3/8 |
| All defects (16) | 11/16 | **11/16** | **6/16** | 4/16 | 7/15 |
| B: false positives (8) | 6/8 red until re-pin | **1/8** red until a deliberate `--accept-weakening` | — | 0 | 0 |

- **The same defects are caught or missed.** The same tests failed on P01, P03, F01, F03, F05 and
  F08, and the same scan findings appeared on P02 and F02. What changed is that the catches are no
  longer hidden behind a lock failure on the first run, and that a reflex re-pin no longer erases
  P04.
- **B1 on P04.** B1 for P04 now shows one failing test (`test_lock_is_fresh`). This is an artefact
  of the refused re-pin, which leaves the lock stale for the baseline run. It is not a plain-test
  catch, and the B1 column does not count it.
- **Gaps closed outside the 24.** The raw-SQL probe (an endpoint that runs `INSERT INTO leases`)
  is now `forbidden R-20 pool_service/api.py:254 raw SQL INSERT on the protected table 'leases'
  outside the sink and door`, and the scan fails. pool-service's `force_remediate` raw
  `INSERT INTO remediations` is a `forbidden/raw-sql` finding, so R-7 is not reported held while it
  exists. A rebound `bind` or `view`, a provider `args` edit, `compares_to` removed, more resamples
  or retries, and an `--update-artifacts` digest are all refused without `--accept-weakening`.

### What got worse

- **The lockfile is larger.** It grew from 162 to 645 lines (27,253 bytes) in pool-service and from
  166 to 681 lines (30,886 bytes) in fact-pipeline, mostly resolved schemas and OpenAPI operations.
  Diffs are rarer and name their changes, but a reviewer who reads the raw lock diff has more to
  read.
- **A legitimate rule edit needs an explicit decision.** F12 widens K-4's vocabulary, which really
  is a relaxation, so it needs `--accept-weakening`. A `bad` edit whose direction the analysis
  cannot decide, and any `bind`, `view` or `model_policy` edit, is also treated as a possible
  weakening, so some legitimate edits need the flag too. None of the 24 changes hit that case.
- **The rule files grew.** pool-service grew from 235 to 239 lines and fact-pipeline from 245 to
  271: about 25 more human decisions (table specs, and sinks and doors on 5 rules in
  fact-pipeline).
- **One bypass produces several findings.** F02 produces 5 bypass findings (K-1, K-4, K-7, K-11 and
  K-18) for one bypass, where it used to produce 1. This is more accurate, because those rules
  really are skipped, but it makes the duplicate-findings friction (pool-service FRICTION #12)
  worse.
- **Some rules still cannot be made held.** K-15 still warns. Its effect (`UPDATE sources`) is also
  written by `add_source`, so no table spec fits without an app code change. It stays
  `enforce(known-callers)`.
- **Existing users have one-time work.** Older locks must be migrated once, and other apps see new
  scan warnings until they declare `mediation.tables`.

### Verdict

The main recurring human cost the original run measured is largely gone:
- Lock failures fell from 18 to 2 of 24.
- 1 of the 8 benign changes needs a decision, where 6 needed a re-pin.
- Re-pinning by reflex no longer erases the lockfile's one unique catch. A weakening needs a named
  flag, and the CI log says what is being weakened.

The protective value did not change: 6 of 16 defects are caught by an invariants mechanism, and
none of the 8 uncovered defects are. One residual gap is closed: raw SQL around a door, which the
scan now sees once the rule declares its tables.

So the ledger shifts toward "worth it" under the conditions the original verdict lists: a few
high-stakes properties that new code might route around, new code paths that arrive without tests,
reviewers who read rule and lock diffs, and a small, stable rule set. It still adds work when the
defects that matter are business logic no rule states, or when the providers or enforcement points
are themselves buggy (F01, F03), because there the rule gives a false sense of coverage and the
tests still do the work. The new costs are a lockfile about four times larger and a deliberate flag
for every relaxing rule edit. The limitations below still apply: one author, 24 changes and
synthetic apps.

## Addendum 2, 2026-11-12: nothing below the API, and a tool-calling agent

This release removed the features that reached below the API (storage enforcers, transactional
targets, provider consistency, `tables:`, `.sql` contracts and `inv init --sql`), fixed how `allow`
lets paths through, typed attribute chains in the scan, and added Graphify input and module
constants to `inv init`. The table in the previous addendum stands, except that the 2 raw-SQL
bypasses in pool-service's `rogue.py`, and the ledger app's second back door, are no longer reported:
they are below the API.

**A defect found by review, fixed.** The scan treated an `allow` entry like a door, so a new caller
of a frozen back door reached the sink unseen, and code inside an allowed function (unwrapping the
door, monkeypatching it) was not reported. A path is now let through only when every function on it
is allowed. Three scenarios were added for it below; all three failed `inv check` for the stated
reason.

**A false positive found by the new app, fixed.** On the first `inv init` of the support agent,
every tool call `ctx.billing.refund(...)` was name-matched to `StripeGateway.refund`, so the tools
were frozen as back doors and the service doors had 0 callers. Receiver types now follow attribute
chains on a typed receiver, resolved where the class is written (the first attempt resolved them in
the caller's module and still missed). After the fix, `inv init` on the agent's code, with no
hints, wrote:

| What | Found |
|---|---|
| contracts | the 4 pydantic models (3 tool inputs and the planner's `Plan`), 23 constraints |
| door rules | `LedgerRepo`'s 3 writes through the 3 `BillingService` methods (3 callers: the tools); `StripeGateway.refund` through `BillingService.refund` |
| back doors frozen | 1 real one, the legacy `admin.goodwill_refund`, with its caller `admin_goodwill` |
| `inv check` as drafted | pass |

The agent's 12 seeded defects were each caught for the stated reason, and its 2 safe changes (a new
tool through the door, a tighter cap re-pinned by `--relock`) passed:

| # | Defect | Caught by |
|---|---|---|
| 1 | a new tool calls the gateway directly (2 ways) | scan: bypass |
| 2 | the gateway method put in the tool registry | scan: the sink used as a value (unresolved, `inv check` fails) |
| 3 | the refund cap raised 10x | lock: WEAKENED, refused by `--relock` |
| 4 | `semantic_resamples` 1 to 5 | lock: WEAKENED |
| 5 | a new tool through the frozen back door; then allowing it | scan: bypass; lock: WEAKENED |
| 6 | the model over-refunds, uses another customer's order, over-credits | PLAN-1: resampled once, then a person; BILL-1 and BILL-2 at the door for any caller |
| 7 | invalid JSON, an unknown tool name, an extra field | the closed schema: retried with the parser error, at most 3 attempts |
| 8 | the model abstains | a review is opened, nothing runs (in detect: recorded, verdict `abstain`) |
| 9 | a forged plan, a plan checked for another customer | the executor edge |
| 10 | the plan changed after the check, the ledger moved since | the executor edge re-checks on what it receives |
| 11 | the guard deleted from a door | verify |
| 12 | a rule deleted | lock: WEAKENED |

Under `pytest --inv-ablate`, BILL-1, BILL-2 and PLAN-1 are each load-bearing: their falsifiers fail
when the rule is switched off.

**Three rounds.**

| Round | What ran | Result |
|---|---|---|
| 1 | the test suite on Python 3.11 and 3.14 with each CEL backend of the time (subset, cel-expr, celpy; celpy was removed later), `-O`, mypy | 3,930 to 3,932 passed per run (3 to 5 skipped); mypy clean |
| 2 | 12 mutants of the code this release changed (the `allow` fix, chain typing, chain freezing, Graphify, constants, the hook, the removal errors); the 39 agent-style scenarios on the ledger and ORM apps; the three apps' CI | the 12 mutants caught (one survived at first, Graphify's community check, and a test was added); 39 of 39 scenarios as expected (20 ledger, 19 ORM); the apps' CI passed |
| 3 | a freshly built wheel in a new venv: removed names absent, all three apps' CI, both scenario sets | 70, 86 and 32 app tests passed; 39 of 39 scenarios as expected |

Of the 39 scenarios, the ones that loosen a law or open a back door were blocked, and these passed:
raw SQL to the table in a new module and through a module constant (below the API), a unit test that
calls the sink, an unrelated edit, a fixed back door with its `allow` entry removed, and `--base` on
a clean tree.

**Follow-up from a review checklist.** Four more items were built after the rounds above and
covered by tests and mutants: receivers typed from a factory's return annotation (`get_repo() ->
Repo`), store clients recognised by annotation (`s: Session`, a `SessionDep` alias), a one-to-one
layer between HTTP routes and a write accepted as its door, and module constants that are `None` or
tuples, lists or sets. Graphify call links now count only when `EXTRACTED`. After them the suite ran
3,944 tests, all 16 mutants of the changed code were caught, the three apps' CI passed, and the 39
scenarios were unchanged. `inv init` on the full-stack FastAPI template (read and parsed only, never
installed or run) still writes no door rule, as it should: its routes call `crud` and write through
the session themselves, so no layer sits between them to lock.

**Graphify, run for real.** Graphify 0.9.107 (PyPI `graphifyy`, the Graphify-Labs package) was
installed in its own venv and run as `graphify update .` (tree-sitter only, no model, no API key) on
the three apps and the FastAPI template. Each graph took under a second. The format matched what
`inv init` reads: paths relative to the project, `L<line>` locations, integer communities, and
`EXTRACTED` / `INFERRED` tags. Two findings changed the code. First, its `uses` relation is a
parameter type, not a call, so it no longer counts as one; and its EXTRACTED `calls` are plain
function calls, a subset of what the scan already extracts (every method call on an object is
INFERRED). Second, on fact-pipeline the community rule promoted `cli.main`, which writes the store
directly, from a back door to a door, so the rule came out clean while blessing a back door. The rule
is now strict (a candidate's callers must all sit in a third community, and an entry point never
qualifies), with regression tests for both shapes. After the fix:

| Codebase | Functions | Graph nodes matched | Doors without the graph | With the graph |
|---|---|---|---|---|
| support-agent | 47 | 62 of 132 | 2 rules | the same |
| pool-service | 64 | 97 of 194 | 1 rule | the same |
| fact-pipeline | 94 | 152 of 245 | 1 rule (commented out: back doors remain) | the same rule with one more door, `Pipeline.resolve_review` (still commented out) |
| FastAPI template | 51 | 55 of 194 | none | none |
| three-layer shop, unconventional names (`api.py` → `checkout.py` → `ledger.py`; test fixture) | 10 | 10 of 19 | none | none |

The last row is the case the graph was meant for: no name says service or repository, each middle
function has one caller, so the heuristics find no door. The graph finds none either. Its
communities follow call chains, not layers: `handle_buy` → `place_order` → `post_charge` is one
community and the refund chain another, so no function sits between two communities. The fixture and
its real graph are pinned in `tests/fixtures/graphify/shop/` with a test that asserts this. So on the
five codebases measured, the graph added one plausible door (fact-pipeline) and no rule the
heuristics missed: on code like this, communities are not a layer signal.

**Cost.** Typing attribute chains made `inv scan` slower: 1.17 s against 0.94 s for the library's
own source and tests (104 modules, 1,852 functions).

**What this does not show.** The same author wrote the library, the agent, its defects and the
scenarios. The 12 defects ran on the agent's deterministic fake planner (`APP_MODEL=fake`); the
replay flows (`tests/test_replay.py`) run on hand-written traces, not recordings, so no reply from a
real model is part of these numbers. A check on an API's arguments is not a lock: two agents
refunding one order at once can both pass BILL-1, and the ledger's transaction is what decides.
Graphify's real output was measured only as reported above (four codebases and the pinned shop
fixture, tree-sitter extraction with no model); the library's other Graphify tests read graph files
written for the tests in the same format.

## Addendum, 2026-11-12: the adoption path

This addendum measures only `inv init` and door rules (the CI-only first step), not the 24-change
experiment above.

| Codebase | What `inv init` wrote | Back doors found and frozen | `inv check` as drafted |
|---|---|---|---|
| pool-service (copy) | 7 pydantic models, 13 constraints; 1 door rule for `store` with 7 service doors | 7: 4 of the 6 planted in `rogue.py` (the other 2 are raw SQL, found with `--sql`), a benchmark, 2 endpoints writing the store | pass |
| ORM app (SQLAlchemy repo, Django-style manager, Stripe gateway, no SQL) | 2 models, 7 constraints; 3 door rules | 3: a worker skipping the quota check, 2 admin-script calls | pass |
| ledger app (raw SQL store) | 1 door rule | 1 (2 with `--sql`) | pass |
| full-stack FastAPI template (SQLModel; read only, nothing executed) | 9 models, 26 constraints; no door rule: no service layer | none to freeze; its direct writes are listed | not run (its code was not installed) |

Three rounds of scripted agent-style changes ran against the ledger app (in API-only and `--sql`
modes) and the ORM app: 52 changes, of which the 35 that loosen a law or add a back door were
blocked for the stated reason and the 17 safe ones passed. Rounds 1 and 2 ran in the development
venv. Round 3 ran on a freshly built wheel in a fresh venv, after the full test matrix (Python 3.11
and 3.14, the three CEL backends of the time (subset, cel-expr, celpy; celpy was removed later, so
two remain), `-O`, mypy, the CI of the two apps that existed then) passed.

**What this does not show.** The apps and the changes were written by the same author as the
library, the ORM app's repositories were written to look typical rather than taken from a real
project, and no real team has run it. The door chosen for each repository is a heuristic; on the
template it correctly found none, which also means a codebase without a service layer gets no path
laws from `inv init`.

## Method

This method applies to both runs.

1. Both apps were copied to a working directory, and a pristine copy was kept beside them. The
   copies ran against the shared venv, with `PYTHONPATH` pointing at the copy's `src`, so the
   original apps were never imported. The library was not modified.
2. **Ground truth was written first.** The file `GROUND_TRUTH.md` was written before any change was
   applied or any CI run was made. It holds the category of each of the 24 changes and a predicted
   result.
3. Each change is a unified diff (`patches/<ID>.patch`) made from exact text edits (`changes.py`).
   The runner (`run.py`) applies the patch with `patch -p2` and then runs the app's own `ci.sh`.
   That script runs `inv validate`, `inv lock --check`, `inv verify`, `inv scan` and pytest with
   `--inv-verify --inv-ablate`. The runner records the exit code, the failing step, the failing
   tests and the wall-clock time.
4. **Re-pin step.** If `ci.sh` stopped at `inv lock --check`, the runner did what an agent would do
   next: it ran `inv lock` and then ran `ci.sh` again. Both results are recorded. Each run then
   reverses the patch (`patch -R`), restores the lock, and checks with `diff -r` that the copy
   matches the pristine copy again. All 24 reversed cleanly.
5. **Two baselines** answer the question "would ordinary tests alone have caught it?"
   - **B1 (mechanical, a lower bound).** The same tests run with every rule ablated
     (`INV_ABLATE=<all ids>`, `INV_ABLATE_TESTING=1`). That turns off every in-process guard, plus
     fact-pipeline's storage DDL, which keys off the same flags. The pool-service SQL triggers stay
     on, because they are plain SQL. Tests that already fail on the pristine apps in this mode are
     dropped, because they assert library behaviour. That drops 31 of 72 pool tests and 43 of 86
     fact tests, which leaves 41 and 43. A change counts as caught when any remaining test fails.
     (On the current apps B1 drops 36 of 73 and 49 of 92, since tests were added after the
     published run, among them the replayed-trace tests in `tests/test_replay.py`; see
     [evaluation/README.md](../evaluation/README.md#what-has-changed-since-the-published-run).)
     This understates plain tests, because some of the dropped tests (for example
     `test_worked_flow_7_6`) check business outcomes as well as rule ids.
   - **B2 (per-case reasoning, a plain-code counterfactual).** Imagine the same properties written
     as ordinary `if` checks in the same functions, with the same tests asserting plain exceptions,
     and no scan, lockfile, ablation, verify or load-time checks. A defect counts as caught when a
     test that exercises it would fail in that world. B2 is the fairer comparison for the question
     "what did the machinery add?".
6. The human-cost numbers come from the real files: `invariants.yaml`, `invariants.lock`,
   `FRICTION.md`, `src/` and `tests/` in the pristine copies, and the README Performance table.

The control runs on the unchanged apps took these times (3 runs each):
- **Full `ci.sh`:** pool-service 4.96–5.07 s, fact-pipeline 1.84–1.92 s.
- **Plain `pytest`:** pool-service 3.53–3.56 s, fact-pipeline 1.25–1.29 s.

## Before: the original run

Everything in this section is the original run, on the library before the lock redesign. It is kept
unchanged as the "before" picture, and its numbers are superseded by
[Current results](#current-results-after-the-lock-redesign) wherever the two differ.

### Original short answer

The concept did work, but only for one kind of defect. It caught every defect whose shape the rules
had anticipated: two door bypasses, a rule-weakening edit, and a rename that broke a rule's
binding. Plain unit tests missed all four. It caught nothing a rule did not cover.

It also added real, recurring human work. 6 of the 8 benign changes turned CI red until someone
re-pinned the lockfile. Overall `inv lock --check` failed on 18 of the 24 changes, almost always
because a whole-module contract hash changed. That turns re-pinning into a reflex, and the reflex
undoes the one catch that only the lockfile made (the rule-weakening edit).

Against well-written ordinary checks and tests, the measured gain is 2 bypasses caught by
`inv scan`, plus 2 catches that depend on a human (the lock diff) or exist only because the
library exists (the binding check). The price is about 240 rule lines per app and about 160
explicit human decisions across the two apps. Whether that trade pays off depends on conditions, which are listed
under [Original verdict](#original-verdict).

### The 24 changes

The categories are: C, a defect that breaks a property a written rule covers; U, a defect no rule
covers; B, benign. "lock (noise)" means `inv lock --check` failed only because a whole-module
contract hash changed, which happens on any edit to that module. The first CI time is the run
before any re-pin. The second time is the run after `inv lock`.

| ID | Cat | Change | First CI run | After re-pin | What caught it (meaningful) | B1 | B2 |
|---|---|---|---|---|---|---|---|
| P01 | C | `_allocate` gives one remainder too many (`[:left+1]`), breaking R-1 | lock (noise), 0.37 s | pytest red, 4.17 s | Runtime guard R-1 (`Rejected` in 7 tests); the ordinary asserts too | yes | yes |
| P02 | C | **Bypass:** new bulk-lease endpoint calls `store.insert_lease` directly, skipping the `lease_slots` door and H-1 | lock (noise), 0.38 s | `inv scan` red, 0.62 s | `inv scan` (R-20 bypass). The H-1 limit (1..64) skip was **not** caught: H-1 has no mediation | no | no |
| P03 | C | `approve_resize` records the proposer as `approved_by`, so every approval is a self-approval (R-21) | lock (noise), 0.37 s | pytest red, 4.23 s | Runtime guard R-21: happy-path resize tests get 422 | no | yes |
| P04 | C | **Bypass by weakening the rule:** R-22 cap 4x→10x in the YAML, falsifier test edited to 10→150 | lock: `rules R-22: hash changed`, 0.37 s | green, 4.88 s | **Lockfile only.** After a re-pin nothing objects | no | no |
| P05 | U | `GET /pools/{id}` reports lease count instead of slot sum | pytest red, 4.81 s | — | Ordinary test (`leased == 48` gets 16) | yes | yes |
| P06 | U | `approve_resize` drops its PENDING check, so an APPLIED proposal can be re-approved and replayed | lock (noise), 0.38 s | green, 4.90 s | **Not caught** | no | no |
| P07 | U | split endpoint labels `lease-{lid+1}` (off-by-one) | green, 4.89 s | — | **Not caught** | no | no |
| P08 | U | `record_job` swaps `elapsed_s` and `timeout_s`, so R-7 later reasons over wrong facts | green, 4.83 s | — | **Not caught** | no | no |
| P09 | B | `store.leased` refactor | lock (noise), 0.40 s | green, 5.00 s | No false positive. **Re-pin needed** | — | — |
| P10 | B | new `GET /pools`, with a test; `openapi.json` regenerated | lock (noise), 0.39 s | green, 5.06 s | No false positive. **Re-pin needed** (pool_api, store_py) | — | — |
| P11 | B | `heapq.nsmallest` instead of a full sort in `_allocate` | lock (noise), 0.39 s | green, 5.13 s | No false positive. **Re-pin needed** | — | — |
| P12 | B | `logging.info` line in the lease endpoint | green, 4.79 s | — | Clean | — | — |
| F01 | C | Stored evidence end offset one short. Breaks K-2's statement ("a quote is at its source offsets"), but K-2 is enforced only on the model reply | lock (noise), 0.28 s | pytest red, 1.83 s | Ordinary test `test_evidence_offsets_are_absolute_into_the_source`. **No rule fired** | yes | yes |
| F02 | C | **Bypass:** new `Store.bulk_import` writes with `insert_fact_row`, skipping `ingest_door` (K-1, K-4, K-7, K-11, K-18) | lock (noise), 0.28 s | `inv scan` red, 0.50 s | `inv scan` (K-1 bypass). The K-1 trigger still backs evidence; K-4, K-7, K-11 and K-18 are skipped at run time | no | no |
| F03 | C | `Store.support` drops `trust = 'trusted'`, so the K-12 provider counts untrusted publishers | lock (noise), 0.28 s | pytest red, 1.84 s | Tests (`test_k12_untrusted…`: "DID NOT RAISE"; the hypothesis property). **The guard approved**, because its provider is part of the trusted base | no | yes |
| F04 | C | **Signature change:** `promote_door(fact_id)` → `promote_door(fact_ids: list)`, so the K-12 binding breaks | `inv validate` red, 0.14 s | — | Load check: `K-12: args.fact_id does not resolve`. `inv lock` refuses for the same reason | n/a¹ | n/a¹ |
| F05 | U | `audit_sample` uses `retry_count > 1` (off-by-one) | lock (noise), 0.28 s | pytest red, 1.71 s | Ordinary tests (`test_worked_flow_7_6`, `test_cli_flow`) | no² | yes |
| F06 | U | `retrieve` returns k−1 chunks | lock (noise), 0.28 s | green, 1.80 s | **Not caught** | no | no |
| F07 | U | the extraction review payload drops `error` and `attempts` (lost diagnostics) | lock (noise), 0.28 s | green, 1.81 s | **Not caught** | no | no |
| F08 | U | `candidates` drops `.lower()`, so the resolver mints duplicate entities | lock (noise), 0.29 s | pytest red, 1.83 s | Ordinary tests (`test_worked_flow_7_6`, `test_cli_entities_and_review`) | yes | yes |
| F09 | B | `Store.facts` as a comprehension | lock (noise), 0.29 s | green, 1.89 s | No false positive. **Re-pin needed** | — | — |
| F10 | B | new `stats` CLI subcommand, with a test | green, 2.02 s | — | Clean | — | — |
| F11 | B | `list.extend` in `Pipeline.extract` | lock (noise), 0.32 s | green, 1.95 s | No false positive. **Re-pin needed** | — | — |
| F12 | B | new predicate `founded_in`: models, the Literal, K-4's list in the YAML, and a test | lock: `rules K-4: hash changed`, 0.30 s | green, 1.91 s | No false positive. **Re-pin and a rule edit needed** | — | — |

¹ In F04 the defect is the rule binding itself. Without the library there is no binding to break,
and with it the rule file refuses to load, so no tests can run. It is counted as a catch for
invariants and left out of the baseline denominators below.
² B1 drops `test_worked_flow_7_6` and `test_cli_flow`, because they fail on the pristine app once
the guards are off.

Two extra observations do not change the table:
- **A raw-SQL bypass goes unseen.** As a probe outside the 24, I added an endpoint that runs
  `INSERT INTO leases` with raw SQL. `inv scan` passes it. R-20 declares no `mediation.tables`,
  because doing so would flag `release`'s `DELETE` (FRICTION #2). Only the SQLite trigger would
  stop an over-lease.
- **The pydantic contract pin missed a schema change.** In F12 the `FactReply` contract hash did
  not change, although its `predicate` field gained a value. The hash covers
  `inspect.getsource(FactReply)`, and the `Literal` lives in a module-level alias
  (`Predicate = Literal[...]`). Only K-4's rule hash moved.

### Catch rates in the original run

| Category | invariants CI | of which by an invariants mechanism | B1 (mechanical) | B2 (plain code + tests) |
|---|---|---|---|---|
| C: covered defects (8) | **8/8** (7/8 if the lock diff is re-pinned unread) | 6/8 (guard 2, scan 2, lock 1, load 1) | 2/8 | 4/7 (F04 left out) |
| U: uncovered defects (8) | 3/8 | 0/8 | 2/8 | 3/8 |
| All defects (16) | 11/16 (10/16 re-pinned unread) | 6/16 | 4/16 | 7/15 |
| B: false positives (8) | 0 test or scan failures; **6/8 red until re-pin** | — | 0 | 0 |

How to read these rates:
- **Runtime guards.** The guards fired on 2 changes (P01, P03). In both cases a plain `if` with
  the same tests would have caught the defect too (B2). The guards earn their keep in production,
  on inputs the tests never try, and this experiment cannot measure that.
- **Where invariants beat B2.** It caught P02 and F02 (scan), P04 (lock, if a human reads the
  diff) and F04 (load check, a defect the library's own wiring makes possible).
- **Rules did not catch these.** In F01 and F03 the property was covered by a rule, but the rule
  could not see the defect. In F01 the enforcement point was in the wrong place. In F03 the
  provider was the bug. Ordinary tests caught both.
- **Uncovered defects.** Invariants added nothing here, as the design guide says. The 3 catches
  were ordinary tests.
- **Two layers never fired.** `inv verify` and `--inv-ablate` produced no catch on any of the 24
  changes, and no false positive. They guard the integrity of the rule set itself (is every guard
  wired, is every guard load-bearing), not individual changes.

### The human-cost ledger

#### What it added

| Item | pool-service | fact-pipeline |
|---|---|---|
| Rule YAML, non-blank and non-comment lines | 235 | 245 |
| App source, non-blank lines | 588 (545 Python + 43 SQL) | 988 |
| Rule YAML as a share of app code | **40%** | **25%** |
| `invariants.lock` (generated, but reviewed in diffs) | 162 lines | 166 lines |
| Invariants plumbing in `src` | 14 `@inv` decorators, 4 providers, a transaction boundary, the storage-error mapping, `openapi.json` and its export script | 16 decorators; `providers.py`, `context.py` and `monitor.py` (97 lines); the ablation hook in `store.ddl()` (a test hook in production code) |
| Rules | 9 (all `enforce`) | 12 (11 `enforce`, 1 `detect`) |

The human decisions the rule files embody:
- **pool-service, about 69 decisions:** 9 status promotions, 10 `on_violation` choices (9 reject,
  1 escalate), 10 `on_error` choices (all `fail_closed`), 4 provider consistencies (2 snapshot,
  1 same_txn, 1 immutable), 9 `effects` declarations, 6 mediation arguments, 9 owners and
  12 falsifier designations.
- **fact-pipeline, about 89 decisions:** 12 status promotions, 15 `on_violation` choices
  (13 reject, 1 route, 1 escalate), 11 `on_error` choices, 8 consistencies (5 same_txn,
  3 snapshot), 5 `effects` declarations, 11 mediation arguments, 12 owners and 15 falsifiers.

FRICTION.md has 15 entries for pool-service and 17 for fact-pipeline. After the library fix
rounds, about 4 are still open in pool-service (column-level tables, OpenAPI taken from the live
app, duplicate scan findings, no sampled mode) and 7 in fact-pipeline. Several have left lasting
workarounds in the apps:
- a mirrored `bypass/` tree;
- `bad: 'false'` for a scan-only rule;
- the predicate vocabulary duplicated in the YAML, with a test that checks the two agree;
- two identical provider functions;
- a contextvar to pass the store to the providers;
- an OpenAPI export script and a test that checks the exported file is current.

**Lock churn.** 6 of the 8 benign changes needed `inv lock` before CI went green. Counting
defects as well, `lock --check` failed on 18 of 24 changes. Its message gave a specific signal
twice, naming the rule whose hash changed (P04, a real weakening, and F12, a legitimate rule
edit). The other 16 times it said only that a whole-module contract hash had changed (for
example `contracts store_py: hash changed`). Because the step is second in `ci.sh` under
`set -e`, any edit to a contract module stops CI in 0.3 s until someone re-pins.

**CI time.** The invariants steps add about 1.5 s to pool-service (5.0 s against 3.5 s, +43%) and
about 0.6 s to fact-pipeline (1.9 s against 1.3 s, +48%). In absolute terms that is negligible.

**Runtime overhead**, from the README's Performance table (p50 per call, guard on a pure function):
- **subset backend:** 7.1 µs for 1 clause, 22.5 µs for 4 clauses;
- **cel-expr backend:** 9.3 µs and 19.6 µs;
- **celpy backend** (removed since): 87 µs and 858 µs.

The FRICTION files report the cost on real targets. R-1's four clauses add about 42 µs to a 1 µs
function. The fact ingest door costs about 240 µs guarded against 12 µs bare, because every
provider runs on every call (the README's figure; this experiment did not re-measure it; on
2026-11-12, after an index fix in the app's store for the K-11 provider's query, it was about 125 µs
guarded against 11 µs bare, see fact-pipeline's FRICTION.md item 3). At HTTP
scale, about 2.5 ms per request, this is noise; on hot loops it is not.

#### What it saved

- **Two bypasses.** P02 and F02 added a new code path around a door, without a test. `inv scan`
  caught both with a clear message ("reaches the sink without passing …"). No unit test caught
  them, because no test exercised the new path. This is the one class where the machinery found
  what ordinary practice would have missed.
- **One rule weakening.** P04 surfaced as `rules R-22: hash changed`. That is useful, but only if
  a human reads the lock diff instead of re-pinning.
- **One broken rule binding.** F04 failed fast at load time with a precise message instead of
  failing closed at run time.
- **Indirect benefits.** The rule files pushed the authors to write property and differential
  tests (`test_r1_property`, `test_k12_promotes_iff_…`, `test_r20_door_and_trigger_agree`). Those
  tests caught P01, F03, F05 and F08 in the CI run. Some of them would exist without the library,
  so this experiment cannot cleanly separate the library's effect from that of the discipline.

### Original verdict

In this experiment invariants added more human work than it saved, per change. It is a net win
only under some conditions.

- **The per-change cost.** The rule files are a one-time cost of about 25–40% of app size and
  about 160 decisions across the two apps. On top of that, every module edit costs a lock re-pin: 75% of benign
  changes needed one.
- **The per-change benefit.** Over plain checks plus tests, it caught 2 bypasses of 16 defects,
  plus 1 rule weakening that depends on review and 1 binding error. It caught none of the
  8 uncovered defects. 3 of those were caught by ordinary tests, and 5 (31% of all defects)
  escaped everything.

It reduces work for humans when:
- the app has a few high-stakes properties (money, quotas, approvals, provenance) that
  agent-written code might route around;
- new code paths often appear without tests, so scan-style mediation checks catch what tests
  cannot;
- reviewers really read rule and lock diffs, so a weakening is noticed rather than re-pinned;
- the rule set stays small and stable, so its one-time cost is amortised over many changes.

It adds work when:
- most changes are ordinary refactors or features, because contract pins at module granularity
  turn each one into a re-pin;
- the team, or the agent, re-pins by reflex, because that erases the lockfile's only unique
  catch (P04);
- the defects that matter are business logic no rule states (5 of 8 uncovered defects escaped);
- properties depend on providers or on enforcement points that are themselves buggy (F01, F03),
  because there the rule gives a false sense of coverage and the tests still do the work.

Two changes would most improve the ratio, and both are library work:
- pin contracts at signature and schema granularity instead of the whole module, which would have
  cut lock churn from 18 of 24 changes to about 3 here (P04, F04, F12);
- let `mediation.tables` work per column and per statement kind, so raw-SQL bypasses of R-20 are
  in scope.

## Limitations

- **Author bias.** I wrote all 24 changes and the ground truth, after reading the rules, the scan
  semantics and the tests. The bypasses (P02, F02) have exactly the shape the scan models: a
  direct call to a declared sink. My raw-SQL probe shows that a slightly different bypass is
  missed. Real agent defects would be distributed differently.
- **Small sample.** There were 24 changes, 8 per category, in 2 apps. One case more or less moves
  a rate by 12 points. No confidence intervals are given because they would be meaningless at
  this size.
- **Synthetic apps.** Both apps were built to demonstrate the library. Their tests were written
  alongside the rules, and many of them assert rule ids. That is why B1 has to drop 31 of 72 and
  43 of 86 tests, and why B2 is a judgement, not a measurement.
- **What it does not measure.** It does not measure production value (guards firing on inputs no
  test tried, monitors over live data) or the review time spent reading lock and rule diffs. The
  runtime overhead comes from the README and FRICTION, not from new measurements.
- **A library under development.** The library was still being changed while the original run
  took place. All 24 runs, the control runs and the baselines finished without unexplained
  failures, so no re-runs were needed for that reason.

## Reproducing the numbers

The harness ships in [evaluation/](../evaluation): the ground truth written before any run
(`GROUND_TRUTH.md`), the text edits and patches (`changes.py`, `patches/*.patch`), the runner
(`run.py`), and the per-change result files of the published re-run (`published/`). The bulky
per-run logs are not shipped; the runner writes them again. From `invariants-py`, in a venv with
`pip install -e '.[dev,cel]'`:

```bash
python evaluation/run.py                  # all 24 changes, about 5 minutes; P02 F12 for a subset
```

It copies `apps/pool-service` and `apps/fact-pipeline` into a temporary working directory (the
repository is never modified), runs each app's `ci.sh` on the unchanged copy, finds B1's deselect
list, then applies each change, runs `ci.sh` (and the reflex re-pin when the lock is stale) and B1,
undoes it, and prints a table that compares each result with the published one. See
[evaluation/README.md](../evaluation/README.md) for the options.

A re-run on 2026-11-12, on the apps as they are in this repository, reproduced every headline
number of the current results: 2 of 24 lock failures (P04 and F12, both refused on the reflex
re-pin), 1 of 8 benign changes red, 8 of 8 covered and 3 of 8 uncovered defects caught by CI, and
the same 6 caught by an invariants mechanism. 23 of the 24 results match exactly; P03 fails 7 tests
instead of 6 because the current app has one more happy-path test through an approval. Because the
apps changed below their APIs since the published run, P04 is applied from a re-based edit (the same
4x to 10x weakening of R-22) and P12 from its text edits. The details are in
[evaluation/RESULTS.md](../evaluation/RESULTS.md).
