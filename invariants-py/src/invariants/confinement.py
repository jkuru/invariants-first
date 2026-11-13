"""Confining the files a rule file names (contract refs, enforcer artifacts) to one directory.

`inv mcp` runs `inv check` and `inv status` with INV_CONFINE_ROOT set to its --root: a rule file an
agent can edit must not make the tool read (and echo, in a lock diff or an error) a file outside the
directory the person who started the server pointed it at. Plain `inv` leaves it unset and reads a ref
wherever the rule file points."""
from __future__ import annotations

import os
import pathlib

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
