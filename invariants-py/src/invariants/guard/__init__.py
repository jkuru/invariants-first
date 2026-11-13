"""Decorators: @inv.guard, @inv.model_call, @inv.edge, and inv.Checked.

The package is split by seam; this module keeps the namespace the single guard.py had:
- checked.py: inv.Checked and the frozen containers (FrozenDict, FrozenList) its value lives in
- replies.py: inv.parse and the closed-schema field checks, the view a rule reads, the edge re-check
- plan.py: the call core shared by sync and async wrappers (_Call, _ModelRun, _EdgePlan)
- decorators.py: the boundary API (@inv.guard, @inv.model_call, @inv.edge)
"""
from __future__ import annotations

from .checked import (_DICTS, _FREEZE_DEPTH, _LISTS, Checked, FrozenDict, FrozenList, _freeze,  # noqa: F401
                      _immutable, _rebuild_checked, _slot, _thaw)
from .decorators import (C, C_co, ModelCallDecorator, P, R, _checked_type, _descriptor,  # noqa: F401
                         _edge_path, _hints, _refined, _state_fields, edge, guard, model_call)
from .plan import (_Call, _EdgeCall, _EdgePlan, _ModelRun, _get_field, _received, _set_path,  # noqa: F401
                   _with_field)
from .replies import (_ISO_DURATION, _MAX_DEPTH, _NON_FINITE, _REPR, _TEXT_FORMS, _TEXT_TYPES,  # noqa: F401
                      INSTANCE_ATTR, _build, _check, _err, _field_of, _finite, _from_text, _iso_duration,
                      _no_constant, _same, _show, _tname, _vt, check_schema, instance_class, parse,
                      parse_error_text, recheck, rule_view, supported_field_type)

# names guard.py imported from the rest of the library, kept so invariants.guard.<name> still resolves
from ..cel import CelError, values as cel_values  # noqa: F401
from ..errors import CoverageError, Escalation, ParseError  # noqa: F401
from ..evaluate import aact, ablated, act, evaluate, report  # noqa: F401
from ..loader import closed_schema, literal_eq, schema_fields, type_hints  # noqa: F401
from ..model import GUARD_POINTS, Outcome, Rule  # noqa: F401
from ..providers import ProviderFailed, prefetch_async, prefetch_many  # noqa: F401
from ..registry import WRAPPED, Handoff, Registry, _wraps, current, note_decision  # noqa: F401
from ..resolve import resolve  # noqa: F401
from ..snapshot import take_snapshot  # noqa: F401

# Classes keep the module name they had before the split: a pinned signature (`invariants.lock`) names
# an annotation's class by its module, and pickles name it too, so a moved __module__ would make an
# upgrade alone re-pin the lock and make new pickles unreadable by older versions.
for _obj in list(globals().values()):
    if isinstance(_obj, type) and (_obj.__module__ == __name__ or _obj.__module__.startswith(__name__ + ".")):
        _obj.__module__ = 'invariants.guard'
_rebuild_checked.__module__ = _freeze.__module__ = 'invariants.guard'
del _obj
