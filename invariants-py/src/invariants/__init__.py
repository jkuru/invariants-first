"""invariants: rule-file-driven guards for Python (the Python SDK of the Invariant-first design).

Explicit decorators only: no AST rewriting, no eval/exec of rule or source text.
"""

import sys as _sys


def _drop_cwd_for_inv() -> None:
    """`python -m invariants.cli` (or invariants.mcp) puts the working directory first on sys.path, so a
    project's own yaml.py, tomllib.py or json.py would be imported by the process that decides the
    verdict. Drop it, as `python -P` does, before anything else is imported. Only while runpy imports
    this package for one of those two commands; an app that imports invariants keeps its sys.path."""
    import os
    argv = list(getattr(_sys, "orig_argv", []))
    target = next((argv[i + 1] if a == "-m" and i + 1 < len(argv) else a[2:]
                   for i, a in enumerate(argv[1:], 1) if a == "-m" or (a.startswith("-m") and len(a) > 2)), None)
    if _sys.argv[:1] != ["-m"] or _sys.flags.safe_path or target not in ("invariants.cli", "invariants.mcp"):
        return
    try:
        here = os.getcwd()
    except OSError:
        here = None
    if _sys.path and _sys.path[0] in ("", here):
        del _sys.path[0]


_drop_cwd_for_inv()

from importlib import metadata as _metadata  # noqa: E402

from . import cel
from .errors import (BypassError, CoverageError, Escalation, Halted, InvariantError, LoadError, ParseError, Rejected,
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

# The layers below the package import nothing above them; it wires what they read from above here.
from invariants_scan import hooks as _scan_hooks  # noqa: E402

from . import confinement as _confinement, registry as _registry
from .loader import binding_problem as _binding_problem, schemas as _schemas
from .registry import coverage as _coverage

# the scan reads ablation and the confinement root through these hooks
_scan_hooks.ablated = lambda: _registry.env_ablated() | set(_registry.GLOBAL_ABLATED)
_scan_hooks.reached = lambda rule_id: _registry.note_ablation_reached(rule_id, via="scan")
_scan_hooks.confine_root = _confinement.confine_root
# verify_coverage checks a guard's bindings with the loader's check
_coverage.binding_problem = _binding_problem


def _pydantic_closed(model: type) -> type:
    """The loader closes a pydantic schema with the pydantic integration, imported only when one is met."""
    from .integrations.pydantic import closed
    return closed(model)


_schemas.pydantic_closed = _pydantic_closed

# inv.doors: `invariants.doors` is the module of `inv init` (finding doors in the source), and it
# re-exports the run-time door check (doors_runtime), so `inv.doors.install(...)` type-checks.
from . import doors  # noqa: E402

try:
    __version__ = _metadata.version("invariants-first")
except _metadata.PackageNotFoundError:      # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"
CEL_BACKEND = "subset"     # the default backend name; see invariants.cel.get_backend (invariants_cel)

__all__ = [
    "provider", "Dependency", "guard", "model_call", "edge", "Checked", "parse",
    "load", "load_resource", "read_document", "schema_path", "verify", "verify_coverage", "standing",
    "check_snapshot", "evaluate", "evaluate_env", "fill_view", "effective_mode",
    "Registry", "current", "default_registry", "using", "isolated", "ablated", "is_ablated",
    "configure", "observe", "capture", "monitor",
    "Handoff", "ReviewStore", "MemoryReviews", "JsonlReviews", "ErrorSpike",
    "Outcome", "Rule", "Provider",
    "LoadError", "CoverageError", "ParseError", "InvariantError", "Rejected", "Halted", "Escalation", "Routed",
    "ReviewNotOpened", "BypassError", "cel", "CEL_BACKEND", "__version__",
]
