"""The human-readable report."""
from __future__ import annotations

import re

from .contracts import Finding, Report

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_CONTROL_BUT_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(m: re.Match[str]) -> str:
    c = m.group()
    return _NAMED.get(c) or (f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")


def plain(value: object) -> str:
    """A value from the scanned tree (a path, a message naming one) on one line: every control
    character, a newline too, escaped as \\n or \\xNN, so a file name cannot start a line of its own."""
    return _CONTROL.sub(_escape, str(value))


def safe_output(text: str) -> str:
    """Text `inv` prints, made safe for a CI log: control characters other than newline and tab
    escaped (a carriage return or terminal escape cannot rewrite what a reader sees), and a line
    that would start a GitHub Actions workflow command (`::name::`) shown with a backslash first."""
    lines = _CONTROL_BUT_LINES.sub(_escape, text).split("\n")
    for i, line in enumerate(lines):
        body = line.lstrip()
        if body.startswith("::"):
            lines[i] = line[:len(line) - len(body)] + "\\" + body
    return "\n".join(lines)


def _where(f: Finding) -> str:
    return f"{plain(f.file)}:{f.line}:{f.col}" if f.file else "-"


def _covers(f: Finding) -> str:
    """What an accepted inferred path would cover besides its site (its sha covers it): the entrypoints
    and name-matched calls that reach it, which a reviewer reads before accepting it."""
    if not f.reached_by:
        return ""
    tops = [x for x in f.reached_by if x.startswith("entrypoint ")]
    calls = [x for x in f.reached_by if not x.startswith("entrypoint ")]
    shown = "; ".join(plain(c) for c in calls[:5]) + (f"; and {len(calls) - 5} more (--format json)" if len(calls) > 5 else "")
    return f"; covers {len(tops)} entrypoint(s) and {len(calls)} name-matched call(s): {shown}"


def _code(f: Finding) -> str:
    """The finding's code before its message (fix round 29: what an allow_unresolved entry names), unless
    it is its kind alone (bypass)."""
    return "" if f.code == f.kind else f"{plain(f.code)}: "


def to_text(report: Report) -> str:
    """Human-readable report, one finding per line, its code first. A rule that declares
    mediation.allow_unresolved shows, on each unresolved finding still failing, the sha an entry accepting
    it names (a rule that does not is told how to see them); an accepted finding is shown with its
    reviewer's reason."""
    lines = []
    opted = {rid for rid, m in report.scanned.items() if m.accept is not None}
    for f in report.findings:
        key = f"  [sha {f.sha}{_covers(f)}]" if f.rule in opted and f.kind == "unresolved" and f.sha else ""
        lines.append(f"{f.kind:<10} {plain(f.rule):<8} {_where(f)}  {_code(f)}{plain(f.message)}{key}")
    for f in report.accepted:
        moved = "" if f.moved_from is None else \
            f"; moved from line {f.moved_from}: update its at to {plain(f.file)}:{f.line}" if f.moved_col is None else \
            f"; moved from {f.moved_from}:{f.moved_col}: update its at to {plain(f.file)}:{f.line}:{f.col}"
        lines.append(f"{'accepted':<10} {plain(f.rule):<8} {_where(f)}  {_code(f)}{plain(f.message)} "
                     f"(allow_unresolved: {plain(f.why)}{moved})")
    for rid in report.scanned:
        c = report.counts(rid)
        summary = ", ".join(f"{n} {k}" for k, n in c.items()) or "no findings"
        lines.append(f"{rid}: {report.rule_result(rid)} ({summary})")
    hidden = [rid for rid in report.scanned if rid not in opted and any(
        f.rule == rid and f.kind == "unresolved" and f.file and f.line > 0 for f in report.findings)]
    if hidden:
        lines.append(f"note: {', '.join(plain(r) for r in hidden)}: to see each unresolved finding's sha on its "
                     f"line (what an allow_unresolved entry names), give the rule `allow_unresolved: []`; "
                     f"`inv scan --format json` gives it too")
    for rid, why in report.not_scanned.items():
        lines.append(f"{rid}: not scanned ({why})")
    lines += [f"note: {plain(n)}" for n in report.notes]
    if report.skipped:
        lines.append("skipped (directories the scan always skips): "
                     + ", ".join(f"{plain(d)}/ {n} file(s)" for d, n in report.skipped.items()))
    lines.append(f"scanned {report.files} file(s), {report.functions} function(s)")
    return "\n".join(lines)
