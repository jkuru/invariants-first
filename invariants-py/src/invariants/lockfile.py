"""`invariants.lockfile`: the old name of the `invariants.lock` package, kept so existing imports work.

Every name this module defined before the lock was split into stages, public and private, is
re-exported from the stage that now holds it: lock.contracts, lock.pinning, lock.schema_diff,
lock.cel_mono, lock.rule_diff and lock (the orchestrator). They are the same objects, so patching a
name here does not change what the stages read: patch the stage module. New code imports
`invariants.lock`.
"""
from __future__ import annotations

from .errors import LoadError
from .lock import (DUMP_WIDTH, _ENTRY_SECTIONS, _ENTRY_SHAPES, _V3_ONLY, LockError, LockVersionError, _comparable,
                   _contract_users, _dump, _refs_in, changes, check_version, diff, dumps, migrate_v1, migrate_v2,
                   read, schema_problems, validate, without_damaged_schemas)
from .lock.cel_mono import (_BOOL_CALLS, _BOOL_OPS, _CMP, _FALSE, _INT64, _INTEGRAL, _MACRO_CALLS, _MIRROR, _NEGATE,
                            _OP_STRENGTH, _TOTAL_CALLS, Typer, _Canon, _clause_key, _describe_site, _disjuncts,
                            _error_sites, _int_lit, _lit, _magnitude, _mask, _new_sites_text, _nonneg, _num,
                            _numeric, _order, _unique, _walk, canon_tree, clause_types, compare_bad, make_typer,
                            new_error_sites)
from .lock.contracts import (ANALYSED, COMPENSABLE_RANK, COMPUTED, DEFAULT_SCAN_EXCLUDE, DOC_FIELDS, EFFECTS_RANK,
                             ENFORCER_DEFAULTS, ERROR_RANK, FEEDBACK_DEFAULT, FIELD_POLICY, LOCK_NAME, LOCK_VERSION,
                             MAX_DEFS, MAX_DEPTH, MEDIATION_DOC, MODE_RANK, MORE_IS_WEAKER, NEUTRAL, NOTES, PIN,
                             PRESENCE, PROSE, PY_CONTRACTS, RANKED, RANKS, REMOVED_NOTE, SCHEMA_CONTRACTS,
                             STATUS_RANK, TIMING_RANK, VIOLATION_RANK, WEAKENING, Change, _is_ref, _json_paths,
                             _plain, _sha, _short, canonical, policy, same)
from .lock.pinning import (_ARRAYS, _BOUND_ATTRS, _DATA_KEYS, _JSON_TYPES, _LEN_ATTRS, _MAPS, _NAME_MAPS, _PARAM_KEYS,
                           _SHARED_SCHEMAS, SCHEMA_PROSE, Schemas, _annotated_bounds, _annotation_schema, _class_ref,
                           _constrains, _doc_digests, _has_constraints, _is_pydantic, _is_schema_class,
                           _is_typeddict, _json_object, _json_type, _local_def, _openapi_app, _openapi_app_pin,
                           _openapi_file, _openapi_operations, _operation_ids, _provider_pin, _python_ref,
                           _signature_pin, _type_schema, build, contract_hash, expand_applies_to, json_schema, mediated,
                           normalize_semantic, param_schemas, resolved_schema, rule_hash, rule_semantics, scan_pin,
                           signature_types, stdlib_json_schema, strip_prose)
from .lock.rule_diff import (_bad_changes, _clauses, _direction_change, _enforcer_key, _entry_changes, _field_change,
                             _keyed, _label, _mediation_changes, _model_policy_changes, _named_operations,
                             _operation_renames, _rule_changes, _scan_changes, _signature_shape)
from .lock.schema_diff import (_FLIPPED, _NAME_SEGMENTS, _PIN_ARRAYS, _PIN_MAPS, _PIN_SCALARS, _TWIN_BOUNDS,
                               EXACT_CONSTRAINTS, JUDGED_SCHEMAS, LOWER_BOUNDS, STRUCTURAL, UPPER_BOUNDS, _anchors,
                               _def_removed, _local_ref, _local_refs, _node, _number, _opens_closed,
                               _param_schema_changes, _pattern_narrower, _pin_def, _pin_type, _property_opens,
                               _ref_changed, _required_weakening, _schema_changes, _twin_tightens, _type_weakening,
                               _types, constraint_weakening, pin_json_schema)
from .resolve import resolve
from .scan import anywhere
