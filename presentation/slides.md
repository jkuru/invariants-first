---
marp: true
theme: default
paginate: true
title: "invariants-first: A One-Way Ratchet for Your API Rules"
description: "Presentation for 11/18 — value laws, path laws, runtime defense-in-depth, and open-source demos"
---

# invariants-first

### A One-Way Ratchet for Your API Rules

**A change can tighten your rules freely — loosening one, by a person or a coding agent, fails CI until a reviewer says yes.**

*Presentation — Nov 18, 2026*

<!--
Speaker Notes (0:00 - 1:00):
- Open with the title and thesis immediately: invariants-first is a one-way ratchet for API rules.
- Don't pitch it as "a CEL/AST framework." Pitch it around the coding-agent and LLM-agent failure mode everyone in the room recognizes.
-->

---

## 1. The Problem: "Tests Pass, Production Breaks"

Coding agents optimize to make tests pass. When a constraint is in their way:
- They raise a Pydantic limit (`50_000 -> 500_000`) or widen a type
- They delete a check or skip the service layer and call `StripeGateway.refund()` directly
- Every diff looks plausible in review — **and all tests still pass**

Runtime LLM agents have the twin problem — **valid shape, invalid behavior**:
- The model outputs schema-valid JSON that refunds *another customer's* order or exceeds what is left on the invoice

<!--
Speaker Notes (1:00 - 3:00):
- Give a concrete example: an agent hits a validation error in a test, bumps `le=50_000` to `500_000`, re-runs pytest, and opens a green PR.
- Point out that JSON Schema / Pydantic only check *shape* ("is amount_cents an int > 0?"), not *reality* ("does this exceed remaining_cents in the ledger?").
-->

---

## 2. Core Idea: Separate Shape, Laws, and Bodies

Every layer of a system is an API: HTTP route, service method, repository, model call, external gateway.

| Concern | Owned by | Examples |
|---|---|---|
| **Shape** | Pydantic / OpenAPI | Types, field bounds, `Literal`s |
| **Laws** | People (`invariants.yaml` + `invariants.lock`) | **Value laws** (inputs/outputs never allowed) & **Path laws** (which API may call which) |
| **Bodies** | People or coding agents | Business logic inside functions — **free to rewrite** |

> Path laws hold on the boundary regardless of what sits behind the sink: SQLAlchemy, Django, Stripe, or raw SQL.

<!--
Speaker Notes (3:00 - 5:30):
- Emphasize division of labor: humans own the boundary laws; agents keep total freedom inside function bodies.
- Explain the two kinds of law:
  1. Value laws (CEL expressions + pinned schemas/contracts)
  2. Path laws (static call-graph reachability from entrypoints through doors to sinks)
-->

---

## 3. How the One-Way Ratchet Works

1. **Lock what you already have (`inv init`)**
   - Zero hand-written rules on day 1
   - Reads Pydantic constraints into `contracts`, discovers service-to-repo `door` rules, and freezes existing back doors in `allow:` so CI passes immediately
2. **Tightening goes through (`inv lock`)**
   - Narrow a bound or add a rule -> re-pin the lock -> commit
3. **Loosening needs a person (`inv check --base main`)**
   - Compares against `main` — **even if the agent ran `inv lock --accept-weakening` itself**
   - Fails CI until a human reviewer explicitly accepts (e.g., via PR label)

<!--
Speaker Notes (5:30 - 7:30):
- Highlight the killer detail: agents *will* try to update the lockfile to hide a loosening.
- Because `inv check --base main` diffs semantics against the base branch's lock/rules (using JSON Schema + CEL monotonicity), self-re-pinning doesn't fool CI.
-->

---

## 4. Live Demo 1A: Open-Source `fastapi` (Zero-Config Ratchet)

Running `inv init` on an untouched copy of **FastAPI** (`./presentation/demo.sh 1`):

```text
$ inv init --root . --path .
wrote invariants.yaml
  read 52 source file(s)
  pinned 4 pydantic model(s) with 12 constraint(s)
  suggested 6 rule(s) from function preconditions (commented out)
wrote invariants.lock: 0 rule(s), 0 target(s), 0 provider(s), 4 contract(s), 4 schema(s)
```

