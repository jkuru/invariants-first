"""Confining the files a rule file names (contract refs, enforcer artifacts) and the lock, the modules it makes
the tool import (targets, contracts, providers) and the files the scan reads to one directory.

`inv mcp` runs `inv check` and `inv status` with INV_CONFINE_ROOT set to its --root: a rule file an
agent can edit must not make the tool read (and echo, in a lock diff or an error) a file outside the
directory the person who started the server pointed it at, nor import (and so run, and pin or echo an
object of) a module outside it. Its in-process tools (scan, explain, propose_rules) run the scan
inside `confining(root)`, which makes the walk of the source roots stop at a symlink out of it. Plain
`inv` leaves both unset: it reads a ref wherever the rule file points, imports any module the rule
file names, and follows every symlink under the source roots."""
from __future__ import annotations

import contextlib
import contextvars
import functools
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import pathlib
import sys
from typing import Any, Iterator, Sequence

from .errors import LoadError

CONFINE_ENV = "INV_CONFINE_ROOT"
_SCAN_ROOT: contextvars.ContextVar[pathlib.Path | None] = contextvars.ContextVar("inv_scan_root", default=None)


def confine_root() -> pathlib.Path | None:
    """The directory reads are confined to, resolved: INV_CONFINE_ROOT, else the root of an enclosing
    confining() block, else None (not confined)."""
    root = os.environ.get(CONFINE_ENV)
    if root:
        return pathlib.Path(root).resolve()
    return _SCAN_ROOT.get()


@contextlib.contextmanager
def confining(root: pathlib.Path) -> Iterator[None]:
    """Within the block, confine_root() is root (resolved): `inv mcp` runs its in-process scans in one."""
    token = _SCAN_ROOT.set(root.resolve())
    try:
        yield
    finally:
        _SCAN_ROOT.reset(token)


def inside(path: str | os.PathLike[str], top: pathlib.Path) -> bool:
    """path, with every symlink on it followed, lies in top (itself resolved)."""
    return pathlib.Path(os.path.realpath(path)).is_relative_to(top)


def confined(path: pathlib.Path, what: str) -> pathlib.Path:
    """path, unchanged; LoadError when INV_CONFINE_ROOT is set and path resolves (symlinks followed)
    outside it. Checked before the file is opened, so nothing of an outside file is read."""
    root = os.environ.get(CONFINE_ENV)
    if not root:
        return path
    top = pathlib.Path(root).resolve()
    if not path.resolve().is_relative_to(top):
        raise LoadError(f"{what} resolves outside {top}, the --root `inv mcp` confines reads to; "
                        f"not read (start the server with a wider --root to reach it)")
    return path


def import_module(name: str, what: str) -> Any:
    """importlib.import_module(name). When INV_CONFINE_ROOT is set, the top-level package of name must
    be found as a file (a namespace package: only directories) inside it, checked with
    importlib.util.find_spec before anything is imported: an outside module's import-time code never
    runs and its objects (os.environ, say) never reach a pin or an error. The standard library and
    installed packages are outside. Every module of the project imported after that (name's own
    submodules, and whatever the project's code imports) is checked the same way by the finder
    install() puts first on sys.meta_path, before its code runs, and every prefix of name already in
    sys.modules (which no finder sees again) must have been loaded from inside it: a symlink inside the root to a
    directory or file outside it (`app/leak -> ../../vault`) is refused, not followed. A module that is
    not found raises ModuleNotFoundError, as import_module does; one found outside raises LoadError."""
    root = os.environ.get(CONFINE_ENV)
    if not root:
        return importlib.import_module(name)
    install()
    top = pathlib.Path(root).resolve()
    _check_module(name, what, top)
    mod = importlib.import_module(name)
    _check_loaded(name, what, top)          # what the project's import-time code put in sys.modules
    return mod


def _check_module(name: str, what: str, top: pathlib.Path) -> None:
    head = name.split(".")[0]
    refused = LoadError(f"{what}: module {head} is not a file inside {top}, the --root `inv mcp` confines "
                        f"imports to (the standard library and installed packages are outside it); not "
                        f"imported (start the server with a wider --root, or run `inv check` yourself)")
    if not head.isidentifier():
        raise refused
    try:
        spec = importlib.util.find_spec(head)
    except (ImportError, ValueError) as e:
        raise refused from e
    if spec is None:
        raise ModuleNotFoundError(f"No module named {head!r}", name=head)
    places = _places(spec)
    if not places or not all(inside(p, top) for p in places):
        raise refused
    _check_loaded(name, what, top)


