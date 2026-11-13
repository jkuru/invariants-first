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

The file it writes passes `inv check` as written. It never overwrites a rule file.

The package is split by seam; this module keeps the namespace the single bootstrap.py had:
- translate.py: Python -> CEL (one condition, its truth tests by annotation, module constants)
- finding.py: discovery (preconditions into Guards, constrained pydantic models into Models, discover())
- writing.py: rendering the rule file text and the summary lines
"""
from __future__ import annotations

from .finding import (CON_TYPES, EXCLUDE, FIELD_CONSTRAINTS, MODEL_ROOTS, Found, Guard, Model,  # noqa: F401
                      _constraint_terms, _guard, _guards, _kinds, _message, _models, _params, _preconditions,
                      discover)
from .translate import (_ARITH, _CMP, _METHODS, _NEG, _SIZED, Unsupported, _base_name, _Cel,  # noqa: F401
                        _const_node, _literal, _raise, module_constants, negate, to_cel, truth_kind)
from .writing import (_contract_lines, _doors_summary, _q, _rule_id, _snake, _suggested_lines,  # noqa: F401
                      render, summary)

# names bootstrap.py imported, kept so invariants.bootstrap.<name> still resolves (and annotations of the
# classes below, whose module is this one, evaluate here)
from typing import Any, Iterable  # noqa: F401
from ..scan import anywhere, iter_sources  # noqa: F401

# `from invariants.bootstrap import *` exports exactly the public names bootstrap.py's star import gave (it
# had no __all__), and no modules: without this list it would bind the submodules over a caller's own
# names. The modules bootstrap.py leaked (ast, dataclasses, json, pathlib, re) are not exported.
__all__ = ["Any", "CON_TYPES", "EXCLUDE", "FIELD_CONSTRAINTS", "Found", "Guard", "Iterable", "MODEL_ROOTS",
           "Model", "Unsupported", "annotations", "anywhere", "discover", "iter_sources", "module_constants",
           "negate", "render", "summary", "to_cel", "truth_kind"]

# Classes keep the module name they had before the split (reprs, pickles and pinned signatures name it).
for _cls in (Unsupported, _Cel, Guard, Model, Found):
    _cls.__module__ = "invariants.bootstrap"
del _cls
