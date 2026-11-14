"""Resolve 'py:module.attr' target strings to objects."""
from __future__ import annotations

import difflib
import importlib
import pkgutil
from types import TracebackType
from typing import Any, Iterable

from .errors import LoadError

MAX_LISTED = 12


def resolve(target: str, what: str = "target") -> Any:
    """'py:pkg.mod.Class.method' -> the object, or LoadError. what: how the error names the reference
    ("target", or "provider NAME: from" for a provider's `from:`)."""
    if not str(target).startswith("py:"):
        raise LoadError(f"unsupported {what} {target!r}")
    parts = target[3:].split(".")
    if not all(p.isidentifier() for p in parts):
        raise LoadError(f"{what} {target!r}: not a dotted Python name (py:package.module.function)")
    for i in range(len(parts), 0, -1):
        name = ".".join(parts[:i])
        try:
            obj = importlib.import_module(name)
            break
        except ModuleNotFoundError as e:
            if _is_this_module(e, name):
                continue
            outer, inner = _importing(e.__traceback__)
            mod = outer or name
            raise LoadError(f"cannot import {mod}: ModuleNotFoundError: {e}"
                            + (f" (while importing {inner})" if inner and inner != mod else "")) from e
    else:
        raise LoadError(f"{what} {target}: no importable module {parts[0]!r}")
    have = name
    for p in parts[i:]:
        if not hasattr(obj, p):
            raise LoadError(f"{what} {target}: {have} has no {p!r}{suggest(p, names_in(obj))}")
        obj = getattr(obj, p)
        have = f"{have}.{p}"
    return obj


def _is_this_module(e: ModuleNotFoundError, name: str) -> bool:
    """True when the module not found is `name` itself or a package on its path (the target names a
    module that does not exist); False when importing `name` failed on a module it imports."""
    missing = getattr(e, "name", None)
    return missing is None or missing == name or name.startswith(missing + ".")


def _importing(tb: TracebackType | None) -> tuple[str | None, str | None]:
    """The outermost and innermost app modules whose top-level code was running when an import failed:
    the module resolve() was importing, and the one whose own import statement failed."""
    found: list[str] = []
    while tb is not None:
        f = tb.tb_frame
        mod = f.f_globals.get("__name__") or ""
        if f.f_code.co_name == "<module>" and mod and not mod.startswith(("importlib", "invariants", "_frozen")):
            found.append(mod)
        tb = tb.tb_next
    return (found[0], found[-1]) if found else (None, None)


def names_in(obj: Any) -> list[str]:
    """The public names an object has: its attributes, and a package's submodules (not imported yet)."""
    out = {n for n in dir(obj) if not n.startswith("_")}
    path = getattr(obj, "__path__", None)
    if path is not None:
        try:
            out |= {m.name for m in pkgutil.iter_modules(path) if not m.name.startswith("_")}
        except Exception:                                # an odd namespace path: attributes only
            pass
    return sorted(out)


def edit_distance(a: str, b: str) -> int:
    """Edits from a to b (insert, delete, substitute, or swap two neighbours: tset -> tests is 2)."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def close_matches(word: str, names: Iterable[str], n: int = 3) -> list[str]:
    """The names closest to word: a near spelling (difflib) or a few typed edits away (a swap counts
    as one), closest first."""
    pool = sorted({x for x in names if isinstance(x, str)})
    near = set(difflib.get_close_matches(word, pool, n=n, cutoff=0.6))
    limit = 2 if len(word) >= 4 else 1 if len(word) == 3 else 0
    near |= {x for x in pool if edit_distance(word.lower(), x.lower()) <= limit}
    near.discard(word)
    return sorted(near, key=lambda x: (edit_distance(word.lower(), x.lower()),
                                       -difflib.SequenceMatcher(None, word, x).ratio(), x))[:n]


def suggest(word: str, names: Iterable[str], listed: bool = True) -> str:
    """'; did you mean 'x'?' when a name is close, and (listed) the names there are."""
    names = sorted(set(names))
    close = close_matches(word, names)
    text = f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
    if listed and names and not close:
        shown = ", ".join(names[:MAX_LISTED]) + (f", ... ({len(names) - MAX_LISTED} more)"
                                                 if len(names) > MAX_LISTED else "")
        text += f" (it has: {shown})"
    return text
