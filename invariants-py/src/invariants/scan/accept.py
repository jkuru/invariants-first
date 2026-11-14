"""Reviewed exceptions: mediation.allow_unresolved accepts an unresolved finding, one site at a time.

A person who has read an unresolved site and found it cannot reach the sink writes an entry for it:

    allow_unresolved:
      - {at: "app/plugins.py:41", code: unresolved/reflection, sha: 3f9c0a17be,
         why: "loads the export plugins named in settings.EXPORTERS; none imports app.store"}

An entry matches a finding of that rule with the same file, code and sha. The sha is a short hash of the
finding's code, the function it is in and its source line with the spacing normalized, so an edit above
the site that moves it keeps the entry (the finding is reported as moved, with its new line), and an edit
of the site itself does not. Each entry accepts one finding; the one at its line first, else the nearest.
A matched finding is shown as accepted and never fails the rule; an entry that matches nothing is
missing/allow-unresolved-unused. bypass and forbidden findings can never be accepted (the rule file
refuses such an entry). Adding or widening an entry is a weakening in the lock; removing one is not.
"""
from __future__ import annotations

import hashlib
import pathlib

from .contracts import Accept, Finding


class _Lines:
    """The source lines of the files findings are in, read once each."""

    def __init__(self) -> None:
        self._files: dict[pathlib.Path, list[str]] = {}

    def line(self, root: str | None, file: str, line: int) -> str:
        path = pathlib.Path(root or ".") / file
        if path not in self._files:
            try:
                self._files[path] = path.read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
                self._files[path] = []
        lines = self._files[path]
        return lines[line - 1] if 0 < line <= len(lines) else ""


def site_sha(code: str, owner: str | None, text: str) -> str:
    """The key of an unresolved finding: its code, its function and its source line, spacing normalized."""
    norm = " ".join(text.split())
    return hashlib.sha256("\0".join([code, owner or "", norm]).encode("utf-8")).hexdigest()[:10]


def fill_sha(findings: list[Finding], lines: _Lines) -> None:
    """Give every unresolved finding in a file its sha (what an allow_unresolved entry names)."""
    for f in findings:
        if f.kind == "unresolved" and f.file and f.line > 0 and f.sha is None:
            f.sha = site_sha(f.code, f.owner, lines.line(f.root, f.file, f.line))


def apply(rule: str, entries: list[Accept], findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(the findings still reported, the accepted ones): each entry accepts one unresolved finding with
    its file, code and sha, the one at its line (and column, when given) first, else the nearest; an entry
    that matches none is a missing/allow-unresolved-unused finding."""
    taken: set[int] = set()
    accepted: list[Finding] = []
    unused: list[Finding] = []
    for e in entries:
        cands = [f for f in findings if id(f) not in taken and f.kind == "unresolved" and f.file == e.file
                 and f.code == e.code and f.sha == e.sha]
        here = [f for f in cands if f.line == e.line and (e.col is None or f.col == e.col)]
        pick = here[0] if here else min(cands, key=lambda f: (abs(f.line - e.line), f.line, f.col), default=None)
        if pick is None:
            unused.append(Finding("missing", rule, "missing/allow-unresolved-unused",
                                  f"allow_unresolved entry {e.at} ({e.code}, sha {e.sha}) matches no unresolved "
                                  f"finding: the site was edited, removed or now resolves; delete the entry (a "
                                  f"tightening), or review the site again and write a new one",
                                  e.file, e.line, e.col or 1))
            continue
        taken.add(id(pick))
        pick.why = e.why
        if pick.line != e.line:
            pick.moved_from = e.line
        accepted.append(pick)
    return [f for f in findings if id(f) not in taken] + unused, accepted
