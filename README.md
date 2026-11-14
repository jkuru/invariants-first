# invariants-first

Monorepo for invariant-first rule files. OpenAPI and pydantic define the shape of your APIs; the rule
file defines their laws at every layer (HTTP route, service, repository, model call, external client):
value laws on an API's inputs and outputs, and path laws on which API may call which. `inv init` locks
the laws your code already has, so no change, by a person or a coding agent, can loosen one or go
around a service without someone saying yes.

| Path | What it is |
|---|---|
| [`invariants-py/`](invariants-py) | The Python library and `inv` CLI (package `invariants-first`, import `invariants`). Start with its [README](invariants-py/README.md) and the [10-minute guide](invariants-py/docs/start.md). |
| [`packages/invariants-cel`](packages/invariants-cel) | The CEL engines and the type check (package `invariants-cel`, import `invariants_cel`), usable on their own. |
| [`packages/invariants-scan`](packages/invariants-scan) | The path-law scan and `inv-scan` (package `invariants-scan`, import `invariants_scan`), usable on its own. |
| [`packages/invariants-schema-diff`](packages/invariants-schema-diff) | Is a JSON Schema or CEL edit a weakening? (package `invariants-schema-diff`, import `invariants_schema_diff`; depends on `invariants-cel`). |
| [`apps/pool-service`](apps/pool-service) | Example app: a FastAPI and SQLite lease service with an LLM triage step. Uses only the library's public API. |
| [`apps/fact-pipeline`](apps/fact-pipeline) | Example app: a batch pipeline that extracts cited facts with a model (a deterministic fake by default; `APP_MODEL` picks replay, record or Claude). |
| [`apps/support-agent`](apps/support-agent) | Example app: a tool-calling support agent (planner, executor edge, billing doors) with 12 seeded agent defects, each caught. |
| [`Invariant_first.md`](Invariant_first.md) | Invariant-first, for engineers: the two kinds of law, how each is checked, the architecture, a worked agent example and the limits. |
| [`Invariants_what_how_when.md`](Invariants_what_how_when.md), [`makeInvariantSimple.md`](makeInvariantSimple.md) | The original concept notes. |
| `action.yml`, `.pre-commit-hooks.yaml` | The GitHub Action and the pre-commit hook. They sit at the root so other repositories can use them as `jkuru/invariants-first`. |

`invariants-first` depends on the three libraries, which are released with it at the same version
([`invariants-py/RELEASING.md`](invariants-py/RELEASING.md)); `invariants.cel` and `invariants.scan`
re-export their public API.

Status: alpha (0.1.0), not yet on PyPI. Each app's `ci.sh` runs every check against the library in
this checkout:

```bash
cd invariants-py && python -m venv .venv && . .venv/bin/activate
pip install -e ../packages/invariants-cel -e ../packages/invariants-scan -e ../packages/invariants-schema-diff -e '.[dev,cel]'
python -m pytest tests -q
../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh && ../apps/support-agent/ci.sh
```

The library is licensed under Apache-2.0; see [`invariants-py/LICENSE`](invariants-py/LICENSE).
