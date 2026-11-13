"""Finding the source files under the roots, minus exclude globs and the always-skipped directories.

A file is indexed once, named from the deepest root that holds it (`src/app/x.py` is `app.x` with the
roots `src` and `.`); the name every other root gives it (`src.app.x`) is an alias of the same module.
It is left out of the scan only when, from every root that holds it, an exclude glob matches its path
or it lies in an always-skipped directory: adding a root never excludes more. In one directory, what
Python imports wins (a package `ops/` over an extension module `ops.so` over `ops.py` over a sourceless
`ops.pyc`); the code it shadows is noted. A module with no Python source (an extension module, a
sourceless `.pyc`) cannot be read; it is noted too, never scanned as empty."""
from __future__ import annotations

import dataclasses
import fnmatch
import pathlib
from typing import Iterable, Iterator

from .contracts import SKIP_DIRS

# the directories the scan always skips, as the lock pins them: SKIP_DIRS and every dot-directory
SKIPPED = (".*", *sorted(SKIP_DIRS))
_NO_SOURCE = (".so", ".pyd", ".pyc")             # importable modules the scan cannot read
_RANK = {"package": 0, "extension": 1, "source": 2, "bytecode": 3}   # what Python imports first, per directory


def anywhere(*patterns: str) -> tuple[str, ...]:
    """Exclude globs that match at any depth: `tests` and `*/tests`, `test_*.py` and `*/test_*.py`."""
    return tuple(x for p in patterns for x in (p, f"*/{p}"))


def _excluded(rel: pathlib.PurePath, patterns: Iterable[str]) -> str | None:
    """The first glob a root-relative posix path (or any parent directory of it) matches, else None."""
    text = rel.as_posix()
    heads = [pathlib.PurePosixPath(*rel.parts[:i]).as_posix() for i in range(1, len(rel.parts))]
    return next((p for p in patterns
                 if fnmatch.fnmatch(text, p) or any(fnmatch.fnmatch(h, p.rstrip("/")) for h in heads)), None)


def _skipped(rel: pathlib.PurePath) -> str | None:
    """The always-skipped directory (SKIP_DIRS, a dot-directory) a root-relative path lies in, else None."""
    return next((p for p in rel.parts[:-1] if p in SKIP_DIRS or p.startswith(".")), None)


@dataclasses.dataclass
class _Source:
    """One importable file, named from its deepest root."""

    root: pathlib.Path             # the directory its name is relative to
    file: pathlib.Path
    name: str
    pattern: str | None            # the exclude glob or skipped directory that leaves it out, else None
    skip: bool                     # it lies in an always-skipped directory (from its naming root)
    form: str                      # package | extension | source | bytecode
    aliases: list[str]             # the names the other roots holding it give it
    long: str                      # its name from the shallowest root that holds it (relative imports)
    index: int                     # its naming root's place in the root order
    partial: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # (glob, root): from some roots
    globbed: bool = False          # an exclude glob matches it (from its naming root)
    shadowed_by: _Source | None = None   # the module Python imports under this name instead


def _form(f: pathlib.Path) -> tuple[str, str] | None:
    """(module file stem, form) of an importable file: `x.py`, `x.pyc`, `x.so`, `x.cpython-312-darwin.so`."""
    if f.suffix == ".py":
        return f.stem, "source"
    if f.suffix in _NO_SOURCE:
        stem = f.name.split(".")[0]
        if f.suffix == ".pyc" and f.name != f"{stem}.pyc":
            return None                               # x.cpython-312.pyc is a cache entry, not importable
        return (stem, "extension" if f.suffix != ".pyc" else "bytecode") if stem.isidentifier() else None
    return None


def _name(rel: pathlib.PurePath, stem: str) -> list[str]:
    parts = [*rel.parts[:-1], stem]
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _files(root: pathlib.Path) -> list[pathlib.Path]:
    if root.is_file():
        return [root]
    return sorted(f for pat in ("*.py", *(f"*{s}" for s in _NO_SOURCE)) for f in root.rglob(pat) if f.is_file())