Simulate an agent weakening `fastapi/openapi/models.py` (`multipleOf: gt=0 -> ge=0`) and trying to hide it with `inv lock --accept-weakening`:

```text
$ inv check --base main
  FAIL  since main
        2 change(s) since main weaken a rule:
          schema fastapi.openapi.models.Schema: pydantic.properties.multipleOf.anyOf[0].exclusiveMinimum: 0 -> (none)  WEAKENED (exclusiveMinimum removed: no lower bound)
        a person must accept these: re-run with --accept-weakening or INV_ALLOW_WEAKENING=1
```

<!--
Speaker Notes (7:30 - 10:00):
- Run `./presentation/demo.sh 1` live (takes ~5 seconds total for both FastAPI and LangChain Core).
- Point out that FastAPI has 0 hand-written invariant config — `inv init` found `ServerVariable`, `Schema`, `HTTPBearer`, `ServerSentEvent`, plus 6 precondition rule candidates automatically.
- Also note honestly: `inv init` finds no door rules on FastAPI because a web framework library has no service/repository layers.
-->

---

## 5. Live Demo 1B: Open-Source `langchain_core` (Tighten vs. Weaken)

On **`langchain_core`** (187 source files):
- `inv init` pins **35 Pydantic models, 47 constraints** (`AgentAction`, `AgentFinish`, `AIMessage`, `Document`, `ChoiceAnswer`, `NoulAnswer`, ...)

1. **Safe tightening** (`Choice.criteria`: `min_length=1 -> min_length=2`):
   - `inv lock` succeeds &rarr; `inv check --base main` **PASSES** (exit 0)
2. **Sneaky weakening** (`NoulAnswer`: `le=1.0 -> le=2.0` + `inv lock --accept-weakening`):
   - `inv check --base main` **FAILS** (exit 1):

```text
schema langchain_core.decisions.types.NoulAnswer: pydantic.properties.noul.maximum: 1.0 -> 2.0  WEAKENED (maximum raised: larger values pass; used by contract noul_answer schema)
```

<!--
Speaker Notes (10:00 - 12:00):
- Show the contrast directly in terminal output: tightening needs no reviewer sign-off; widening confidence `le=1.0 -> 2.0` fails even after the agent re-locked it.
-->

---

## 6. Live Demo 2: Making the Ratchet Unskippable

- **Claude Code `PreToolUse` hook (`inv hook pretool`)** blocks tampering *before* CI:
  - Editing `invariants.lock` directly &rarr; **exit 2**
  - Running `inv lock --accept-weakening` &rarr; **exit 2**
- **`apps/support-agent` CI Defect Suite** (`tests/test_ci_defects.py`, 13 tests):
  - **D1**: New tool calls `StripeGateway.refund` directly &rarr; caught by `inv scan`
  - **D2**: `StripeGateway.refund` placed in `TOOLS` dict &rarr; caught (`sink used as a value`)
  - **D3**: Refund cap `50_000 -> 500_000` + re-lock &rarr; caught by `inv check --base main`
  - **D4**: `semantic_resamples: 1 -> 5` &rarr; caught (`rule weakening`)
  - **D5**: Tool routed through legacy `admin.goodwill_refund` in `allow:` &rarr; caught (`bypass`)
  - **D11 / D12**: Agent deletes `@inv.guard("BILL-1")` or deletes rule `BILL-2` &rarr; caught (`verify` / `lock`)

<!--
Speaker Notes (12:00 - 14:30):
- Run `./presentation/demo.sh 2`.
- Highlight D5: freezing legacy back doors in `allow:` doesn't turn the legacy function into a blessed door — routing *new* callers through it still fails `inv scan`.
-->

---

## 7. Live Demo 3: Static Path-Law Bypass Scan (`inv scan`)

AST + type-inference call-graph scan on `apps/pool-service` (`./presentation/demo.sh 3`):

```text
$ PYTHONPATH=src inv scan invariants.yaml --root src --root bypass
```

