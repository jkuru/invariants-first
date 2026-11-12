# support-agent

A tool-calling support agent that issues refunds, credits and cancellations. It is the flagship test
of the [`invariants`](../../invariants-py/README.md) library: every layer is an API, and every law sits
on one of them. It needs no API key; `FakeAgentModel` stands in for the model and can be told to
misbehave.

```
ticket -> triage_node -> planner_node -> router -> tool_executor_node -> tool -> BillingService -> StripeGateway / LedgerRepo
            (graph)     (@inv.model_call)            (@inv.edge)                (@inv.guard: the doors)    (the sinks)
                              |  abstain / exhausted
                              +-> human_handoff_node (a review is opened)
```

## Install and run

```
pip install -e '../../invariants-py[pydantic,schema]'   # the library from this checkout (or pip install invariants-first)
pip install -e '.[dev]'
./ci.sh                                                  # inv check, inv status, then pytest with ablation
```

`./ci.sh` makes a private `.venv` on its first run unless you set `PYTHON` or activate a venv.

## How the rule file was made

`invariants.yaml` was started with `inv init` on the code, before any rule was written by hand. It
found, with no hints:

- the 4 pydantic models (the three tool inputs and the planner's `Plan`), 23 constraints, pinned;
- two door rules: `LedgerRepo`'s writes go through the three `BillingService` methods, and
  `StripeGateway.refund` through `BillingService.refund`, with the 3 tools as their callers;
- the one real back door, the legacy `admin.goodwill_refund` (and its caller `admin_goodwill`),
  frozen in `allow`.

Then three runtime laws were added by hand: BILL-1 and BILL-2 on the doors, and PLAN-1 on the planner
and its only consumer.

| Id | Kind | Law |
|---|---|---|
| LEDGER-REPO-DOOR | path | the ledger's writes are reached only through `BillingService` |
| STRIPE-GATEWAY-DOOR | path | the payment gateway is reached only through `BillingService.refund` (the legacy admin refund is frozen in `allow`) |
| contracts | value | the tool inputs and the plan keep their pydantic constraints (refund cap 50,000, credit cap 10,000, ...) |
| BILL-1 | value | a refund is for a known order and never above what is left of it, whoever calls the door |
| BILL-2 | value | a credit never exceeds what the customer's tier still allows |
| PLAN-1 | value | a plan never refunds more than is left of the customer's own order, never over-credits, never acts for another customer; 2 format retries, 1 resample, `ESCALATE_TO_HUMAN` hands off; re-checked at the executor edge |

## The seeded defects

Twelve defects an agent (the model at run time, or a coding agent in a pull request) can introduce.
Each has a test; the CI ones copy the app, make the change and run `inv check` in a fresh process.

| # | Defect | Caught by | Test |
|---|---|---|---|
| 1 | a new tool calls `StripeGateway.refund` directly (through the context, or a fresh gateway) | `inv check`: bypass | `test_ci_defects.py::test_d1_*` |
| 2 | `StripeGateway.refund` put in the `TOOLS` registry | `inv check`: the sink used as a value | `test_d2_*` |
| 3 | the refund cap raised from 50,000 to 500,000 | `inv check`: schema lock, WEAKENED | `test_d3_*` |
| 4 | `semantic_resamples` raised from 1 to 5 | `inv check`: lock, WEAKENED | `test_d4_*` |
| 5 | a new tool routed through the legacy back door in `allow` | `inv check`: bypass (an allowed back door is not a door); allowing it too is a weakening | `test_d5_*` |
| 6 | the model plans a refund above what is left, on another customer's order, or an over-credit | PLAN-1 (resample, then a person); BILL-1 / BILL-2 at the door whoever calls | `test_runtime_defects.py::test_d6_*` |
| 7 | invalid JSON, an unknown tool name, an extra field | the closed `Plan` schema: retried with the parser error, at most 3 attempts, then a person | `test_d7_*` |
| 8 | the model answers `ESCALATE_TO_HUMAN` | abstain: a review is opened, nothing runs | `test_d8_*` |
| 9 | a forged `CheckedPlan` that never went through the planner, or one checked for another customer | `@inv.edge` on the executor | `test_d9_*` |
| 10 | graph state changes the plan after the planner checked it, or the ledger moved since | `@inv.edge` re-validates and re-checks PLAN-1 on what it receives | `test_d10_*` |
| 11 | the coding agent deletes `@inv.guard("BILL-1")` | `inv check`: verify | `test_d11_*` |
| 12 | the coding agent deletes rule BILL-2 | `inv check`: lock, WEAKENED | `test_d12_*` |

Safe changes pass: a new tool that goes through `BillingService`, and a tighter cap (re-pinned by
`inv check --relock` with no person needed).

## What it does not show

- Concurrency: two agents refunding the same order at once can both pass BILL-1 (a check is not a
  lock). The ledger's own transaction is the implementation's business, below every rule.
- The model is a fake. The rules see only its JSON, so a real model changes nothing in the checks,
  but its failure rates are its own.
