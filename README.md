# invariants-first

Monorepo for invariant-first rule files: lock the rules your code already has, so no change, by a
person or a coding agent, can loosen them without someone saying yes.

| Path | What it is |
|---|---|
| [`invariants-py/`](invariants-py) | The Python library and `inv` CLI (package `invariants-first`, import `invariants`). Start with its [README](invariants-py/README.md) and the [10-minute guide](invariants-py/docs/start.md). |
| [`apps/pool-service`](apps/pool-service) | Example app: a FastAPI and SQLite lease service with an LLM triage step. Uses only the library's public API. |
| [`apps/fact-pipeline`](apps/fact-pipeline) | Example app: a batch pipeline that extracts cited facts with a model (a deterministic fake). |
| [`prototype/`](prototype) | The first prototype the library grew from. Kept for history; not maintained. |
| [`Invariant_first.md`](Invariant_first.md) | The design guide: the reasoning behind the design, the industry survey, and the limits. |
| [`Invariants_what_how_when.md`](Invariants_what_how_when.md), [`makeInvariantSimple.md`](makeInvariantSimple.md) | The original concept notes. |
| `action.yml`, `.pre-commit-hooks.yaml` | The GitHub Action and the pre-commit hook. They sit at the root so other repositories can use them as `jkuru/invariants-first`. |

Status: alpha (0.1.0), not yet on PyPI. Each app's `ci.sh` runs every check against the library in
this checkout:

```bash
cd invariants-py && python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev,cel]'
python -m pytest tests -q
../apps/pool-service/ci.sh && ../apps/fact-pipeline/ci.sh
```

The library is licensed under Apache-2.0; see [`invariants-py/LICENSE`](invariants-py/LICENSE).
