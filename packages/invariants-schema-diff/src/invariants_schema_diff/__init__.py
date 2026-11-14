"""invariants-schema-diff: is an edit a weakening?

Two judges, both static and standard library only (the CEL one parses with invariants-cel):

- a JSON Schema edit: does the new schema accept input the old one refused (a bound raised or removed,
  an enum widened, a field no longer required, a closed object opened, a $ref re-pointed, ...)?
  `schema_changes(old, new)` lists each leaf difference as a Change, `weakened` when it may accept more;
  `constraint_weakening` judges one difference (schemas).
- a CEL condition edit: does `new` flag the same cases as `old`, more ("stronger"), fewer ("weaker"),
  or can it not be decided ("unknown")? `compare_bad(old, new, parse)` (cel_mono, over the canonical
  trees of cel_canon and the error sites of cel_sites).

Modules: changes (canonical JSON, Change, the leaf-level JSON diff), schemas, cel_canon, cel_sites,
cel_mono. invariants-first's lock (`inv lock`, `inv check`) is built on them.
"""
from __future__ import annotations

from typing import Any

from .cel_canon import Typer, canon_tree, make_typer
from .cel_mono import compare_bad, judge_bad
from .cel_sites import new_error_sites
from .changes import Change, _json_paths, canonical, same
from .schemas import _schema_entry_changes, constraint_weakening, ref_polarity

__all__ = ["Change", "Typer", "canon_tree", "canonical", "compare_bad", "constraint_weakening", "judge_bad",
           "make_typer", "new_error_sites", "ref_polarity", "same", "schema_changes"]


def schema_changes(old: Any, new: Any, where: str = "schema") -> list[Change]:
    """Every leaf difference between two JSON Schemas, as a Change at `where`: `what` is the path
    ("jsonschema.properties.amount.maximum", "jsonschema.required removed"), `weakened` is True when the
    edit may let the schema accept more input, and `note` says why. [] when they are the same."""
    ra, rb = {"jsonschema": old}, {"jsonschema": new}
    return _schema_entry_changes(where, "", ra, rb, _json_paths(ra, rb))
