"""Load and validate a rule file (JSON or YAML, a path or a dict). Every defect is a LoadError.

The package is split by seam; this module keeps the namespace the single loader.py had:
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

from .loading import _copied, _load_contracts, _load_provider, _load_rules, _load_targets, load, log  # noqa: F401
from .paths import _check_scopes, _read_paths, _scope_problem, _snapshot_paths, binding_problem  # noqa: F401
from .policy import _abstain_in_schema, _abstain_shape, _load_policy, _policy_shape, _restates_enum  # noqa: F401
from .reading import YAML_SUFFIXES, read_document, schema_path  # noqa: F401
from .rules import (_bad_clauses, _bind_target, _calls, _check_artifact, _check_bindings,  # noqa: F401
                    _check_functions, _check_provider_args, _check_rule_fields, _compile_rule,
                    _compute_max_status, _load_enforcer, _load_rule, _names_map)
from .schemas import (_BUILTIN_TYPES, _dataclass_fields, _params, _parse_type, _type_set,  # noqa: F401
                      closed_schema, literal_eq, schema_fields, schema_name, type_hints)
from .shapes import REMOVED, REMOVED_VALUES, _check_fields, _check_mediation, _check_path, _strings  # noqa: F401
from .short_forms import (DOOR_FIELDS, SHORT_ENFORCER, SHORT_FIELDS, STRUCTURE_ENFORCER,  # noqa: F401
                          _expand_door, _expand_guard, _expand_tests, _py_list, expand_short_forms)
from .typebind import _schema_type, _type_env, _typecheck  # noqa: F401

# names loader.py imported from the rest of the library, kept so invariants.loader.<name> still resolves
from typing import Any, Iterator  # noqa: F401
from .. import cel, typecheck as tc  # noqa: F401
from ..errors import LoadError  # noqa: F401
from ..model import (DEFAULT_NAMES, ENFORCER_FIELDS, ENUMS, EVALUATED, FILE_FIELDS, MEDIATION_FIELDS,  # noqa: F401
                     PATH_ROOTS, POLICY_FIELDS, PROVIDER_FIELDS, PROVIDER_OPTIONAL, RULE_FIELDS, STATUS_RANK,
                     TARGET_FIELDS, VIEW_POINTS, Provider, Rule)
from ..providers import provider_names  # noqa: F401
from ..registry import ABLATE_ENV, Registry, ablation_level, current, env_ablated  # noqa: F401
from ..resolve import resolve  # noqa: F401

# `from invariants.loader import *` exports exactly the public names loader.py's star import gave (it had
# no __all__), and no modules: without this list it would bind the submodules over a caller's own
# names. The modules loader.py leaked (cel, copy, dataclasses, enum, functools, hashlib, inspect, json,
# logging, pathlib, sys, tc, types, typing) are not exported.
__all__ = ["ABLATE_ENV", "Any", "DEFAULT_NAMES", "DOOR_FIELDS", "ENFORCER_FIELDS", "ENUMS", "EVALUATED",
           "FILE_FIELDS", "Iterator", "LoadError", "MEDIATION_FIELDS", "PATH_ROOTS", "POLICY_FIELDS",
           "PROVIDER_FIELDS", "PROVIDER_OPTIONAL", "Provider", "REMOVED", "REMOVED_VALUES", "RULE_FIELDS",
           "Registry", "Rule", "SHORT_ENFORCER", "SHORT_FIELDS", "STATUS_RANK", "STRUCTURE_ENFORCER",
           "TARGET_FIELDS", "VIEW_POINTS", "YAML_SUFFIXES", "ablation_level", "annotations",
           "binding_problem", "closed_schema", "current", "env_ablated", "expand_short_forms", "literal_eq",
           "load", "log", "provider_names", "read_document", "resolve", "schema_fields", "schema_name",
           "schema_path", "type_hints"]