Catches **10 findings across 4 rules** in `bypass/pool_service/rogue.py` in **< 1s**:
- Direct sink bypasses (`store.set_size`, `store.insert_lease`, `store.insert_pool`)
- `forbidden/unwrap` via `fn.__wrapped__`
- `forbidden/getattr-sink` via `getattr(store, "set_size")`

- **Scale:** scans **104 modules / 1,852 functions in 1.17 s** (`invariants-py` codebase).
- Also runs on OSS packages: on `rich` (100 files), `inv init` finds 37 back doors and 66 unresolved dynamic sites and lists five of each (door section 281 → 123 lines).

<!--
Speaker Notes (14:30 - 17:00):
- Run `./presentation/demo.sh 3`.
- Explain why naive grep isn't enough: agents unwrap decorators (`__wrapped__`), alias modules, use `getattr`, or pass sinks as values in dicts (`TOOLS`). `invariants-scan` tracks local types, factory returns, escapes, and reflection idioms.
-->

---

## 7b. Quiet on Day One: Diff-First Scan + Run-Time Door Check

**Two tiers in `inv check`**
- **Blocks:** a call that skips a door, `getattr` on a sink, `__wrapped__`, monkeypatching
- **Warns:** a call the scan can't follow (an untyped `x.save()`)
- **Diff-first with `--base main`:** a site already on `main` warns; one the PR adds fails
- `strict: true` on a rule, or `--strict-scan`, for sinks that need every site reviewed

**Run-time backstop: `pytest --inv-doors`**
- A door marks itself active while it runs; a sink checks that one of its doors is
- Catches `getattr`, DI containers and callbacks on every path the tests run
- `inv.doors.install(mode="detect")` for staging

<!--
Speaker Notes:
- No day-one annotation tax: legacy dynamic calls are warnings, but an agent can't add a new one unnoticed.
- Start with the one to three sinks an agent must never reach directly.
-->

---

## 8. Runtime Defense-in-Depth (`support-agent`)

What CI cannot see happens at runtime when the LLM plans an action or held data changes:

```text
ticket -> triage -> planner (@inv.model_call: PLAN-1) -> execute_plan (@inv.edge) -> tool -> BillingService (@inv.guard: BILL-1, BILL-2) -> StripeGateway / LedgerRepo
                          |
                          +-> abstain / exhausted -> human handoff
```

- **`@inv.model_call` (`PLAN-1`):** Closed Pydantic schema (`Plan`), 2 format retries (with parser error), 1 blind semantic resample against live `planned_order` / `planned_tier`, or `ESCALATE_TO_HUMAN`.
- **`@inv.edge` (`execute_plan`):** Requires an unforgeable `CheckedPlan` token *and* re-validates `PLAN-1` against live ledger state (catches stale state / mid-flight tampering).
- **`@inv.guard` (`BILL-1`, `BILL-2`):** Enforces invariants on the door itself, whoever calls it.

<!--
Speaker Notes (17:00 - 19:30):
- Run `./presentation/demo.sh 4` (`test_runtime_defects.py`, D6–D10).
- Emphasize `fakestripe.REFUNDS`: in every failure mode (over-refund, cross-customer order, invalid JSON, forged `CheckedPlan`, mutated plan/ledger between planner & executor), no money ever moves.
-->

---

## 9. Proving Guards Aren't Theater (`--inv-ablate`)

How do you know a rule actually blocks bad behavior rather than silently passing?

**`pytest --inv-ablate`** switches each rule off one by one and checks that a named falsifier test fails (`load-bearing`), elevating the rule's standing to **`held`**:

```bash
./apps/fact-pipeline/ci.sh
inv status invariants.yaml --import fact_pipeline.pipeline --ablation build/inv-report.json
```

```text
----------------------------- invariants ablation ------------------------------
  K-1: load-bearing
      caught  tests/test_store_rules.py::test_k1_guard_refuses_fact_without_evidence
  K-2: load-bearing
      caught  tests/test_extract.py::test_k2_persistent_misquote_escalates
...
```

<!--
Speaker Notes (19:30 - 21:30):
- Run `./presentation/demo.sh 5`.
- Note the practical tip: run `inv status --ablation build/inv-report.json` *after* `pytest --inv-ablate` so `inv status` shows `held` / `ablation proved it: yes`.
-->

