"""`inv init`: draft a first rule file from the laws a codebase already has, on its API boundaries.

It reads the source (it does not import it) and finds:

- pydantic models with constraints (Field(gt=..., le=..., max_length=...), Literal, constr/conint...):
  value laws on the APIs that take or return them. Each becomes a `pydantic` contract. Nothing else is
  needed: `inv lock` pins the model's JSON Schema, and a later change that lets it accept more input
  is refused as a weakening.
- doors (doors.py): path laws between the service layer and the repositories it calls.
- preconditions at the top of a function: `if <cond>: raise ...` and `assert <cond>`, where the condition
  only reads the parameters. Each function's preconditions become one short-form rule (guard + bad),
  translated from Python to CEL. These are written commented out: a rule needs `@inv.guard("ID")` on the
  function before `inv check` passes, so `inv init` leaves that decision, and the edit, to a person.

Before writing (unless `--no-lock`), `inv init` tries the lock's pin on each drafted contract, importing
its model as `inv lock` will; a model pydantic cannot give a JSON Schema for (an arbitrary-type field)
or whose module does not import is written commented out, with the reason. The file it writes passes
`inv check` as written. It never overwrites a rule file.

The package is split by seam:
- translate.py: Python -> CEL (one condition, its truth tests by annotation, module constants)
- finding.py: discovery (preconditions into Guards, constrained pydantic models into Models, discover())
- writing.py: rendering the rule file text and the summary lines
"""
from __future__ import annotations

from .finding import CON_TYPES, EXCLUDE, FIELD_CONSTRAINTS, MODEL_ROOTS, Found, Guard, Model, discover  # noqa: F401
from .translate import Unsupported, module_constants, negate, to_cel, truth_kind  # noqa: F401
from .writing import contract_record, contracts, render, summary  # noqa: F401

# `from invariants.bootstrap import *` exports these names and no submodules.
__all__ = ["CON_TYPES", "EXCLUDE", "FIELD_CONSTRAINTS", "Found", "Guard", "MODEL_ROOTS", "Model",
           "Unsupported", "contract_record", "contracts", "discover", "module_constants", "negate", "render",
           "summary", "to_cel",
           "truth_kind"]
