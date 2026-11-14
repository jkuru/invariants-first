"""Finding the source files under the roots, minus exclude globs and the always-skipped directories.

A file is indexed once, named from the deepest root that holds it (`src/app/x.py` is `app.x` with the
roots `src` and `.`); the name every other root gives it (`src.app.x`) is an alias of the same module.
It is left out of the scan only when, from every root that holds it, an exclude glob matches its path
or it lies in an always-skipped directory: adding a root never excludes more. In one directory, what
Python imports wins (a package `ops/` over an extension module `ops.so` over `ops.py` over a sourceless
`ops.pyc`); the code it shadows is noted. A module with no Python source (an extension module, a
sourceless `.pyc`) cannot be read; it is noted too, never scanned as empty.

A symlinked directory is followed, as Python's import system follows it: its files are named by the
link's path (`pkg/admin -> ../.vendor/admin` gives `pkg.admin.tasks`), a link back into its own
ancestry is not followed twice, and a target outside every root is still scanned and noted. A link
inside an always-skipped directory is not followed (a virtualenv's `lib64`, pnpm's node_modules).
Confined (`inv mcp`: confinement.confine_root(), through invariants_scan.hooks), a symlink to a directory or file that resolves
outside the confinement root is not followed either: it is collected as refused, and the scan reports
it as unresolved, naming only the link."""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib
import zipfile
from typing import Iterable, Iterator

from . import hooks
from .contracts import SKIP_DIRS

# the directories the scan always skips, as the lock pins them: SKIP_DIRS and every dot-directory
SKIPPED = (".*", *sorted(SKIP_DIRS))
_NO_SOURCE = (".so", ".pyd", ".pyc")             # importable modules the scan cannot read
_ARCHIVES = (".zip", ".egg", ".whl", ".pyz")    # zipimport reads these from sys.path
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


@dataclasses.dataclass
class Found:
    """What a walk of the roots finds besides the importable files."""

    # (root, link): a followed symlink to a directory (from _layout: only those outside every root)
    outside: list[tuple[pathlib.Path, pathlib.Path]] = dataclasses.field(default_factory=list)
    # (root, archive, the exclude glob that leaves it out from every root holding it, else None)
    archives: list[tuple[pathlib.Path, pathlib.Path, str | None]] = dataclasses.field(default_factory=list)
    # (root, link): a symlink not followed because it resolves outside the confinement root (inv mcp)
    refused: list[tuple[pathlib.Path, pathlib.Path]] = dataclasses.field(default_factory=list)


