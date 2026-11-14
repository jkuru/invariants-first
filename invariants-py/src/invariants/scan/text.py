"""The human-readable report."""
from __future__ import annotations

from .contracts import Finding, Report


def _where(f: Finding) -> str:
    return f"{f.file}:{f.line}:{f.col}" if f.file else "-"


def _covers(f: Finding) -> str:
    """What an accepted inferred path would cover besides its site (its sha covers it): the entrypoints
    and name-matched calls that reach it, which a reviewer reads before accepting it."""
    if not f.reached_by:
        return ""
    tops = [x for x in f.reached_by if x.startswith("entrypoint ")]
    calls = [x for x in f.reached_by if not x.startswith("entrypoint ")]
    shown = "; ".join(calls[:5]) + (f"; and {len(calls) - 5} more (--format json)" if len(calls) > 5 else "")
    return f"; covers {len(tops)} entrypoint(s) and {len(calls)} name-matched call(s): {shown}"


def to_text(report: Report) -> str:
    """Human-readable report, one finding per line. A rule that declares mediation.allow_unresolved shows,
    on each unresolved finding still failing, the sha an entry accepting it names; an accepted finding
    is shown with its reviewer's reason."""
    lines = []
    opted = {rid for rid, m in report.scanned.items() if m.accept is not None}
    for f in report.findings:
        key = f"  [sha {f.sha}{_covers(f)}]" if f.rule in opted and f.kind == "unresolved" and f.sha else ""
        lines.append(f"{f.kind:<10} {f.rule:<8} {_where(f)}  {f.message}{key}")
    for f in report.accepted:
        moved = "" if f.moved_from is None else \
            f"; moved from line {f.moved_from}: update its at to {f.file}:{f.line}" if f.moved_col is None else \
            f"; moved from {f.moved_from}:{f.moved_col}: update its at to {f.file}:{f.line}:{f.col}"
        lines.append(f"{'accepted':<10} {f.rule:<8} {_where(f)}  {f.message} (allow_unresolved: {f.why}{moved})")
    for rid in report.scanned:
        c = report.counts(rid)
        summary = ", ".join(f"{n} {k}" for k, n in c.items()) or "no findings"
        lines.append(f"{rid}: {report.rule_result(rid)} ({summary})")
    for rid, why in report.not_scanned.items():
        lines.append(f"{rid}: not scanned ({why})")
    lines += [f"note: {n}" for n in report.notes]
    if report.skipped:
        lines.append("skipped (directories the scan always skips): "
                     + ", ".join(f"{d}/ {n} file(s)" for d, n in report.skipped.items()))
    lines.append(f"scanned {report.files} file(s), {report.functions} function(s)")
    return "\n".join(lines)