def _check_loaded(name: str, what: str, top: pathlib.Path) -> None:
    """Every dotted prefix of name that sys.modules holds was loaded from inside top: ConfinedFinder
    never sees a module already there (imported before, or put there by the project's own code)."""
    parts = name.split(".")
    for n in range(1, len(parts) + 1):
        prefix = ".".join(parts[:n])
        mod = sys.modules.get(prefix)
        if mod is not None and not _loaded_inside(mod, top):
            raise LoadError(f"{what}: module {prefix} is not a file inside {top}, the --root `inv mcp` confines "
                            f"imports to (it was loaded from outside it); not imported (start the server with "
                            f"a wider --root, or run `inv check` yourself)")


def _loaded_inside(mod: Any, top: pathlib.Path) -> bool:
    """An imported module (sys.modules holds it, under whatever name) was loaded from inside top: its
    file, or every directory of a namespace package, resolves there."""
    f = getattr(mod, "__file__", None)
    if isinstance(f, str):
        return inside(f, top)
    spec = getattr(mod, "__spec__", None)
    places = _places(spec) if isinstance(spec, importlib.machinery.ModuleSpec) else []
    return bool(places) and all(inside(p, top) for p in places)


def _places(spec: importlib.machinery.ModuleSpec) -> list[str]:
    """The files and directories a module spec loads from: its origin (a file), its package path."""
    return [*([spec.origin] if spec.has_location and spec.origin else []),
            *(spec.submodule_search_locations or [])]


@functools.lru_cache(maxsize=4096)
def _real(path: str) -> str:
    return os.path.realpath(path)


def _through(path: str, top: pathlib.Path) -> bool:
    """Some directory on the way to path (path made absolute, not resolved) resolves inside top: path
    is reached from the root, whatever it resolves to at the end."""
    p = pathlib.Path(os.path.abspath(path))
    t = str(top)
    return any(_real(str(a)) == t or _real(str(a)).startswith(t + os.sep) for a in p.parents)


class ConfinedFinder(importlib.abc.MetaPathFinder):
    """While INV_CONFINE_ROOT is set, the first finder on sys.meta_path: it asks the finders after it for
    a module's spec and refuses (LoadError, before the module's code runs) one of the project's modules
    that resolves outside the root. A module is the project's when a directory on the way to it lies in
    the root (a symlink `app/leak` inside the root, whatever it points to), or when it is a submodule of
    a top-level package found inside the root (one a package's __path__ leads elsewhere). The standard
    library and installed packages are reached from outside the root and load as before."""

    def find_spec(self, fullname: str, path: Sequence[str] | None = None,
                  target: Any = None) -> importlib.machinery.ModuleSpec | None:
        root = os.environ.get(CONFINE_ENV)
        if not root:
            return None
        spec = None
        for finder in list(sys.meta_path):
            if isinstance(finder, ConfinedFinder):
                continue
            find = getattr(finder, "find_spec", None)
            if find is not None and (spec := find(fullname, path, target)) is not None:
                break
        if spec is None:
            return None
        top = pathlib.Path(root).resolve()
        out = [p for p in _places(spec) if not inside(p, top)]
        if out and (any(_through(p, top) for p in out) or _project_package(fullname, top)):
            raise LoadError(f"module {fullname} resolves outside {top}, the --root `inv mcp` confines imports "
                            f"to (a symlink, or a package path, inside the root leads out of it); not imported "
                            f"(start the server with a wider --root, or run `inv check` yourself)")
        return spec


def _project_package(fullname: str, top: pathlib.Path) -> bool:
    """fullname is a submodule of a top-level package of the project: a regular package whose
    __init__.py is inside top (its __path__, which the package's code may extend, does not decide it),
    or a namespace package (PEP 420: no __init__.py, no origin) with a portion inside top. A portion
    found later elsewhere on sys.path does not make a namespace package's submodules someone else's."""
    head, dot, _ = fullname.partition(".")
    if not dot:
        return False
    spec = getattr(sys.modules.get(head), "__spec__", None)
    if spec is None:
        return False
    if spec.has_location and spec.origin:
        return inside(spec.origin, top)
    return any(inside(p, top) for p in list(spec.submodule_search_locations or []))


def install() -> None:
    """Put ConfinedFinder first on sys.meta_path (once); it acts only while INV_CONFINE_ROOT is set."""
    if not any(isinstance(f, ConfinedFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, ConfinedFinder())


if os.environ.get(CONFINE_ENV):
    install()