---

## 10. Evaluation, Benchmarks & Architecture

### Controlled Evaluation (`docs/evaluation.md`, 24 seeded changes)
- **Defects caught:** **8 / 8** covered defects caught (6/8 by invariants mechanisms: scan bypass, runtime guard, lock weakening, load check; vs **4 / 7** for plain code + tests).
- **Contract-level lock redesign:** Lock failures dropped **18/24 &rarr; 2/24**; benign false positives dropped **6/8 &rarr; 1/8**; reflex `inv lock` accepted **0** weakenings.
- **Graphify 0.9.107 comparison (5 codebases):** Added **0** missed door rules over naming heuristics (tree-sitter communities cluster by feature call chains, not layers).

### Performance (Apple M4 Max, Python 3.14)
- **Guard overhead (no providers):** **7.1 µs** p50 / **8.4 µs** p99 (1 clause, `subset`); **22.7 µs** p50 / **28.3 µs** p99 (4 clauses).
- **Guard + SQLite providers (`fact-pipeline` `ingest_door`):** **~122–125 µs** p50.
- **Static scan:** **1.17 s** across 104 modules / 1,852 functions.

### Monorepo (Alpha `0.1.0`, Apache-2.0)
- `invariants-first` (`invariants-py`, 6,969 tests) + 3 standalone packages: `invariants-cel`, `invariants-scan`, `invariants-schema-diff` (2,057 tests) + 3 example apps (`pool-service`: 73, `fact-pipeline`: 92, `support-agent`: 51) + `mypy` clean.

<!--
Speaker Notes (21:30 - 24:00):
- Highlight why contract-level pinning mattered: early module-hash locks broke 18/24 changes (massive friction); diffing schema/CEL semantics cut benign breaks to 1/8 while catching every seeded weakening.
-->

---

## 11. Honest Limits & Open Edges

1. **Nothing below the API**
   - Concurrency races (e.g., `pool-service` `R-20` explicitly documents `residual_paths` and stays `enforce(known-callers)`, not `held` — DB triggers handle the race) and buggy `@inv.provider` code are out of scope.
2. **Dynamic Python escapes**
   - `inv scan` catches `getattr`, `__dict__`, `__wrapped__`, aliases, and escapes, though arbitrary `eval`/C-extension dispatch is out of scope.
3. **Evaluation caveats**
   - Self-authored example apps/defects; defect tests run on deterministic fakes and hand-written replay fixtures (not live production LLM traces).
4. **An external audit, reproduced before fixed**
   - Each finding got a failing test first; confirmed ones were fixed (round 35), rejected ones left with the reason. **109 of 110** hand-written mutants of the fixes are caught (the last is equivalent).

<!--
Speaker Notes (24:00 - 26:30):
- Being upfront about limits makes engineers trust the system immediately.
- Mention R-20 explicitly: the tool itself refuses to mark `R-20` as `held` because a check on API arguments cannot replace a database transaction/trigger.
-->

---

## 12. Try It & Next Steps

```bash
# Install from this checkout
cd invariants-py && python -m venv .venv && . .venv/bin/activate
pip install -e ../packages/invariants-cel -e ../packages/invariants-scan \
            -e ../packages/invariants-schema-diff -e '.[dev,cel]'

# On your project (zero rules required to start)
inv init
inv check --base main
```

### Gradual Adoption Path
1. **Today (CI only):** `inv init` + `inv check --base main` + pre-commit / Claude Code hook
2. **Critical boundaries:** Add `@inv.guard`, `@inv.model_call`, `@inv.edge` on money/data doors
3. **Prove coverage:** `pytest --inv-verify --inv-ablate`

**Repo:** `invariants-first` (`jkuru/invariants-first`) &bull; **Guide:** `invariants-py/docs/start.md`

<!--
Speaker Notes (26:30 - 28:00):
- Close on the 3-step adoption path: start with zero prod code edits (`inv init` + `inv check --base main`), then add runtime guards only where LLM outputs or state doors need them.
- Open for Q&A.
-->
