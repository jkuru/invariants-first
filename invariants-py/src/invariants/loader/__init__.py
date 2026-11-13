"""Load and validate a rule file (JSON or YAML, a path or a dict). Every defect is a LoadError.

The package is split by seam:
- reading.py: reading the file from disk (YAML or JSON), and the bundled JSON Schema's path
- short_forms.py: the short rule forms (guard/tests, door/sink/allow) rewritten into the full form
- shapes.py: shape checks on raw records (known, removed and enum fields; root paths; mediation)
- schemas.py: Python types the file names or the code declares (provider types, annotations, closed schemas)
- loading.py: load(), the document (contracts, targets, providers, rules) into a Registry
- rules.py: one rule (its clauses, enforcers, names, target, bindings, and the status its evidence allows)
- paths.py: what each enforcer reads, whether it is set at that point, binds, or needs an old_self copy
- policy.py: model_policy checks against the closed schema
- typebind.py: the static types of the names a clause reads, and the load-time type check
"""
from __future__ import annotations

from .loading import load, log  # noqa: F401
from .paths import binding_problem  # noqa: F401
from .reading import YAML_SUFFIXES, read_document, schema_path  # noqa: F401
from .schemas import closed_schema, literal_eq, schema_fields, schema_name, type_hints  # noqa: F401
from .shapes import REMOVED, REMOVED_VALUES  # noqa: F401
from .short_forms import DOOR_FIELDS, SHORT_ENFORCER, SHORT_FIELDS, STRUCTURE_ENFORCER, expand_short_forms  # noqa: F401
from .typebind import _type_env  # noqa: F401

# `from invariants.loader import *` exports these names and no submodules.
__all__ = ["DOOR_FIELDS", "REMOVED", "REMOVED_VALUES", "SHORT_ENFORCER", "SHORT_FIELDS",
           "STRUCTURE_ENFORCER", "YAML_SUFFIXES", "binding_problem", "closed_schema",
           "expand_short_forms", "literal_eq", "load", "log", "read_document", "schema_fields", "schema_name",
           "schema_path", "type_hints"]