def _files(root: pathlib.Path, found: Found | None = None) -> list[pathlib.Path]:
    """Every importable file under root, following symlinked directories (each target once per walk, never
    one back into the link's own ancestry). A zip archive is collected into found. Confined, a symlink
    (to a directory or a file) that resolves outside the confinement root is not followed, and is
    collected into found.refused."""
    top = hooks.confine_root()
    if root.is_file():
        return [root]
    out: list[pathlib.Path] = []
    entered: set[str] = set()

    def out_of_bounds(p: pathlib.Path) -> bool:
        if top is None or pathlib.Path(os.path.realpath(p)).is_relative_to(top):
            return False
        if found is not None:
            found.refused.append((root, p))
        return True

    def walk(d: pathlib.Path, real: str, chain: frozenset[str], rel: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            p = d / e.name
            try:
                is_dir, link = e.is_dir(), e.is_symlink()
            except OSError:
                continue
            if is_dir:
                if not link:
                    walk(p, os.path.join(real, e.name), chain | {real}, (*rel, e.name))
                    continue
                if any(x in SKIP_DIRS or x.startswith(".") for x in (*rel, e.name)):
                    continue                          # a link in a skipped directory is not followed
                if out_of_bounds(p):
                    continue                          # confined: a link out of the root is not followed
                target = os.path.realpath(p)
                if target in chain or target == real or target in entered:
                    continue                          # a cycle, or a target this walk already entered by a link
                entered.add(target)
                if found is not None:
                    found.outside.append((root, p))
                walk(p, target, chain | {real}, (*rel, e.name))
            elif link and top is not None and e.name.endswith((".py", *_NO_SOURCE, *_ARCHIVES)) and (
                    any(x in SKIP_DIRS or x.startswith(".") for x in rel) or out_of_bounds(p)):
                continue                              # confined: a file link out of the root is not read
            elif e.name.endswith((".py", *_NO_SOURCE)):
                if p.is_file():
                    out.append(p)
            elif found is not None and e.name.endswith(_ARCHIVES) and p.is_file():
                found.archives.append((root, p, None))
    walk(root, str(root), frozenset(), ())
    return sorted(out)


def holds_code(file: pathlib.Path) -> int:
    """How many importable members (.py, .pyc, extension modules) a zip archive holds; 0 if not a zip."""
    try:
        with zipfile.ZipFile(file) as z:
            return sum(1 for n in z.namelist() if n.endswith((".py", *_NO_SOURCE)))
    except (zipfile.BadZipFile, OSError, ValueError):
        return 0


def _layout(roots: Iterable[pathlib.Path], exclude: Iterable[str], found: Found | None = None) -> list[_Source]:
    """Every importable file under the roots, once each, in root order then path order. With found, the
    symlinks to directories outside every root and the archives in the roots are collected there."""
    exclude = list(exclude)
    roots = list(dict.fromkeys(r.resolve() for r in roots))
    held: dict[pathlib.Path, list[tuple[pathlib.Path, pathlib.PurePath]]] = {}
    order: list[tuple[pathlib.Path, pathlib.Path]] = []
    walked = Found()
    for root in roots:
        base = root.parent if root.is_file() else root
        for f in _files(root, walked):
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
    if found is not None:
        dirs = [r for r in roots if r.is_dir()]
        links: dict[pathlib.Path, pathlib.Path] = {}  # each link once, from the deepest root holding it
        for r, link in walked.outside:
            if not any(_inside(os.path.realpath(link), str(d)) for d in dirs) and (
                    link not in links or len(r.parts) > len(links[link].parts)):
                links[link] = r
        found.outside += [(r, link) for link, r in links.items()]
        refused: dict[pathlib.Path, pathlib.Path] = {}  # each refused link once, from the deepest root
        for r, link in walked.refused:
            if link not in refused or len(r.parts) > len(refused[link].parts):
                refused[link] = r
        refused = {link: r for link, r in refused.items()       # left out by an exclude glob: not scanned anyway
                   if not (exclude and _excluded(link.relative_to(r), exclude))}
        found.refused += [(r, link) for link, r in refused.items()]
        seen: set[pathlib.Path] = set()
        for r, arc, _ in walked.archives:             # named from the deepest root, like a module
            if arc in seen:
                continue
            seen.add(arc)
            homes = sorted((d for d in dirs if _inside(str(arc), str(d))), key=lambda d: -len(d.parts))
            globs = [_excluded(arc.relative_to(d), exclude) if exclude else None for d in homes] or [None]
            found.archives.append((homes[0] if homes else r, arc, globs[0] if all(globs) else None))
    return out


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


_SITE = ("site-packages", "dist-packages")


def third_party(root: pathlib.Path, file: pathlib.Path,
                cache: dict[pathlib.Path, pathlib.Path | None] | None = None) -> pathlib.Path | None:
    """The directory a recognised third-party tree inside the root installs file's imports from, else None:
    a virtualenv (a pyvenv.cfg above the file; its site-packages if the file is in one), a site-packages
    holding distribution metadata (*.dist-info, *.egg-info), or a node package (node_modules/<pkg>/ with
    its package.json). A hand-made `site-packages/` or `node_modules/` directory is none of these."""
    if cache is None:
        return _third_party(root, file)
    if file.parent not in cache:
        cache[file.parent] = _third_party(root, file)
    return cache[file.parent]


def _third_party(root: pathlib.Path, file: pathlib.Path) -> pathlib.Path | None:
    rel = file.relative_to(root)
    dirs = [root.joinpath(*rel.parts[:i]) for i in range(len(rel.parts) - 1, 0, -1)]   # nearest first
    site = next((d for d in dirs if d.name in _SITE), None)
    if site is not None and any(_listing(site, (".dist-info", ".egg-info"))):
        return site
    if any((d / "pyvenv.cfg").is_file() for d in dirs):
        return site or next(d for d in dirs if (d / "pyvenv.cfg").is_file())
    for d in dirs:
        up = d.parent.parent if d.parent.name.startswith("@") else d.parent
        if up.name == "node_modules" and (d / "package.json").is_file():
            return d
    return None


def _listing(d: pathlib.Path, suffixes: tuple[str, ...]) -> Iterator[str]:
    try:
        yield from (n for n in os.listdir(d) if n.endswith(suffixes))
    except OSError:
        return


def provides(base: pathlib.Path, file: pathlib.Path, top: str) -> bool:
    """A directory from file's own up to base holds a top-level module or package named top: an import
    of it from file finds that copy first."""
    d = file.parent
    while True:
        if (d / f"{top}.py").is_file() or (d / top).is_dir() or any(
                n.split(".")[0] == top for n in _listing(d, _NO_SOURCE)):
            return True
        if d == base or base not in d.parents:
            return False
        d = d.parent


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
