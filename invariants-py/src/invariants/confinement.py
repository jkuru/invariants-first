"""Confining the files a rule file names (contract refs, enforcer artifacts) and the modules it makes
the tool import (targets, contracts, providers) to one directory.

`inv mcp` runs `inv check` and `inv status` with INV_CONFINE_ROOT set to its --root: a rule file an
agent can edit must not make the tool read (and echo, in a lock diff or an error) a file outside the
directory the person who started the server pointed it at, nor import (and so run, and pin or echo an
object of) a module outside it. Plain `inv` leaves it unset: it reads a ref wherever the rule file
points and imports any module the rule file names."""
from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
from typing import Any

from .errors import LoadError

CONFINE_ENV = "INV_CONFINE_ROOT"


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
    installed packages are outside. A module that is not found raises ModuleNotFoundError, as
    import_module does; one found outside raises LoadError."""
    root = os.environ.get(CONFINE_ENV)
    if root:
        _check_module(name, what, pathlib.Path(root).resolve())
    return importlib.import_module(name)


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
    places = [*([spec.origin] if spec.has_location and spec.origin else []),
              *(spec.submodule_search_locations or [])]
    if not places or not all(pathlib.Path(p).resolve().is_relative_to(top) for p in places):
        raise refused
