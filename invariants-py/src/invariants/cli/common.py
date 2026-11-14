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
                    and name != "invariants" and not name.startswith("invariants.")):
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
