"""Finding the source files under the roots, minus exclude globs and the always-skipped directories."""
from __future__ import annotations

import fnmatch
import pathlib
from typing import Iterable, Iterator

from .contracts import SKIP_DIRS


def anywhere(*patterns: str) -> tuple[str, ...]:
    """Exclude globs that match at any depth: `tests` and `*/tests`, `test_*.py` and `*/test_*.py`."""
    return tuple(x for p in patterns for x in (p, f"*/{p}"))


def _excluded(rel: pathlib.PurePath, patterns: Iterable[str]) -> str | None:
    """The first glob a root-relative posix path (or any parent directory of it) matches, else None."""
    text = rel.as_posix()
    heads = [pathlib.PurePosixPath(*rel.parts[:i]).as_posix() for i in range(1, len(rel.parts))]
    return next((p for p in patterns
                 if fnmatch.fnmatch(text, p) or any(fnmatch.fnmatch(h, p.rstrip("/")) for h in heads)), None)


def iter_sources(roots: Iterable[pathlib.Path],
                 exclude: Iterable[str] = ()) -> Iterator[tuple[pathlib.Path, pathlib.Path, str]]:
    """(root, file, module name) for every .py file under the roots, minus exclude globs (root-relative)
    and the directories the scan always skips (SKIP_DIRS, dot-directories)."""
    for base, f, name, pattern, _ in _walk_sources(roots, exclude):
        if pattern is None:
            yield base, f, name


def _walk_sources(roots: Iterable[pathlib.Path],
                  exclude: Iterable[str]) -> Iterator[tuple[pathlib.Path, pathlib.Path, str, str | None, bool]]:
    """(root, file, module name, the exclude glob it matches or None, skipped) for every .py file under the
    roots. A file under a skipped directory (SKIP_DIRS, a dot-directory) has that directory's name as its
    pattern and skipped True."""
    exclude = list(exclude)
    roots = list(dict.fromkeys(r.resolve() for r in roots))
    dirs = [r for r in roots if r.is_dir()]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        base = root.parent if root.is_file() else root
        inner = [d for d in dirs if d != root and d.is_relative_to(root)]
        for f in files:
            if any(f.is_relative_to(d) for d in inner):
                continue                               # named from the deeper root that holds it
            rel = f.relative_to(base)
            skip = next((p for p in rel.parts[:-1] if p in SKIP_DIRS or p.startswith(".")), None)
            pattern = (_excluded(rel, exclude) if exclude else None) or skip
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                yield base, f, ".".join(parts), pattern, skip is not None
