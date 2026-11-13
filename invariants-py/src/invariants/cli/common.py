"""The shared pieces of `inv`: exit codes, the usage error, output, the rule file, captured runs."""
from __future__ import annotations

import argparse
import contextlib
import pathlib
import sys
from typing import Callable

from .. import lock, scan as scanner
from ..errors import LoadError

OK, FAILED, USAGE, UNRESOLVED, ABLATED = 0, 1, 2, 3, 4
DEFAULT_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")
ALLOW_WEAKENING_ENV = "INV_ALLOW_WEAKENING"


class Usage(Exception):
    """A usage error (exit 2)."""


def _out(text: str) -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _rule_file(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg)
    for name in DEFAULT_FILES:
        if pathlib.Path(name).is_file():
            return pathlib.Path(name)
    raise Usage(f"no rule file given and none of {', '.join(DEFAULT_FILES)} in the current directory")


def _captured(fn: Callable[[argparse.Namespace], int], ns: argparse.Namespace) -> tuple[int, str]:
    """Run a subcommand with its output captured; LoadError and the like become exit 1 with the message."""
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = fn(ns)
        except Usage as e:
            print(e)
            code = USAGE
        except (LoadError, scanner.ScanConfigError) as e:
            print(e)
            code = FAILED
        except Exception as e:                       # an app module failing at import, for example
            print(f"{type(e).__name__}: {e}")
            code = FAILED
    return code, buf.getvalue().strip()


def _print_changes(changes: list[lock.Change], err: bool = False) -> None:
    emit = _err if err else _out
    for c in changes:
        emit(f"  {c}")


def _compare(fn: Callable[[], list[lock.Change]], lock_path: pathlib.Path) -> list[lock.Change]:
    """Run a lock comparison; a stored lock malformed below the shapes read() checks is a LockError."""
    try:
        return fn()
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        raise lock.LockError(f"{lock_path}: malformed lock: cannot compare it with the rule file "
                                 f"({type(e).__name__}: {e}); re-create it with `inv lock`") from e