def _layout(roots: Iterable[pathlib.Path], exclude: Iterable[str]) -> list[_Source]:
    """Every importable file under the roots, once each, in root order then path order."""
    exclude = list(exclude)
    roots = list(dict.fromkeys(r.resolve() for r in roots))
    held: dict[pathlib.Path, list[tuple[pathlib.Path, pathlib.PurePath]]] = {}
    order: list[tuple[pathlib.Path, pathlib.Path]] = []
    for root in roots:
        base = root.parent if root.is_file() else root
        for f in _files(root):
            if _form(f) is None:
                continue
            if f not in held:
                held[f] = []
            held[f].append((base, f.relative_to(base)))
            order.append((base, f))
    out: list[_Source] = []
    done: set[pathlib.Path] = set()
    rank = {b: i for i, b in enumerate(dict.fromkeys(b for b, _ in order))}
    for base, f in order:
        stem, form = _form(f) or ("", "")
        # the deepest root names it (a root's own __init__.py is named from the next root up)
        cands = [c for c in held[f] if _name(c[1], stem)]
        if not cands:
            continue
        name_base, rel = max(cands, key=lambda c: len(c[0].parts))
        if base != name_base or f in done:
            continue
        done.add(f)
        parts = _name(rel, stem)
        if stem == "__init__" and form == "source":
            form = "package"
        reasons = [(_excluded(r, exclude) if exclude else None, _skipped(r), b) for b, r in cands]
        glob, skip = next((g, s) for g, s, b in reasons if b == name_base)
        # left out only when every root that holds it leaves it out: adding a root never excludes more
        out_everywhere = all(g or s for g, s, _ in reasons)
        pattern = (glob or skip) if out_everywhere else None
        partial = [(g, str(b)) for g, s, b in reasons if g and not out_everywhere]
        named = {b: ".".join(p) for b, r in cands if (p := _name(r, stem)) and all(x.isidentifier() for x in p)}
        aliases = [n for b, n in named.items() if b != name_base]
        # relative imports climb directories: from the shallowest root that gives the file a module name
        long = named[min(named, key=lambda b: len(b.parts))] if named else ".".join(parts)
        out.append(_Source(name_base, f, ".".join(parts), pattern, skip is not None, form, aliases, long,
                           rank[name_base], partial, glob is not None))
    _shadow(out)
    return out


def _shadow(found: list[_Source]) -> None:
    """Mark each file whose module name Python resolves to another file: in one directory a package wins
    over an extension module, over source, over a sourceless .pyc; across roots, the first root wins."""
    by_name: dict[str, list[_Source]] = {}
    for s in found:
        by_name.setdefault(s.name, []).append(s)
    for same in by_name.values():
        if len(same) < 2:
            continue
        win = min(same, key=lambda s: (s.index, _RANK[s.form]))
        for s in same:
            if s is not win:
                s.shadowed_by = win


def iter_sources(roots: Iterable[pathlib.Path],
                 exclude: Iterable[str] = ()) -> Iterator[tuple[pathlib.Path, pathlib.Path, str]]:
    """(root, file, module name) for every .py file under the roots the scan reads, minus exclude globs
    (root-relative) and the directories the scan always skips (SKIP_DIRS, dot-directories)."""
    for base, f, name, pattern, _ in _walk_sources(roots, exclude):
        if pattern is None:
            yield base, f, name


def _walk_sources(roots: Iterable[pathlib.Path],
                  exclude: Iterable[str]) -> Iterator[tuple[pathlib.Path, pathlib.Path, str, str | None, bool]]:
    """(root, file, module name, the exclude glob it matches or None, skipped) for every .py file under the
    roots that Python would import under its name. A file under a skipped directory (SKIP_DIRS, a
    dot-directory) has that directory's name as its pattern and skipped True."""
    for s in _layout(roots, exclude):
        if s.form in ("source", "package") and s.shadowed_by is None:
            yield s.root, s.file, s.name, s.pattern, s.skip
