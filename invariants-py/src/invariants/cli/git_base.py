"""`inv check --base`: the lock committed at a git ref, read through git (never a symlink), and the
changes since it."""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Sequence

from .. import lock
from ..errors import LoadError
from .common import ALLOW_WEAKENING_ENV, FAILED, OK, _compare, _out, _print_changes
from ..project import _setuptools_where, _str_list, effective_exclude
from .worker import Imported


def _git(cwd: pathlib.Path, *args: str) -> tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return p.returncode, p.stdout if p.returncode == 0 else p.stderr.strip()


class BaseUnavailable(Exception):
    """`--base` cannot compare with the base branch. A reviewer may accept it (--accept-weakening) when
    the base lock exists but cannot be read or compared; never when the base ref itself is missing."""

    def __init__(self, why: str, acceptable: bool = False) -> None:
        super().__init__(why)
        self.acceptable = acceptable


def _symlinked(p: pathlib.Path, top: pathlib.Path) -> pathlib.Path | None:
    """The first symlink on the way from the repository root `top` down to `p` (p itself included)."""
    for q in [p, *p.parents]:
        if q.resolve() == top:
            return None
        if q.is_symlink():
            return q
    return None


def _ls_tree(top: pathlib.Path, ref: str, flags: Sequence[str] = (),
             paths: Sequence[str] = ()) -> list[tuple[str, str, str]]:
    """(mode, type, path) of each entry `git ls-tree` lists at `ref`."""
    code, text = _git(top, "ls-tree", "--full-tree", "-z", *flags, ref, "--", *paths)
    if code != 0:
        raise BaseUnavailable(f"cannot list {ref} ({text})")
    out = []
    for rec in text.split("\0"):
        head, _, name = rec.partition("\t")
        parts = head.split()
        if len(parts) == 3:
            out.append((parts[0], parts[1], name))
    return out


def _repo_root(where: pathlib.Path) -> pathlib.Path:
    code, top = _git(where, "rev-parse", "--show-toplevel")
    if code != 0:
        raise BaseUnavailable(f"not a git checkout ({top}); --base needs the repository and the base ref")
    return pathlib.Path(top.strip()).resolve()


def _base_lock(lock_path: pathlib.Path, ref: str) -> tuple[dict[str, Any] | None, str]:
    """The lock committed at git `ref` and its name there, or (None, name) when `ref` has no lock at
    the lock's path and no lock that this change moved or removed. The path is the lock's own: a
    symlinked lock, or one under a symlinked directory, is refused, never followed."""
    where = lock_path.parent.resolve()
    root = _repo_root(where)
    if _git(where, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] != 0:
        raise BaseUnavailable(f"base ref {ref} not found; fetch it first (for example `git fetch origin main`)")
    link = _symlinked(pathlib.Path(os.path.abspath(lock_path)), root)
    if link is not None:
        raise BaseUnavailable(f"{link} is a symlink; the lock must be a regular file at its own path, so "
                              f"--base compares the lock committed there")
    rel = (where / lock_path.name).relative_to(root).as_posix()
    name = f"{ref}:{rel}"
    entry = next((e for e in _ls_tree(root, ref, paths=[rel]) if e[2] == rel), None)
    if entry is None:
        gone = [n for _, kind, n in _ls_tree(root, ref, ["-r"])
                if n.rsplit("/", 1)[-1] == lock.LOCK_NAME and kind == "blob"
                and (_symlinked(root / n, root) is not None or not (root / n).is_file())]
        if gone:
            raise BaseUnavailable(f"{ref} has {', '.join(gone)}, which this change moved or removed: not a "
                                  f"first adoption, and the rules it pinned are not compared", acceptable=True)
        return None, name
    if entry[0] == "120000" or entry[1] != "blob":
        raise BaseUnavailable(f"{name} is not a regular file at {ref} (git mode {entry[0]})", acceptable=True)
    code, text = _git(root, "show", name)
    try:
        if code != 0:
            raise ValueError(text)
        stored = lock.loads(text)
        if not isinstance(stored, dict):
            raise ValueError("not a JSON object")
        lock.check_version(stored, name)
        lock.validate(stored, name)
    except (ValueError, LoadError) as e:
        raise BaseUnavailable(f"{name} is not a lock this invariants reads ({e})", acceptable=True) from e
    return stored, name


BaseRead = tuple[dict[str, Any] | None, str] | BaseUnavailable   # what _base_lock gave or raised


