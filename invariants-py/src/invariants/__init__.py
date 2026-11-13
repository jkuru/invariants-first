"""invariants: rule-file-driven guards for Python (the Python SDK of the Invariant-first design).

Explicit decorators only: no AST rewriting, no eval/exec of rule or source text.
"""

from importlib import metadata as _metadata

from . import cel
from .errors import (CoverageError, Escalation, Halted, InvariantError, LoadError, ParseError, Rejected,
                     ReviewNotOpened, Routed)
from .evaluate import evaluate, evaluate_env, fill_view
from .guard import Checked, edge, guard, model_call, parse
from .loader import load, read_document, schema_path
from .model import Outcome, Provider, Rule, effective_mode
from .operations import load_resource, monitor
from .providers import Dependency, provider
from .registry import (ErrorSpike, Handoff, JsonlReviews, MemoryReviews, Registry, ReviewStore, ablated, capture,
                       configure, current, default_registry, is_ablated, isolated, observe, standing, using,
                       verify, verify_coverage)
from .snapshot import check_snapshot

from . import _scan_hooks, registry as _registry   # the scan reads ablation through these hooks

_scan_hooks.ablated = lambda: _registry.env_ablated() | set(_registry.GLOBAL_ABLATED)
_scan_hooks.reached = lambda rule_id: _registry.note_ablation_reached(rule_id, via="scan")

try:
    __version__ = _metadata.version("invariants-first")
except _metadata.PackageNotFoundError:      # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"
CEL_BACKEND = "subset"     # the default backend name; see invariants.cel.get_backend

__all__ = [
    "provider", "Dependency", "guard", "model_call", "edge", "Checked", "parse",
    "load", "load_resource", "read_document", "schema_path", "verify", "verify_coverage", "standing",
    "check_snapshot", "evaluate", "evaluate_env", "fill_view", "effective_mode",
    "Registry", "current", "default_registry", "using", "isolated", "ablated", "is_ablated",
    "configure", "observe", "capture", "monitor",
    "Handoff", "ReviewStore", "MemoryReviews", "JsonlReviews", "ErrorSpike",
    "Outcome", "Rule", "Provider",
    "LoadError", "CoverageError", "ParseError", "InvariantError", "Rejected", "Halted", "Escalation", "Routed",
    "ReviewNotOpened", "cel", "CEL_BACKEND", "__version__",
]
