"""`inv check`'s scan step: two tiers, and with --base only what is new since the base blocks.

Bypass, forbidden and missing findings block everywhere. An unresolved finding (a call the scan cannot
follow) is advisory: printed as a WARN line, and the step passes. With `--base REF` the scan also runs
on REF's copy of the scan roots (read with `git archive`, scanned with the rule file as it is now), and
an unresolved site that is not on REF blocks ("new since REF"); one that is stays a warning ("on REF").
Sites are compared as a multiset of (rule, code, file, sha): the sha hashes the site's code, function
and statement, never its line, so code that only moved is still the same site. When REF cannot be read
(no such ref, a shallow clone without it, a root outside the repository) every unresolved site blocks:
the check fails closed. `inv check --strict-scan`, or `mediation.strict: true` on a rule, makes every
unresolved site of that rule block (the behaviour before diff-first). An enforce rule the scan skipped
because it is ablated fails the step. `inv scan` itself is unchanged: it still exits 3 on unresolved.
"""
from __future__ import annotations

import argparse
import collections
import io
import pathlib
import posixpath
import subprocess
import tarfile
import tempfile
from typing import Any, Sequence

import invariants_scan as scanner

from ..loader import read_document
from ..project import _exclude, _roots
from .common import FAILED, OK, _out
from .git_base import BaseUnavailable, _git, _ls_tree, _repo_root

BLOCKING = frozenset({"bypass", "forbidden", "missing"})
Key = tuple[str, str, "str | None", "str | None"]


def site_key(f: scanner.Finding) -> Key:
    """What makes an unresolved site the same site on the base: its rule, code, file (relative to its
    root) and sha (its code, function and statement; never its line). Reading sha fills it."""
    return (f.rule, f.code, f.file, f.sha)


def _archive(top: pathlib.Path, ref: str, paths: Sequence[str]) -> bytes:
    """`git archive` of `paths` at `ref` (a tar), or BaseUnavailable."""
    try:
        p = subprocess.run(["git", "archive", "--format=tar", ref, "--", *paths], cwd=top, capture_output=True,
                           timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise BaseUnavailable(f"git archive {ref} failed ({e})") from e
    if p.returncode != 0:
        raise BaseUnavailable(f"git archive {ref} failed ({p.stderr.decode('utf-8', 'replace').strip()})")
    return p.stdout


def _extract(data: bytes, dest: pathlib.Path) -> None:
    """The regular files and directories of a tar from `git archive`, under dest. A symlink, a hard link
    or a name that leaves dest is skipped, never followed (its sites then count as new: they block)."""
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            parts = pathlib.PurePosixPath(m.name).parts
            if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                continue
            target = dest.joinpath(*parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is not None:
                target.write_bytes(src.read())


def base_sites(path: pathlib.Path, ref: str, roots: Sequence[pathlib.Path], exclude: Sequence[str],
               doc: dict[str, Any]) -> collections.Counter[Key]:
    """The unresolved sites (site_key) the scan finds in git `ref`'s copy of the scan roots, scanned with
    the rule file as it is now (`doc`); raises BaseUnavailable when `ref` cannot be read. A root `ref`
    does not have is scanned empty: every site in it is new."""
    where = path.resolve().parent
    top = _repo_root(where)
    if _git(where, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] != 0:
        raise BaseUnavailable(f"base ref {ref} not found")
    rels = []
    for r in roots:
        try:
            rel = pathlib.Path(r).resolve().relative_to(top).as_posix()
        except ValueError:
            raise BaseUnavailable(f"scan root {r} is outside the git repository {top}") from None
        rels.append(posixpath.normpath(rel))
    tree = {n for _, _, n in _ls_tree(top, ref, ["-r", "-t"])}
    present = [r for r in rels if r == "." or r in tree]
    with tempfile.TemporaryDirectory(prefix="inv-base-") as tmp:
        dest = pathlib.Path(tmp)
        if present:
            _extract(_archive(top, ref, present), dest)
        base_roots = [dest / r for r in rels]
        for r in base_roots:
            r.mkdir(parents=True, exist_ok=True)
        report = scanner.scan(doc, base_roots, exclude)
        # every sha is read here, while the files it hashes are still on disk
        return collections.Counter(site_key(f) for f in report.findings if f.kind == "unresolved")


def _strict_rules(doc: dict[str, Any]) -> set[str]:
    """The rules whose mediation says strict: true (every unresolved site blocks)."""
    out = set()
    for r in doc.get("rules") or []:
        med = r.get("mediation") if isinstance(r, dict) else None
        if isinstance(med, dict) and med.get("strict") is True and isinstance(r.get("id"), str):
            out.add(r["id"])
    return out


def cmd_scan_gate(a: argparse.Namespace) -> int:
    """The scan step: the scan's text, each unresolved line marked, and the verdict (OK or FAILED). The
    counts the trial metrics read are put in a.gate (a dict)."""
    path = pathlib.Path(a.rulefile)
    doc = read_document(path)
    roots, exclude = _roots(a, path), _exclude(a, path)
    report = scanner.scan(doc, roots, exclude)
    strict = _strict_rules(doc)
    base: collections.Counter[Key] | None = None
    unreadable = ""
    if a.base:
        try:
            base = base_sites(path, a.base, roots, exclude, doc)
        except (BaseUnavailable, OSError) as e:      # a temporary copy that cannot be written, say
            unreadable = str(e)
    lines = scanner.to_text(report).splitlines()
    blocking = sum(1 for f in report.findings if f.kind in BLOCKING)
    new = legacy = warned = blocked = 0
    for i, f in enumerate(report.findings):
        if f.kind != "unresolved":
            continue
        tag, block = "", False
        if base is not None:
            k = site_key(f)
            if base[k] > 0:
                base[k] -= 1
                legacy += 1
                tag = f"  [on {a.base}]"
            else:
                new += 1
                tag, block = f"  [new since {a.base}]", True
        elif a.base:                                   # the base cannot be read: fail closed
            new += 1
            tag, block = f"  [{a.base} unreadable]", True
        if (a.strict_scan or f.rule in strict) and not block:
            tag, block = tag + "  [strict]", True
        if block:
            blocked += 1
            lines[i] += tag
        else:
            warned += 1
            lines[i] = f"WARN  {lines[i]}{tag}"
    a.gate.update(blocking=blocking, new=new if a.base else None, legacy=legacy if a.base else None,
                  unresolved=new + legacy if a.base else warned + blocked, accepted=len(report.accepted),
                  warned=warned)
    for line in lines:
        _out(line)
    if unreadable:
        _out(f"cannot read the scan roots at {a.base} ({unreadable}), so every unresolved site blocks (fail "
             f"closed); fetch the base with its history (`git fetch origin <branch>`, or actions/checkout "
             f"with fetch-depth: 0) and run again")
    if blocking:
        return FAILED
    if report.ablated_enforce:
        _out(f"enforce rule(s) {', '.join(report.ablated_enforce)} skipped because they are ablated "
             f"(INV_ABLATE); this is not a pass")
        return FAILED
    return FAILED if blocked else OK
