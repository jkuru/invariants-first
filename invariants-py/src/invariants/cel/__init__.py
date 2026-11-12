"""CEL adapter: the Backend protocol and backend selection.

A backend compiles rule text into a Program. A Program evaluates against an env of
Python values and reports the static references it reads. Backends must raise
CelSyntaxError at compile time and CelError at evaluation time.

Built-in backends: subset (stdlib, the default), cel-expr (cel-expr-python) and celpy
(cel-python). INV_CEL_BACKEND=subset|cel-expr|celpy|auto picks one; auto takes the first
installed of cel-expr, celpy, subset.
"""
from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any, Callable, Protocol, runtime_checkable

from . import subset
from ._engine import ENGINE_FUNCTIONS
from .errors import CelError, CelSyntaxError
from .subset import SubsetBackend

ENV_VAR = "INV_CEL_BACKEND"
DEFAULT = "subset"


@runtime_checkable
class Program(Protocol):
    """A compiled expression."""

    def evaluate(self, env: dict[str, Any]) -> Any:
        """Evaluate against env. Raise CelError on any evaluation error."""
        ...

    def references(self) -> frozenset[tuple[str, ...]]:
        """Free identifiers with their static field paths, e.g. ('args', 'total_jobs').

        A path stops at the first non-field step (index, call), so
        `old_self.sizes[k]` gives ('old_self', 'sizes'). Macro variables are not free.
        """
        ...


@runtime_checkable
class Backend(Protocol):
    """A CEL engine behind the adapter."""

    name: str

    def compile(self, expr: str) -> Program:
        """Compile expr. Raise CelSyntaxError if it does not compile."""
        ...


# Optional Backend attribute: functions, a frozenset of every function name the backend
# evaluates. The loader refuses a clause that calls another name. Without it, no name check.


# Optional Program method: negated_membership() -> (path, literals) | None, for
# `!(path in [literals])`. The loader uses it to report clauses held by a schema enum.

def _cel_expr() -> Backend:
    from .expr_python import ExprPythonBackend
    return ExprPythonBackend()


def _celpy() -> Backend:
    from .celpy_backend import CelpyBackend
    return CelpyBackend()


_FACTORIES: dict[str, Callable[[], Backend]] = {"subset": SubsetBackend, "cel-expr": _cel_expr, "celpy": _celpy}
_CACHE: dict[str, Backend] = {}
_LOCK = threading.Lock()
# The engine module each built-in backend needs; a backend whose module is missing is unavailable.
_REQUIRES = {"cel-expr": "cel_expr_python", "celpy": "celpy"}
AUTO_ORDER = ("cel-expr", "celpy", "subset")


def _importable(name: str) -> bool:
    mod = _REQUIRES.get(name)
    if mod is None:
        return True
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def resolve_name(name: str | None = None) -> str:
    """The backend name to use: name, else $INV_CEL_BACKEND, else 'subset'; 'auto' picks the
    first importable of cel-expr, celpy, subset."""
    key = name or os.environ.get(ENV_VAR) or DEFAULT
    if key == "auto":
        return next(n for n in AUTO_ORDER if n in _FACTORIES and _importable(n))
    return key


def register_backend(name: str, factory: Callable[[], Backend]) -> None:
    """Register a backend factory under name (replaces any earlier one)."""
    with _LOCK:
        _FACTORIES[name] = factory
        _CACHE.pop(name, None)
        _REQUIRES.pop(name, None)


def available_backends() -> list[str]:
    """Names of the registered backends whose engine is installed."""
    return sorted(n for n in _FACTORIES if _importable(n))


def get_backend(name: str | Backend | None = None) -> Backend:
    """Return a backend by name (subset, cel-expr, celpy, auto, or a registered one).

    None means $INV_CEL_BACKEND, else 'subset'.
    """
    if name is not None and not isinstance(name, str):
        return name
    key = resolve_name(name)
    with _LOCK:
        if key not in _FACTORIES:
            raise ValueError(f"unknown CEL backend {key!r}; choose from auto, {', '.join(sorted(_FACTORIES))}")
        if not _importable(key):
            raise ValueError(f"CEL backend {key!r} needs the {_REQUIRES[key]!r} package, which is not installed")
        if key not in _CACHE:
            _CACHE[key] = _FACTORIES[key]()
        return _CACHE[key]


__all__ = ["Backend", "Program", "CelError", "CelSyntaxError", "get_backend", "register_backend",
           "available_backends", "resolve_name", "SubsetBackend", "ENGINE_FUNCTIONS", "subset"]