def base_changes(path: pathlib.Path, lock_path: pathlib.Path, ref: str, extra: Sequence[str],
                 scan: dict[str, Any] | None = None, imported: Imported | None = None,
                 base: BaseRead | None = None) -> tuple[list[lock.Change] | None, str]:
    """The changes between the lock committed at git `ref` and the rule file as it is now, or (None, why)
    when `ref` has no lock (a first adoption, reviewed as a whole). BaseUnavailable when there is no
    git checkout, no such ref, a symlinked lock, a lock the change moved or removed, or a lock at
    `ref` that cannot be read or compared. `base` is what _base_lock gave (or raised) when `inv
    check` read it before the child ran, and `imported` that child, which pins the rule file: this
    process never imports the app."""
    got = _base_lock(lock_path, ref) if base is None else base
    if isinstance(got, BaseUnavailable):
        raise got
    stored, name = got
    if stored is None:
        return None, f"no {name}: first adoption of invariants, nothing to compare (review the whole lock)"
    imp = imported or Imported(path, extra)
    imp.want_old(stored)
    fresh = imp.build(scan)["fresh"]
    types = imp.types(stored)
    default = base_scan_default(imp, path, ref) if "scan" not in stored and "scan" in fresh else None
    return _compare(lambda: lock.changes(stored, fresh, types, default), lock_path), ""


def base_read(lock_path: pathlib.Path, ref: str) -> BaseRead:
    """_base_lock's result, or the BaseUnavailable it raised."""
    try:
        return _base_lock(lock_path, ref)
    except BaseUnavailable as e:
        return e


def base_scan_default(imp: Imported, path: pathlib.Path, ref: str) -> dict[str, list[str]]:
    """_base_scan_default, read once per command (before the child runs, when `inv check` asks first)."""
    got: dict[str, list[str]] = imp.before(("base-default", ref), lambda: _base_scan_default(path, ref))
    return got


def _base_scan_default(path: pathlib.Path, ref: str) -> dict[str, list[str]]:
    """The roots and excludes the scan used at git `ref` with no --root or --exclude: [tool.invariants.scan]
    of the pyproject.toml beside the rule file there, else its src/ (and setuptools `where`) and the
    rule file's directory, as scan_settings gives them in a checkout of `ref`."""
    import posixpath
    import tomllib
    where = path.parent.resolve()
    root = _repo_root(where)
    rel = where.relative_to(root).as_posix()
    pre = "" if rel in ("", ".") else rel + "/"
    tree = {n: kind for _, kind, n in _ls_tree(root, ref, ["-r", "-t"])}
    has_py = tree.get(pre + "pyproject.toml") == "blob"
    tool: dict[str, Any] = {}
    if has_py:
        code, text = _git(root, "show", f"{ref}:{pre}pyproject.toml")
        try:
            tool = (tomllib.loads(text).get("tool") or {}) if code == 0 else {}
        except tomllib.TOMLDecodeError:
            tool = {}
    cfg = (tool.get("invariants") or {}).get("scan") or {}
    roots = [posixpath.normpath(r) for r in _str_list(cfg.get("roots"))]
    if not roots:
        dirs = [posixpath.normpath(d) for d in [*_setuptools_where(tool), "src"]] if has_py else []
        roots = [*(d for d in dirs if d != "." and tree.get(posixpath.normpath(pre + d)) == "tree"), "."]
    return lock.scan_pin(roots, effective_exclude(cfg))


def _base_step(path: pathlib.Path, lock_path: pathlib.Path, ref: str, extra: Sequence[str], allow: bool,
               scan: dict[str, Any] | None = None, imported: Imported | None = None,
               base: BaseRead | None = None, record: list[lock.Change] | None = None) -> int:
    try:
        changes, why = base_changes(path, lock_path, ref, extra, scan, imported, base)
        if record is not None and changes:
            record.extend(changes)
    except BaseUnavailable as e:
        if allow and e.acceptable:
            _out(f"{e} (allowed by a reviewer)")
            return OK
        _out(f"cannot compare with {ref}: {e}")
        if e.acceptable:
            _out(f"a person may accept this: --accept-weakening or {ALLOW_WEAKENING_ENV}=1, set by a reviewer")
        return FAILED
    if changes is None:
        _out(why)
        return OK
    weak = [c for c in changes if c.weakened]
    if not weak:
        _out(f"{len(changes)} change(s) since {ref}, none weakens a rule")
        return OK
    _out(f"{len(weak)} change(s) since {ref} weaken a rule" + (" (allowed by a reviewer)" if allow else ":"))
    _print_changes(weak)
    if allow:
        return OK
    _out(f"a person must accept these: re-run with --accept-weakening or {ALLOW_WEAKENING_ENV}=1, set by a reviewer "
         f"(for example from a pull-request label), never by the change's author or agent")
    return FAILED
