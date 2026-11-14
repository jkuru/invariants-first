"""The shared pieces of `inv`: exit codes, the usage error, output, the rule file, captured runs."""
from __future__ import annotations

import argparse
import contextlib
import pathlib
import sys
from typing import Callable

import invariants_scan as scanner
from invariants_scan.text import safe_output

from .. import lock
from ..errors import LoadError

# the exit codes, the usage error and the --site directories live with the project (invariants.project),
# which the MCP server shares
from ..project import ABLATED, FAILED, OK, SITE, UNRESOLVED, USAGE, Usage  # noqa: F401

DEFAULT_FILES = ("invariants.yaml", "invariants.yml", "invariants.json")
ALLOW_WEAKENING_ENV = "INV_ALLOW_WEAKENING"


def _out(text: str) -> None:
    """Print a line (or lines) of `inv`'s own output; control characters and a line starting a
    GitHub Actions workflow command, which a file name or message from the tree can carry, are escaped."""
    print(safe_output(text), flush=True)


def _err(text: str) -> None:
    print(safe_output(text), file=sys.stderr)


def _rule_file(arg: str | None) -> pathlib.Path:
    if arg:
        return pathlib.Path(arg)
    for name in DEFAULT_FILES:
        if pathlib.Path(name).is_file():
            return pathlib.Path(name)
    raise Usage(f"no rule file given and none of {', '.join(DEFAULT_FILES)} in the current directory")


# the library's own top-level packages: invariants and the three libraries it is built on
_LIBRARY = frozenset({"invariants", "invariants_cel", "invariants_scan", "invariants_schema_diff"})


def _importing(e: BaseException) -> tuple[str, bool] | None:
    """The app module whose import raised e (or what e was raised from), innermost first, and whether
    that module's own top-level code called inv.load(); None when e did not come from importing one."""
    seen: set[int] = set()
    exc: BaseException | None = e
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        tb, found = exc.__traceback__, None
        while tb is not None:
            f = tb.tb_frame
            name = f.f_globals.get("__name__") or ""
            if (f.f_code.co_name == "<module>" and name not in ("__main__", "")
                    and name.partition(".")[0] not in _LIBRARY):
                callee = tb.tb_next.tb_frame if tb.tb_next is not None else None
                found = (name, callee is not None and callee.f_code.co_name == "load"
                         and str(callee.f_globals.get("__name__", "")).startswith("invariants."))
            tb = tb.tb_next
        if found:
            return found
        exc = exc.__cause__ or exc.__context__
    return None


def _describe(e: BaseException, typed: bool = False) -> str:
    """An error's message (with its type when typed), and, when it was raised while importing an app
    module, which one: an error in the app's own import is otherwise read as one in the rule file."""
    text = f"{type(e).__name__}: {e}" if typed else str(e)
    where = _importing(e)
    if where is None or f"import {where[0]}:" in text or f"while importing {where[0]})" in text:
        return text
    mod, loads = where
    text += f" (raised while importing {mod}"
    if loads:
        text += (f": its inv.load() runs as `inv` imports it, and a relative path is read from the current "
                 f"directory; give an absolute one, e.g. "
                 f'inv.load(pathlib.Path(__file__).parent / "invariants.yaml")')
    return text + ")"


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
            print(_describe(e))
            code = FAILED
        except Exception as e:                       # an app module failing at import, for example
            print(_describe(e, typed=True))
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
